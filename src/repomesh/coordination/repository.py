from __future__ import annotations

import random
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from repomesh.config import Settings
from repomesh.coordination import LeaseLost, WorkerIdentityInUse
from repomesh.models import Job

TERMINAL = {"completed", "completed_with_errors", "failed", "cancelled"}
OWNED = """id=%s AND worker_id=%s AND lease_generation=%s
    AND status='running' AND lease_expires_at > clock_timestamp()"""
RowConnection = Connection[dict[str, Any]]


def serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


def ownership(job: Job) -> tuple[str, str | None, int]:
    return job.id, job.worker_id, job.lease_generation


class CoordinationRepository:
    """All live job authority lives in PostgreSQL, never in the SQLite index."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool: ConnectionPool[RowConnection] = ConnectionPool(
            settings.postgres_dsn,
            min_size=0,
            max_size=6,
            timeout=5,
            open=True,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 3,
                "options": f"-c search_path={settings.postgres_schema} -c statement_timeout=10000",
            },
        )

    def migrate(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"repomesh-migrate:{self.settings.postgres_schema}",),
            )
            conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self.settings.postgres_schema)
                )
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )""")
            for path in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
                if not conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=%s", (path.name,)
                ).fetchone():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations(version) VALUES(%s)", (path.name,))

    def close(self) -> None:
        self.pool.close()

    def submit_job(
        self, repository_id: str, mode: str, key: str | None = None, priority: int = 0
    ) -> Job:
        if mode not in {"full", "incremental"} or (key is not None and not key):
            raise ValueError("invalid job mode or empty idempotency key")
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO repository_execution(repository_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (repository_id,),
            )
            row = conn.execute(
                """INSERT INTO jobs(id,repository_id,mode,idempotency_key,priority,max_attempts)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(idempotency_key) DO NOTHING RETURNING *""",
                (
                    str(uuid.uuid4()),
                    repository_id,
                    mode,
                    key,
                    priority,
                    self.settings.job_max_retries,
                ),
            ).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM jobs WHERE idempotency_key=%s", (key,)).fetchone()
                assert row is not None
                if (row["repository_id"], row["mode"], row["priority"]) != (
                    repository_id,
                    mode,
                    priority,
                ):
                    raise ValueError("idempotency key already belongs to a different request")
            return Job.model_validate(serialize(row))

    def get_job(self, job_id: str) -> Job | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
            return Job.model_validate(serialize(row)) if row else None

    def latest_job(self, repository_id: str) -> Job | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE repository_id=%s ORDER BY created_at DESC LIMIT 1",
                (repository_id,),
            ).fetchone()
            return Job.model_validate(serialize(row)) if row else None

    def register_worker(self, worker_id: str, hostname: str, pid: int, version: str) -> str:
        session = str(uuid.uuid4())
        with self.pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO workers(worker_id,session_id,hostname,pid,version,status)
                VALUES(%s,%s,%s,%s,%s,'idle') ON CONFLICT(worker_id) DO UPDATE SET
                session_id=excluded.session_id, hostname=excluded.hostname, pid=excluded.pid,
                version=excluded.version, status='idle', started_at=clock_timestamp(),
                last_seen_at=clock_timestamp(), current_job_id=NULL
                WHERE workers.status='stopped' OR
                  workers.last_seen_at < clock_timestamp() - %s * interval '1 second'
                RETURNING worker_id""",
                (worker_id, session, hostname, pid, version, self.settings.worker_offline_seconds),
            ).fetchone()
            if row is None:
                raise WorkerIdentityInUse(f"worker identity is already active: {worker_id}")
        return session

    def heartbeat_worker(
        self, worker_id: str, session: str, current_job: str | None = None
    ) -> None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """UPDATE workers SET last_seen_at=clock_timestamp(),
                status=%s,current_job_id=%s WHERE worker_id=%s AND session_id=%s AND status!='stopped'
                RETURNING worker_id""",
                ("busy" if current_job else "idle", current_job, worker_id, session),
            ).fetchone()
            if row is None:
                raise LeaseLost("worker registry session lost")

    def stop_worker(self, worker_id: str, session: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """UPDATE workers SET status='stopped',current_job_id=NULL,
                last_seen_at=clock_timestamp() WHERE worker_id=%s AND session_id=%s""",
                (worker_id, session),
            )

    def list_workers(self) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """SELECT worker_id,hostname,pid,version,started_at,last_seen_at,
                current_job_id,completed_jobs,failed_jobs,
                CASE WHEN status!='stopped' AND last_seen_at < clock_timestamp() - %s * interval '1 second'
                     THEN 'offline' ELSE status END AS status FROM workers ORDER BY started_at""",
                (self.settings.worker_offline_seconds,),
            ).fetchall()
            return [serialize(row) for row in rows]

    def claim_job(self, worker_id: str, session: str) -> Job | None:
        # Cleanup of exhausted/cancelled expired jobs is bounded; the next poll continues it.
        with self.pool.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM workers WHERE worker_id=%s AND session_id=%s AND status!='stopped'",
                (worker_id, session),
            ).fetchone():
                raise LeaseLost("worker registry session lost")
            for _ in range(32):
                row = conn.execute("""SELECT j.* FROM jobs j
                    JOIN repository_execution r ON r.repository_id=j.repository_id
                    WHERE (r.active_job_id IS NULL OR r.active_job_id=j.id) AND (
                        (j.status IN ('queued','retrying') AND j.available_at <= clock_timestamp()) OR
                        (j.status='running' AND j.lease_expires_at <= clock_timestamp()))
                    ORDER BY j.priority DESC,j.available_at,j.created_at,j.id
                    LIMIT 1 FOR UPDATE OF j,r SKIP LOCKED""").fetchone()
                if row is None:
                    return None
                reclaimed = row["status"] == "running"
                if reclaimed:
                    conn.execute(
                        """UPDATE job_attempts SET ended_at=clock_timestamp(),outcome='expired'
                        WHERE job_id=%s AND lease_generation=%s AND ended_at IS NULL""",
                        (row["id"], row["lease_generation"]),
                    )
                if row["cancel_requested"] or row["attempt"] >= row["max_attempts"]:
                    terminal = "cancelled" if row["cancel_requested"] else "failed"
                    conn.execute(
                        """UPDATE jobs SET status=%s,worker_id=NULL,lease_expires_at=NULL,
                        completed_at=clock_timestamp(),updated_at=clock_timestamp(),error=%s WHERE id=%s""",
                        (
                            terminal,
                            "attempt budget exhausted after lease expiry"
                            if terminal == "failed"
                            else None,
                            row["id"],
                        ),
                    )
                    conn.execute(
                        "UPDATE repository_execution SET active_job_id=NULL WHERE repository_id=%s",
                        (row["repository_id"],),
                    )
                    if reclaimed:
                        conn.execute(
                            "UPDATE job_attempts SET outcome=%s WHERE job_id=%s AND lease_generation=%s",
                            (terminal, row["id"], row["lease_generation"]),
                        )
                        conn.execute(
                            """UPDATE workers SET failed_jobs=failed_jobs+%s,current_job_id=NULL,status='idle'
                            WHERE worker_id=%s AND current_job_id=%s""",
                            (int(terminal == "failed"), row["worker_id"], row["id"]),
                        )
                    continue
                claimed = conn.execute(
                    """UPDATE jobs SET status='running',worker_id=%s,
                    lease_generation=lease_generation+1,attempt=attempt+1,
                    heartbeat_at=clock_timestamp(), lease_expires_at=clock_timestamp() + %s * interval '1 second',
                    started_at=COALESCE(started_at,clock_timestamp()),updated_at=clock_timestamp(),
                    progress_done=0,progress_total=0,indexed_files=0,indexed_chunks=0,
                    deleted_files=0,skipped_files=0,error_count=0,error=NULL
                    WHERE id=%s RETURNING *""",
                    (worker_id, self.settings.job_lease_seconds, row["id"]),
                ).fetchone()
                assert claimed is not None
                conn.execute(
                    "UPDATE repository_execution SET active_job_id=%s WHERE repository_id=%s",
                    (row["id"], row["repository_id"]),
                )
                conn.execute(
                    """INSERT INTO job_attempts(job_id,lease_generation,worker_id,attempt,lease_expires_at,reclaimed)
                    VALUES(%s,%s,%s,%s,%s,%s)""",
                    (
                        claimed["id"],
                        claimed["lease_generation"],
                        worker_id,
                        claimed["attempt"],
                        claimed["lease_expires_at"],
                        reclaimed,
                    ),
                )
                updated = conn.execute(
                    """UPDATE workers SET status='busy',current_job_id=%s,last_seen_at=clock_timestamp()
                    WHERE worker_id=%s AND session_id=%s RETURNING worker_id""",
                    (row["id"], worker_id, session),
                ).fetchone()
                if updated is None:
                    raise LeaseLost("worker registry session lost during claim")
                return Job.model_validate(serialize(claimed))
        return None

    def _owned(self, conn: RowConnection, job: Job) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM jobs WHERE " + OWNED, ownership(job)).fetchone()
        if row is None:
            raise LeaseLost(f"lease lost: {job.id} generation {job.lease_generation}")
        return row

    def assert_ownership(self, job: Job) -> bool:
        with self.pool.connection() as conn:
            return bool(self._owned(conn, job)["cancel_requested"])

    def was_reclaimed(self, job: Job) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT reclaimed FROM job_attempts WHERE job_id=%s AND lease_generation=%s",
                (job.id, job.lease_generation),
            ).fetchone()
            return bool(row and row["reclaimed"])

    def renew_lease(self, job: Job) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                """UPDATE jobs SET heartbeat_at=clock_timestamp(),updated_at=clock_timestamp(),
                lease_expires_at=clock_timestamp() + %s * interval '1 second' WHERE """
                + OWNED
                + " RETURNING lease_expires_at,cancel_requested",
                (self.settings.job_lease_seconds, *ownership(job)),
            ).fetchone()
            if row is None:
                raise LeaseLost(f"lease renewal rejected: {job.id}")
            conn.execute(
                "UPDATE job_attempts SET lease_expires_at=%s WHERE job_id=%s AND lease_generation=%s",
                (row["lease_expires_at"], job.id, job.lease_generation),
            )
            return bool(row["cancel_requested"])

    def update_progress(
        self, job: Job, stats: Any, path: str | None = None, error: str | None = None
    ) -> None:
        done = stats.indexed_files + stats.skipped_files + stats.deleted_files + stats.error_count
        with self.pool.connection() as conn:
            row = conn.execute(
                """UPDATE jobs SET progress_done=%s,progress_total=%s,indexed_files=%s,
                indexed_chunks=%s,deleted_files=%s,skipped_files=%s,error_count=%s,updated_at=clock_timestamp()
                WHERE """
                + OWNED
                + " RETURNING id",
                (
                    done,
                    stats.total_files,
                    stats.indexed_files,
                    stats.indexed_chunks,
                    stats.deleted_files,
                    stats.skipped_files,
                    stats.error_count,
                    *ownership(job),
                ),
            ).fetchone()
            if row is None:
                raise LeaseLost(f"progress rejected: {job.id}")
            if error and path:
                conn.execute(
                    "INSERT INTO job_file_errors(job_id,lease_generation,file_path,error) VALUES(%s,%s,%s,%s)",
                    (job.id, job.lease_generation, path, error[:2000]),
                )

    @contextmanager
    def mutation_guard(self, job: Job) -> Iterator[Callable[[], bool]]:
        # Embedding/parsing are outside this transaction. Claims skip this row until commit.
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT active_job_id FROM repository_execution WHERE repository_id=%s FOR UPDATE",
                (job.repository_id,),
            ).fetchone()
            if row is None or row["active_job_id"] != job.id:
                raise LeaseLost("repository execution ownership lost")

            def check() -> bool:
                return bool(self._owned(conn, job)["cancel_requested"])

            check()
            yield check

    def request_cancel(self, job_id: str) -> Job:
        with self.pool.connection() as conn:
            row = conn.execute(
                """UPDATE jobs SET cancel_requested=true,updated_at=clock_timestamp(),
                status=CASE WHEN status IN ('queued','retrying') THEN 'cancelled' ELSE status END,
                completed_at=CASE WHEN status IN ('queued','retrying') THEN clock_timestamp() ELSE completed_at END
                WHERE id=%s AND status NOT IN ('completed','completed_with_errors','failed','cancelled') RETURNING *""",
                (job_id,),
            ).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
            if row is None:
                raise ValueError("job not found")
            return Job.model_validate(serialize(row))

    def _finish(self, job: Job, status: str, error: str | None = None, delay: float = 0) -> Job:
        with self.pool.connection() as conn:
            row = conn.execute(
                """UPDATE jobs SET
                status=CASE WHEN cancel_requested THEN 'cancelled' ELSE %s END,
                worker_id=NULL,lease_expires_at=NULL,updated_at=clock_timestamp(),
                completed_at=CASE WHEN %s='retrying' AND NOT cancel_requested THEN NULL ELSE clock_timestamp() END,
                available_at=clock_timestamp() + %s * interval '1 second', error=%s
                WHERE """
                + OWNED
                + " RETURNING *",
                (status, status, delay, error, *ownership(job)),
            ).fetchone()
            if row is None:
                raise LeaseLost(f"terminal update rejected: {job.id}")
            conn.execute(
                "UPDATE repository_execution SET active_job_id=NULL WHERE repository_id=%s AND active_job_id=%s",
                (job.repository_id, job.id),
            )
            conn.execute(
                """UPDATE job_attempts SET ended_at=clock_timestamp(),outcome=%s
                WHERE job_id=%s AND lease_generation=%s""",
                (row["status"], job.id, job.lease_generation),
            )
            conn.execute(
                """UPDATE workers SET completed_jobs=completed_jobs+%s,failed_jobs=failed_jobs+%s,
                status='idle',current_job_id=NULL WHERE worker_id=%s AND current_job_id=%s""",
                (
                    int(row["status"] in {"completed", "completed_with_errors"}),
                    int(row["status"] == "failed"),
                    job.worker_id,
                    job.id,
                ),
            )
            return Job.model_validate(serialize(row))

    def complete_job(self, job: Job, with_errors: bool = False) -> Job:
        return self._finish(job, "completed_with_errors" if with_errors else "completed")

    def cancel_job(self, job: Job) -> Job:
        return self._finish(job, "cancelled")

    def fail_job(self, job: Job, error: str) -> Job:
        return self._finish(job, "failed", error[:2000])

    def schedule_retry(self, job: Job, error: str) -> Job:
        if job.attempt >= job.max_attempts:
            return self.fail_job(job, error)
        base = self.settings.job_retry_base_seconds
        delay = min(
            base * 2 ** min(job.attempt - 1, 20) + random.uniform(0, base),
            self.settings.job_retry_max_seconds,
        )
        return self._finish(job, "retrying", error[:2000], delay)

    def snapshot(self) -> dict[str, Any]:
        with self.pool.connection() as conn:
            jobs = conn.execute("""SELECT
                count(*) FILTER (WHERE status IN ('queued','retrying')) AS queue_depth,
                count(*) FILTER (WHERE status='running') AS active_jobs,
                count(*) FILTER (WHERE status='running' AND lease_expires_at > clock_timestamp()) AS active_leases
                FROM jobs""").fetchone()
            workers = conn.execute(
                """SELECT count(*) AS active_workers FROM workers
                WHERE status!='stopped' AND last_seen_at > clock_timestamp() - %s * interval '1 second'""",
                (self.settings.worker_offline_seconds,),
            ).fetchone()
            attempts = conn.execute("""SELECT count(*) AS claimed,
                count(*) FILTER (WHERE outcome IN ('completed','completed_with_errors')) AS completed,
                count(*) FILTER (WHERE outcome='failed') AS failed,
                count(*) FILTER (WHERE outcome='retrying') AS retried,
                count(*) FILTER (WHERE reclaimed) AS reclaimed
                FROM job_attempts""").fetchone()
            durations = conn.execute("""WITH durations AS (
                SELECT extract(epoch FROM ended_at-claimed_at)::float8 AS seconds
                FROM job_attempts WHERE ended_at IS NOT NULL)
                SELECT count(*) AS count, COALESCE(sum(seconds),0) AS sum,
                count(*) FILTER (WHERE seconds<=1) AS b1,
                count(*) FILTER (WHERE seconds<=5) AS b5,
                count(*) FILTER (WHERE seconds<=10) AS b10,
                count(*) FILTER (WHERE seconds<=30) AS b30,
                count(*) FILTER (WHERE seconds<=60) AS b60,
                count(*) FILTER (WHERE seconds<=300) AS b300,
                count(*) FILTER (WHERE seconds<=900) AS b900 FROM durations""").fetchone()
            return {**(jobs or {}), **(workers or {}), **(attempts or {}), "durations": durations}

    def import_legacy_jobs(self, rows: list[dict[str, Any]]) -> int:
        """Explicit, restart-safe import after stopping the pre-distributed API."""
        count = 0
        with self.pool.connection() as conn:
            for old in rows:
                conn.execute(
                    "INSERT INTO repository_execution(repository_id) VALUES(%s) ON CONFLICT DO NOTHING",
                    (old["repository_id"],),
                )
                terminal = old["status"] in TERMINAL
                row = conn.execute(
                    """INSERT INTO jobs(id,repository_id,kind,mode,status,attempt,max_attempts,
                    idempotency_key,created_at,updated_at,cancel_requested,error,indexed_files,indexed_chunks,
                    deleted_files,skipped_files,error_count,progress_done,progress_total,completed_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING RETURNING id""",
                    (
                        old["id"],
                        old["repository_id"],
                        old["kind"],
                        old["mode"],
                        old["status"]
                        if terminal
                        else "cancelled"
                        if old["cancel_requested"]
                        else "queued",
                        old["attempt"] if terminal else max(1, old["attempt"]),
                        max(self.settings.job_max_retries, old["attempt"] + 1, 2),
                        old["idempotency_key"],
                        old["created_at"],
                        old["updated_at"],
                        bool(old["cancel_requested"]),
                        old["error"],
                        old["indexed_files"],
                        old["indexed_chunks"],
                        old["deleted_files"],
                        old["skipped_files"],
                        old["error_count"],
                        old["progress_done"],
                        old["progress_total"],
                        old["updated_at"] if terminal or old["cancel_requested"] else None,
                    ),
                ).fetchone()
                count += int(row is not None)
        return count
