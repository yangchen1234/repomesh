from __future__ import annotations

import argparse
import json
import math
import os
import platform
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from repomesh.config import Settings
from repomesh.models import SearchMode
from repomesh.providers import QdrantVectorStore
from repomesh.repositories import current_commit, register_repository
from repomesh.services import Services

QUERIES = [
    "incremental indexing file sha deletion",
    "Tree-sitter function chunk lines",
    "reciprocal rank fusion lexical vector",
    "API token authentication",
    "job restart recovery lease heartbeat",
    "answer citation file path lines",
    "Ollama embedding provider",
    "Qdrant vector search",
    "repository allowlist path traversal",
    "Prometheus health metrics",
]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def timed_queries(services: Services, repository_id: str, concurrency: int) -> dict[str, Any]:
    latencies: list[float] = []

    def execute(query: str) -> None:
        started = time.perf_counter()
        services.retriever.search(repository_id, query, SearchMode.hybrid, 5, 20, 20)
        latencies.append((time.perf_counter() - started) * 1000)

    workload = QUERIES * concurrency
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        list(executor.map(execute, workload))
    wall = time.perf_counter() - started
    return {
        "concurrency": concurrency,
        "query_count": len(workload),
        "wall_seconds": wall,
        "queries_per_second": len(workload) / wall,
        "p50_latency_ms": percentile(latencies, 0.5),
        "p95_latency_ms": percentile(latencies, 0.95),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results.json"))
    parser.add_argument("--embedding-provider", choices=["fake", "ollama"], default="fake")
    parser.add_argument("--vector-provider", choices=["memory", "qdrant"], default="memory")
    parser.add_argument("--embedding-dimensions", type=int, default=384)
    args = parser.parse_args()
    source = args.repository.resolve()
    process = psutil.Process(os.getpid())
    with tempfile.TemporaryDirectory(prefix="repomesh-benchmark-") as temporary:
        temp_root = Path(temporary)
        clone = temp_root / "corpus"
        subprocess.run(["git", "clone", "--quiet", "--local", str(source), str(clone)], check=True)
        settings = Settings(
            data_dir=temp_root / "data",
            repository_roots=[temp_root],
            embedding_provider=args.embedding_provider,
            generation_provider="fake",
            vector_provider=args.vector_provider,
            embedding_dimensions=args.embedding_dimensions,
            qdrant_collection=f"repomesh_benchmark_{os.getpid()}",
        )
        services = Services.create(settings)
        try:
            repository = register_repository(clone, "RepoMesh benchmark clone")
            services.database.add_repository(repository)
            cpu_before = psutil.cpu_percent(interval=0.2)
            full_started = time.perf_counter()
            full_job = services.jobs.submit(repository.id, "full", "benchmark-full")
            full = services.jobs.wait(full_job.id, timeout=900)
            full_seconds = time.perf_counter() - full_started
            (clone / "sample_repository" / "parcelflow" / "tracking.py").write_text(
                (clone / "sample_repository" / "parcelflow" / "tracking.py").read_text(
                    encoding="utf-8"
                )
                + "\n# benchmark modification\n",
                encoding="utf-8",
            )
            (clone / "sample_repository" / "parcelflow" / "benchmark_added.py").write_text(
                "def benchmark_added():\n    return 'added'\n", encoding="utf-8"
            )
            removed = clone / "sample_repository" / "web" / "status.css"
            removed.unlink()
            incremental_started = time.perf_counter()
            inc_job = services.jobs.submit(
                repository.id, "incremental", "benchmark-add-modify-delete"
            )
            incremental = services.jobs.wait(inc_job.id, timeout=300)
            incremental_seconds = time.perf_counter() - incremental_started
            single_file = clone / "sample_repository" / "parcelflow" / "config.py"
            single_file.write_text(
                single_file.read_text(encoding="utf-8") + "\n# one-file change\n", encoding="utf-8"
            )
            single_started = time.perf_counter()
            single_job = services.jobs.submit(repository.id, "incremental", "benchmark-one-file")
            single = services.jobs.wait(single_job.id, timeout=300)
            single_seconds = time.perf_counter() - single_started
            concurrency = [timed_queries(services, repository.id, level) for level in (1, 5, 10)]
            cpu_after = psutil.cpu_percent(interval=0.2)
            payload = {
                "kind": "local_performance_benchmark",
                "generated_at": datetime.now(UTC).isoformat(),
                "scope": "Measurements describe only this Windows machine and this repository snapshot.",
                "system": {
                    "os": platform.platform(),
                    "cpu": platform.processor(),
                    "logical_cpu_count": psutil.cpu_count(),
                    "ram_bytes": psutil.virtual_memory().total,
                    "process_rss_bytes": process.memory_info().rss,
                    "cpu_percent_before": cpu_before,
                    "cpu_percent_after": cpu_after,
                },
                "dataset": {
                    "source_repository": str(source),
                    "commit_sha": current_commit(source),
                    "full_index_files": full.indexed_files,
                    "full_index_chunks": full.indexed_chunks,
                },
                "providers": {
                    "embedding_model": services.embedder.model,
                    "embedding_version": services.embedder.version,
                    "vector_store": settings.vector_provider,
                    "qdrant_version": services.vector_store.version(),
                },
                "full_index": {
                    "seconds": full_seconds,
                    "files_per_second": full.indexed_files / full_seconds,
                    "chunks_per_second": full.indexed_chunks / full_seconds,
                    "embedding_batches_per_second": full.indexed_files / full_seconds,
                    "embedding_batch_definition": "one file per embedding call",
                    "errors": full.error_count,
                },
                "incremental_add_modify_delete": {
                    "seconds": incremental_seconds,
                    "indexed_files": incremental.indexed_files,
                    "deleted_files": incremental.deleted_files,
                    "skipped_files": incremental.skipped_files,
                    "errors": incremental.error_count,
                },
                "incremental_one_file": {
                    "seconds": single_seconds,
                    "indexed_files": single.indexed_files,
                    "deleted_files": single.deleted_files,
                    "skipped_files": single.skipped_files,
                    "speedup_vs_full_wall_time": full_seconds / single_seconds,
                    "errors": single.error_count,
                },
                "query_concurrency": concurrency,
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
            combined["performance_benchmark"] = payload
            args.output.write_text(json.dumps(combined, indent=2), encoding="utf-8")
            print(json.dumps(payload, indent=2))
        finally:
            if isinstance(services.vector_store, QdrantVectorStore):
                try:
                    services.vector_store.client.delete_collection(services.vector_store.collection)
                except Exception:
                    pass
            services.close()


if __name__ == "__main__":
    main()
