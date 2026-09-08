from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.harness import WorkerGroup, drop_benchmark_schema
from repomesh.config import Settings
from repomesh.models import SearchMode
from repomesh.providers import QdrantVectorStore
from repomesh.repositories import current_commit, register_repository
from repomesh.services import Services


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def evaluate_case(paths: list[str], gold: set[str]) -> dict[str, float]:
    relevance = [1 if path in gold else 0 for path in paths[:10]]
    first = next((rank for rank, relevant in enumerate(relevance, 1) if relevant), None)

    def recall(k: int) -> float:
        return len(set(paths[:k]) & gold) / len(gold)

    dcg = sum(rel / math.log2(rank + 1) for rank, rel in enumerate(relevance, 1))
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(gold), 10) + 1))
    return {
        "recall_at_3": recall(3),
        "recall_at_5": recall(5),
        "mrr_at_10": 1 / first if first else 0.0,
        "ndcg_at_10": dcg / ideal if ideal else 0.0,
        "hit": 1.0 if first else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results.json"))
    parser.add_argument("--embedding-provider", choices=["fake", "ollama"], default="fake")
    parser.add_argument("--vector-provider", choices=["memory", "qdrant"], default="memory")
    parser.add_argument("--embedding-dimensions", type=int, default=384)
    args = parser.parse_args()
    source_root = args.repository.resolve()
    temporary = tempfile.TemporaryDirectory(prefix="repomesh-evaluation-")
    temporary_root = Path(temporary.name)
    repository_root = temporary_root / "corpus"
    repository_root.mkdir()
    shutil.copytree(source_root / "sample_repository", repository_root / "sample_repository")
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repository_root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "evaluation@repomesh.local"],
        cwd=repository_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "RepoMesh Evaluation"],
        cwd=repository_root,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository_root, check=True)
    commit_environment = os.environ.copy()
    commit_environment.update(
        {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    )
    subprocess.run(
        ["git", "commit", "-m", "deterministic evaluation corpus"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        env=commit_environment,
    )
    cases = json.loads((Path(__file__).with_name("cases.json")).read_text(encoding="utf-8"))
    settings = Settings(
        postgres_schema="bench_" + uuid.uuid4().hex,
        data_dir=args.data_dir or temporary_root / "data",
        repository_roots=[repository_root.parent],
        embedding_provider=args.embedding_provider,
        generation_provider="fake",
        vector_provider=args.vector_provider,
        embedding_dimensions=args.embedding_dimensions,
        qdrant_collection=f"repomesh_evaluation_{os.getpid()}",
    )
    services = Services.create(settings)
    workers = WorkerGroup(services)
    try:
        workers.__enter__()
        repository = register_repository(repository_root, "RepoMesh evaluation corpus")
        if not services.database.repository(repository.id):
            services.database.add_repository(repository)
        job = services.jobs.submit(repository.id, "full", f"eval-{current_commit(repository_root)}")
        completed = services.jobs.wait(job.id, timeout=600)
        modes: dict[str, Any] = {}
        raw_cases: dict[str, list[dict[str, Any]]] = {}
        for mode in SearchMode:
            per_case = []
            latencies = []
            for case in cases:
                response = services.retriever.search(repository.id, case["query"], mode, 10, 30, 30)
                paths = list(dict.fromkeys(result.file_path for result in response.results))
                metrics = evaluate_case(paths, set(case["gold_files"]))
                latencies.append(response.latency_ms)
                per_case.append(
                    {
                        "id": case["id"],
                        "query": case["query"],
                        "gold_files": case["gold_files"],
                        "retrieved": [
                            {
                                "file": result.file_path,
                                "symbol": result.symbol_name,
                                "lines": [result.start_line, result.end_line],
                                "score": result.score,
                            }
                            for result in response.results
                        ],
                        **metrics,
                        "latency_ms": response.latency_ms,
                    }
                )
            modes[mode.value] = {
                key: statistics.fmean(item[key] for item in per_case)
                for key in ["recall_at_3", "recall_at_5", "mrr_at_10", "ndcg_at_10", "hit"]
            }
            modes[mode.value].update(
                {
                    "average_latency_ms": statistics.fmean(latencies),
                    "p50_latency_ms": percentile(latencies, 0.50),
                    "p95_latency_ms": percentile(latencies, 0.95),
                }
            )
            raw_cases[mode.value] = per_case
        payload = {
            "kind": "retrieval_evaluation",
            "generated_at": datetime.now(UTC).isoformat(),
            "repository": "temporary Git corpus built from sample_repository/",
            "source_fixture": str(source_root / "sample_repository"),
            "commit_sha": current_commit(repository_root),
            "dataset": {
                "case_count": len(cases),
                "indexed_files": completed.indexed_files,
                "indexed_chunks": completed.indexed_chunks,
            },
            "providers": {
                "embedding": services.embedder.model,
                "vector_store": settings.vector_provider,
            },
            "metrics": modes,
            "cases": raw_cases,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        combined: dict[str, Any] = {}
        if args.output.exists():
            try:
                existing = json.loads(args.output.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    combined = existing
            except (json.JSONDecodeError, OSError):
                pass
        if "kind" in combined:
            combined = {}
        combined["retrieval_evaluation"] = payload
        args.output.write_text(json.dumps(combined, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(args.output.resolve()), "metrics": modes}, indent=2))
    finally:
        if isinstance(services.vector_store, QdrantVectorStore):
            try:
                services.vector_store.client.delete_collection(services.vector_store.collection)
            except Exception:
                pass
        workers.close()
        services.close()
        drop_benchmark_schema(settings)
        temporary.cleanup()


if __name__ == "__main__":
    main()
