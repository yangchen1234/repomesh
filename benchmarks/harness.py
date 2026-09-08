from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil
import psycopg
from psycopg import sql

from repomesh.config import Settings
from repomesh.coordination.repository import TERMINAL, serialize
from repomesh.models import Job
from repomesh.repositories import register_repository
from repomesh.services import Services


class WorkerGroup:
    """Launch the actual CLI in separate OS processes, with independent connections."""

    def __init__(self, services: Services, count: int = 1, log_dir: Path | None = None) -> None:
        self.services = services
        self.count = count
        self.log_dir = log_dir or services.settings.resolved_data_dir()
        self.processes: list[subprocess.Popen[bytes]] = []
        self.logs: list[Any] = []

    def __enter__(self) -> WorkerGroup:
        env = os.environ.copy()
        for key, value in self.services.settings.model_dump(mode="json").items():
            if value is not None:
                env[f"REPOMESH_{key.upper()}"] = (
                    value if isinstance(value, str) else json.dumps(value)
                )
        try:
            for index in range(self.count):
                stream = (self.log_dir / f"worker-{uuid.uuid4().hex[:8]}-{index}.log").open("wb")
                self.logs.append(stream)
                self.processes.append(
                    subprocess.Popen(
                        [sys.executable, "-m", "repomesh.cli", "worker"],
                        env=env,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                    )
                )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                seen = {
                    w["pid"]
                    for w in self.services.coordination.list_workers()
                    if w["status"] in {"idle", "busy"}
                }
                # Windows venv redirectors can have a different PID from the interpreter.
                if all(process_tree_ids(process) & seen for process in self.processes):
                    return self
                if any(process.poll() is not None for process in self.processes):
                    raise RuntimeError("worker startup failed; inspect worker logs")
                time.sleep(0.05)
            raise TimeoutError("workers did not register within 60 seconds")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for process in self.processes:
            stop_process(process)
        for stream in self.logs:
            stream.close()

    def __exit__(self, *args: Any) -> None:
        self.close()


def process_tree_ids(process: subprocess.Popen[bytes]) -> set[int]:
    try:
        return {
            process.pid,
            *(child.pid for child in psutil.Process(process.pid).children(recursive=True)),
        }
    except psutil.NoSuchProcess:
        return {process.pid}


def stop_process(process: subprocess.Popen[bytes]) -> None:
    for pid in process_tree_ids(process):
        try:
            psutil.Process(pid).terminate()
        except psutil.NoSuchProcess:
            pass
    try:
        process.wait(timeout=10)

    except subprocess.TimeoutExpired:
        for pid in process_tree_ids(process):
            try:
                psutil.Process(pid).kill()
            except psutil.NoSuchProcess:
                pass
        process.wait(timeout=10)


def drop_benchmark_schema(settings: Settings) -> None:
    if not settings.postgres_schema.startswith("bench_"):
        raise ValueError("refusing to drop a schema outside the benchmark namespace")
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(settings.postgres_schema)
            )
        )


@contextmanager
def isolated_runtime(base: Settings, work_dir: Path, **overrides: Any) -> Iterator[Services]:
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="distributed-", dir=work_dir)
    root = Path(temporary.name).resolve()
    assert root.is_relative_to(work_dir)
    schema = "bench_" + uuid.uuid4().hex
    values = base.model_dump()
    values.update(
        data_dir=root / "data",
        repository_roots=[root],
        postgres_schema=schema,
        job_poll_seconds=0.02,
        **overrides,
    )
    services = Services.create(Settings(**values))
    try:
        yield services
    finally:
        services.close()
        drop_benchmark_schema(services.settings)
        temporary.cleanup()


def corpus(services: Services, count: int, copies: int) -> list[str]:
    fixture = Path(__file__).resolve().parents[1] / "sample_repository"
    root = services.settings.resolved_roots()[0]
    repository_ids = []
    for index in range(count):
        destination = root / f"repository-{index}"
        destination.mkdir()
        for copy in range(copies):
            shutil.copytree(fixture, destination / f"fixture-{copy}")
        subprocess.run(["git", "init", "-q", "-b", "main", str(destination)], check=True)
        subprocess.run(["git", "-C", str(destination), "add", "."], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(destination),
                "-c",
                "user.name=RepoMesh Benchmark",
                "-c",
                "user.email=benchmark@repomesh.local",
                "commit",
                "-q",
                "-m",
                "fixture",
            ],
            check=True,
        )
        repository = register_repository(destination)
        services.database.add_repository(repository)
        repository_ids.append(repository.id)
    return repository_ids


def wait_jobs(services: Services, jobs: list[Job], timeout: float = 600) -> list[Job]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        results = [services.coordination.get_job(job.id) for job in jobs]
        if all(job is not None and job.status in TERMINAL for job in results):
            return [job for job in results if job is not None]
        time.sleep(0.05)
    raise TimeoutError(f"{len(jobs)} jobs did not finish within {timeout} seconds")


def audit(services: Services) -> dict[str, Any]:
    with services.coordination.pool.connection() as conn:
        rows = conn.execute("""SELECT a.*, j.repository_id FROM job_attempts a
            JOIN jobs j ON j.id=a.job_id ORDER BY a.claimed_at""").fetchall()
    duplicates = 0
    repo_overlaps = 0
    for field in ("job_id", "repository_id"):
        ends: dict[str, Any] = {}
        for row in rows:
            end = min(row["lease_expires_at"], row["ended_at"] or row["lease_expires_at"])
            previous_end = ends.get(row[field])
            if previous_end is not None and row["claimed_at"] < previous_end:
                if field == "job_id":
                    duplicates += 1
                else:
                    repo_overlaps += 1
            ends[row[field]] = max(previous_end, end) if previous_end else end
    return {
        "duplicate_active_claims": duplicates,
        "repository_lease_overlaps": repo_overlaps,
        "attempts": [serialize(row) for row in rows],
    }
