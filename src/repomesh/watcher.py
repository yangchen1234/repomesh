from __future__ import annotations

import hashlib
import signal
import threading
from pathlib import Path

import psycopg
import structlog
from psycopg_pool import PoolTimeout

from repomesh.coordination.watches import WatchRepository
from repomesh.repositories import (
    current_commit,
    discover_files,
    file_sha256,
    validate_repository_path,
)
from repomesh.services import Services

logger = structlog.get_logger()


def repository_fingerprint(root: Path, max_bytes: int) -> str:
    """Match indexing's Git/secret/binary filters; content hashes also catch timestamp-preserving edits."""
    digest = hashlib.sha256()
    digest.update(current_commit(root).encode())
    for path in discover_files(root, max_bytes):
        digest.update(b"\0" + path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(file_sha256(path).encode())
    return digest.hexdigest()


class RepositoryWatcher:
    """Independent control process. It discovers changes and enqueues; workers alone index."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.watches = WatchRepository(services.coordination)
        self.stop = threading.Event()

    def run_once(self) -> int:
        count = 0
        # A session lock elects one scanner per schema, without an open transaction during I/O.
        # Submission is still atomic under each watch row. A crash releases this lock automatically.
        with self.services.coordination.pool.connection() as conn:
            conn.autocommit = True
            key = "repomesh-watch:" + self.services.settings.postgres_schema
            acquired = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", (key,)
            ).fetchone()
            try:
                if not acquired or not acquired["acquired"]:
                    return 0
                repositories = self.services.database.fetchall(
                    "SELECT id,root,commit_sha FROM repositories ORDER BY created_at"
                )
                for repository in repositories:
                    self.watches.ensure(repository["id"])
                enabled = {w["repository_id"] for w in self.watches.list() if w["enabled"]}
                for repository in repositories:
                    if self.stop.is_set():
                        break
                    repository_id = repository["id"]
                    if repository_id not in enabled:
                        continue
                    try:
                        root = validate_repository_path(
                            repository["root"], self.services.settings.resolved_roots()
                        )
                        fingerprint = repository_fingerprint(root, self.services.settings.max_file_bytes)
                        indexed = hashlib.sha256(repository["commit_sha"].encode())
                        for path, item in sorted(self.services.database.file_manifest(repository_id).items()):
                            indexed.update(b"\0" + path.encode() + b"\0")
                            indexed.update(item["sha256"].encode())
                        # Verify the elected session survived the scan before persisting it.
                        conn.execute("SELECT 1")
                        job = self.watches.observe(repository_id, fingerprint, indexed.hexdigest())
                        if job:
                            count += 1
                            logger.info("watch_job_submitted", repository_id=repository_id, job_id=job.id)
                    except (psycopg.Error, PoolTimeout):
                        raise
                    except Exception as exc:
                        self.watches.record_error(repository_id, str(exc))
                        logger.warning("watch_scan_failed", repository_id=repository_id, error=str(exc))
            finally:
                if not conn.closed:
                    if acquired and acquired["acquired"]:
                        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
                    conn.autocommit = False
        return count

    def run(self, install_signals: bool = True) -> None:
        if install_signals:
            signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        while not self.stop.is_set():
            try:
                self.run_once()
            except (psycopg.Error, PoolTimeout):
                logger.warning("watch_coordination_unavailable")
            self.stop.wait(self.services.settings.watch_poll_seconds)
