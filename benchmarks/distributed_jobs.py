from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from benchmarks.harness import (
    WorkerGroup,
    audit,
    corpus,
    isolated_runtime,
    process_tree_ids,
    wait_jobs,
)
from repomesh.config import Settings
from repomesh.models import SearchMode


def scaling(
    base: Settings, work_dir: Path, workers: int, repositories: int, copies: int
) -> dict[str, Any]:
    with isolated_runtime(base, work_dir) as services:
        ids = corpus(services, repositories, copies)
        with WorkerGroup(services, workers):
            started = time.perf_counter()
            jobs = [services.jobs.submit(repo, "full", None) for repo in ids]
            results = wait_jobs(services, jobs)
            duration = time.perf_counter() - started
            registry = services.coordination.list_workers()
        observed = audit(services)
        files = sum(job.indexed_files for job in results)
        chunks = sum(job.indexed_chunks for job in results)
        completed = sum(job.status == "completed" for job in results)
        failed = sum(job.status == "failed" for job in results)
        assert completed == len(jobs), "indexing workload did not complete cleanly"
        assert not observed["duplicate_active_claims"] and not observed["repository_lease_overlaps"]
        for repo in ids:
            stored = services.database.chunks(repo)
            assert len(stored) == len({chunk["id"] for chunk in stored})
            assert services.retriever.search(
                repo, "inventory", SearchMode.lexical, 3, 10, 10
            ).results
        return {
            "workers": workers,
            "jobs": repositories,
            "duration_seconds": duration,
            "jobs_per_second": repositories / duration,
            "repositories_per_second": repositories / duration,
            "files_per_second": files / duration,
            "chunks_per_second": chunks / duration,
            "indexed_files": files,
            "indexed_chunks": chunks,
            "completed": completed,
            "failed": failed,
            "completed_with_errors": sum(job.status == "completed_with_errors" for job in results),
            "worker_distribution": [
                {
                    "worker_id": w["worker_id"],
                    "pid": w["pid"],
                    "completed_jobs": w["completed_jobs"],
                }
                for w in registry
            ],
            **observed,
        }


def crash_trial(base: Settings, work_dir: Path, copies: int) -> dict[str, Any]:
    with isolated_runtime(
        base, work_dir, job_lease_seconds=2, job_heartbeat_seconds=0.3, worker_offline_seconds=3
    ) as services:
        ids = corpus(services, 1, max(copies, 8))
        with WorkerGroup(services, 2) as group:
            submitted = services.jobs.submit(ids[0], "full", None)
            deadline = time.monotonic() + 30
            victim = None
            partial = 0
            while time.monotonic() < deadline:
                state = services.coordination.get_job(submitted.id)
                if state and state.status == "running" and state.indexed_files > 0:
                    worker = next(
                        w
                        for w in services.coordination.list_workers()
                        if w["worker_id"] == state.worker_id
                    )
                    assert any(worker["pid"] in process_tree_ids(p) for p in group.processes)
                    victim = psutil.Process(worker["pid"])
                    partial = state.indexed_files
                    break
                time.sleep(0.002)
            if victim is None:
                raise RuntimeError("workload finished before fault injection; increase --copies")
            killed_at = time.perf_counter()
            victim.kill()  # SIGKILL on POSIX, TerminateProcess on Windows; no graceful release.
            victim.wait(timeout=10)
            result = wait_jobs(services, [submitted], timeout=120)[0]
            recovery = time.perf_counter() - killed_at
            observed = audit(services)
            assert result.status == "completed" and result.lease_generation > 1
            assert result.skipped_files >= partial
            assert not observed["duplicate_active_claims"]
            manifest = services.database.file_manifest(ids[0])
            chunks = services.database.chunks(ids[0])
            assert all(file["vector_synced"] for file in manifest.values())
            assert len(chunks) == len({chunk["id"] for chunk in chunks})
            return {
                "crashed_workers": 1,
                "reclaimed_jobs": sum(row["reclaimed"] for row in observed["attempts"]),
                "successful_recoveries": 1,
                "failed_jobs": 0,
                "recovery_seconds": recovery,
                "files_persisted_before_kill": partial,
                "files_skipped_on_recovery": result.skipped_files,
                "final_files": len(manifest),
                "final_chunks": len(chunks),
                "status": result.status,
                **observed,
            }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Actual independent-worker indexing and abrupt-crash benchmarks"
    )
    parser.add_argument("--workers", nargs="+", type=int, default=[1, 2, 4, 8])
    parser.add_argument("--repositories", type=int, default=24)
    parser.add_argument("--copies", type=int, default=4)
    parser.add_argument("--crash-trials", type=int, default=3)
    parser.add_argument("--work-dir", type=Path, default=Path("work"))
    parser.add_argument("--output", type=Path, default=Path("benchmarks/distributed_results.json"))
    parser.add_argument("--vector-provider", choices=["memory", "qdrant"], default="memory")
    parser.add_argument("--embedding-provider", choices=["fake", "ollama"], default="fake")
    args = parser.parse_args()
    if (
        any(value < 1 for value in args.workers)
        or args.repositories < max(args.workers)
        or args.copies < 1
    ):
        parser.error("positive workers/copies and at least one repository per worker are required")
    base = Settings(
        vector_provider=args.vector_provider, embedding_provider=args.embedding_provider
    )
    root = Path(__file__).resolve().parents[1]
    source_hash = hashlib.sha256()
    for folder in (root / "src", root / "benchmarks"):
        for file in sorted(folder.rglob("*")):
            if file.suffix in {".py", ".sql"}:
                source_hash.update(file.relative_to(root).as_posix().encode())
                source_hash.update(file.read_bytes().replace(b"\r\n", b"\n"))
    payload: dict[str, Any] = {
        "kind": "distributed_real_indexing",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "source_code_sha256": source_hash.hexdigest(),
        "system": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "cpu": platform.processor(),
            "logical_cpus": psutil.cpu_count(),
            "ram_bytes": psutil.virtual_memory().total,
        },
        "workload": {
            "fixture": "sample_repository",
            "repositories": args.repositories,
            "copies_per_repo": args.copies,
        },
        "providers": {"embedding": args.embedding_provider, "vectors": args.vector_provider},
        "methodology": "Warm independent CLI processes; wall time includes durable submission through completion. Real Git discovery, chunking, embedding and SQLite writes; no simulated delays. Startup excluded. Single-host shared local SQLite volume.",
        "scaling": [],
        "crash_trials": [],
    }

    def save() -> None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    for count in args.workers:
        result = scaling(base, args.work_dir, count, args.repositories, args.copies)
        payload["scaling"].append(result)
        save()
        print(
            json.dumps(
                {
                    key: value
                    for key, value in result.items()
                    if key not in {"attempts", "worker_distribution"}
                }
            ),
            flush=True,
        )
    for trial in range(args.crash_trials):
        result = crash_trial(base, args.work_dir, args.copies)
        payload["crash_trials"].append(result)
        save()
        print(
            json.dumps(
                {
                    "trial": trial + 1,
                    **{key: value for key, value in result.items() if key != "attempts"},
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
