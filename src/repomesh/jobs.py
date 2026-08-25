from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from repomesh.db import Database
from repomesh.indexing import IndexCancelled, IndexStats, RepositoryIndexer
from repomesh.models import Job, utc_now

logger = logging.getLogger(__name__)


class JobManager:
    def __init__(
        self, database: Database, indexer: RepositoryIndexer, lease_seconds: int, max_retries: int
    ) -> None:
        self.database = database
        self.indexer = indexer
        self.lease_seconds = lease_seconds
        self.max_retries = max_retries
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="repomesh-job")
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.Lock()

    def submit(self, repository_id: str, mode: str, idempotency_key: str | None) -> Job:
        if idempotency_key:
            existing = self.database.job_by_key(idempotency_key)
            if existing:
                if existing.repository_id != repository_id or existing.mode != mode:
                    raise ValueError("idempotency key already belongs to a different request")
                return existing
        now = utc_now()
        job = Job(
            id=str(uuid.uuid4()),
            repository_id=repository_id,
            kind="index",
            mode=mode,
            status="queued",
            progress_done=0,
            progress_total=0,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        self.database.add_job(job)
        self._schedule(job.id)
        return job

    def _schedule(self, job_id: str) -> None:
        with self._lock:
            current = self._futures.get(job_id)
            if current and not current.done():
                return
            self._futures[job_id] = self.executor.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        job = self.database.job(job_id)
        if not job:
            return
        attempt = job.attempt
        while attempt < self.max_retries:
            attempt += 1
            now = datetime.now(UTC)
            self.database.update_job(
                job_id,
                status="running",
                attempt=attempt,
                heartbeat_at=now.isoformat(),
                lease_expires_at=(now + timedelta(seconds=self.lease_seconds)).isoformat(),
                error=None,
            )

            def is_cancelled() -> bool:
                state = self.database.job(job_id)
                return bool(state and state.cancel_requested)

            def progress(stats: IndexStats, path: str | None, error: str | None) -> None:
                stamp = datetime.now(UTC)
                done = (
                    stats.indexed_files
                    + stats.skipped_files
                    + stats.deleted_files
                    + stats.error_count
                )
                self.database.update_job(
                    job_id,
                    progress_done=done,
                    progress_total=stats.total_files,
                    indexed_files=stats.indexed_files,
                    indexed_chunks=stats.indexed_chunks,
                    deleted_files=stats.deleted_files,
                    skipped_files=stats.skipped_files,
                    error_count=stats.error_count,
                    heartbeat_at=stamp.isoformat(),
                    lease_expires_at=(stamp + timedelta(seconds=self.lease_seconds)).isoformat(),
                )
                if error and path:
                    self.database.execute(
                        "INSERT INTO job_file_errors(job_id,file_path,error,created_at) VALUES(?,?,?,?)",
                        (job_id, path, error[:2000], utc_now()),
                    )

            try:
                stats = self.indexer.index(job.repository_id, job.mode, progress, is_cancelled)
                status = "completed_with_errors" if stats.error_count else "completed"
                self.database.update_job(job_id, status=status, lease_expires_at=None)
                return
            except IndexCancelled:
                self.database.update_job(job_id, status="cancelled", lease_expires_at=None)
                return
            except Exception as exc:
                logger.exception("index job failed", extra={"job_id": job_id})
                if attempt >= self.max_retries:
                    self.database.update_job(
                        job_id, status="failed", error=str(exc), lease_expires_at=None
                    )
                    return
                self.database.update_job(job_id, status="retrying", error=str(exc))
                time.sleep(min(2 ** (attempt - 1), 8))

    def wait(self, job_id: str, timeout: float = 300) -> Job:
        future = self._futures.get(job_id)
        if future:
            future.result(timeout=timeout)
        job = self.database.job(job_id)
        if not job:
            raise ValueError("job not found")
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.database.job(job_id)
        if not job:
            raise ValueError("job not found")
        if job.status in {"completed", "completed_with_errors", "failed", "cancelled"}:
            return job
        self.database.update_job(job_id, cancel_requested=True)
        return self.database.job(job_id) or job

    def recover(self) -> int:
        jobs = self.database.recoverable_jobs()
        for job in jobs:
            self.database.update_job(job.id, status="queued", lease_expires_at=None)
            self._schedule(job.id)
        return len(jobs)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
