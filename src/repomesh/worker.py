from __future__ import annotations

import os
import signal
import socket
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import structlog
from psycopg_pool import PoolTimeout

from repomesh import __version__
from repomesh.coordination import LeaseLost
from repomesh.indexing import IndexCancelled, IndexStats
from repomesh.models import Job
from repomesh.repositories import validate_repository_path
from repomesh.services import Services

logger = structlog.get_logger()
COORDINATION_ERRORS = (psycopg.Error, PoolTimeout)


class Worker:
    """One execution slot in an independent process; heartbeat is the only helper thread."""

    def __init__(self, services: Services, worker_id: str | None = None) -> None:
        self.services = services
        self.store = services.coordination
        self.settings = services.settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
        self.session: str | None = None
        self.stop = threading.Event()

    def register(self) -> None:
        self.session = self.store.register_worker(
            self.worker_id, socket.gethostname(), os.getpid(), __version__
        )
        logger.info("worker_started", worker_id=self.worker_id)

    def run_once(self) -> bool:
        if self.session is None:
            self.register()
        assert self.session is not None
        self.store.heartbeat_worker(self.worker_id, self.session)
        job = self.store.claim_job(self.worker_id, self.session)
        if job is None:
            return False
        self.execute(job)
        return True

    def execute(self, job: Job) -> None:
        log = logger.bind(
            job_id=job.id,
            repository_id=job.repository_id,
            worker_id=self.worker_id,
            attempt=job.attempt,
            lease_generation=job.lease_generation,
        )
        log.info("job_claimed")
        heartbeat_stop = threading.Event()
        lost = threading.Event()
        cancel = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(self.settings.job_heartbeat_seconds):
                try:
                    if self.store.renew_lease(job):
                        cancel.set()
                    assert self.session is not None
                    self.store.heartbeat_worker(self.worker_id, self.session, job.id)
                    log.info("lease_renewed")
                except (LeaseLost, *COORDINATION_ERRORS):
                    lost.set()
                    log.warning("lease_lost", source="heartbeat")
                    return

        def check() -> bool:
            if lost.is_set():
                raise LeaseLost("heartbeat lost ownership")
            try:
                return cancel.is_set() or self.store.assert_ownership(job)
            except COORDINATION_ERRORS as exc:
                lost.set()
                raise LeaseLost("PostgreSQL unavailable; stopping authoritative work") from exc

        @contextmanager
        def mutation() -> Iterator[Any]:
            if check():
                raise IndexCancelled()
            try:
                with self.store.mutation_guard(job) as guarded_check:

                    def boundary() -> None:
                        if lost.is_set():
                            raise LeaseLost("heartbeat lost ownership")
                        if guarded_check():
                            raise IndexCancelled()

                    boundary()
                    yield boundary
            except COORDINATION_ERRORS as exc:
                lost.set()
                raise LeaseLost("PostgreSQL mutation guard lost") from exc

        def progress(stats: IndexStats, path: str | None, error: str | None) -> None:
            check()
            try:
                self.store.update_progress(job, stats, path, error)
            except COORDINATION_ERRORS as exc:
                lost.set()
                raise LeaseLost("PostgreSQL progress unavailable") from exc
            log.debug("job_progress", path=path, indexed_files=stats.indexed_files, error=error)

        pulse = threading.Thread(target=heartbeat, name="repomesh-heartbeat", daemon=True)
        pulse.start()
        try:
            if self.store.was_reclaimed(job):
                log.info("job_reclaimed")
            log.info("job_started")
            repository = self.services.database.repository(job.repository_id)
            if repository is None:
                raise ValueError("repository not found in shared SQLite index")
            validate_repository_path(repository.root, self.settings.resolved_roots())
            stats = self.services.indexer.index(
                job.repository_id,
                job.mode if job.attempt == 1 else "incremental",
                progress,
                check,
                mutation=mutation,
            )
            check()
            result = self.store.complete_job(job, bool(stats.error_count))
            log.info(
                "job_cancelled" if result.status == "cancelled" else "job_completed",
                status=result.status,
            )
        except LeaseLost:
            log.warning("lease_lost", source="execution")
        except IndexCancelled:
            try:
                self.store.cancel_job(job)
                log.info("job_cancelled")
            except (LeaseLost, *COORDINATION_ERRORS):
                log.warning("lease_lost", source="cancellation")
        except Exception as exc:
            log.exception("job_execution_error")
            try:
                if lost.is_set():
                    raise LeaseLost("heartbeat lost ownership")
                result = self.store.schedule_retry(job, str(exc))
                log.info(
                    "job_retry_scheduled"
                    if result.status == "retrying"
                    else f"job_{result.status}",
                    available_at=result.available_at,
                )
            except (LeaseLost, *COORDINATION_ERRORS):
                log.warning("lease_lost", source="failure")
        finally:
            heartbeat_stop.set()
            pulse.join()

    def run(self, install_signals: bool = True) -> None:
        if install_signals:
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        try:
            self.register()
            while not self.stop.is_set():
                try:
                    if not self.run_once():
                        self.stop.wait(
                            min(self.settings.job_poll_seconds, self.settings.job_heartbeat_seconds)
                        )
                except COORDINATION_ERRORS:
                    logger.warning("coordination_unavailable", worker_id=self.worker_id)
                    self.stop.wait(self.settings.job_poll_seconds)
                except LeaseLost:
                    logger.error("worker_session_lost", worker_id=self.worker_id)
                    return
        finally:
            self.close()

    def close(self) -> None:
        if self.session:
            try:
                self.store.stop_worker(self.worker_id, self.session)
            except COORDINATION_ERRORS:
                pass
            self.session = None
            logger.info("worker_stopped", worker_id=self.worker_id)
