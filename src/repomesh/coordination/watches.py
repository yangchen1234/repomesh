from __future__ import annotations

import uuid
from typing import Any

from repomesh.coordination.repository import CoordinationRepository, serialize
from repomesh.models import Job


class WatchRepository:
    """Durable observations and job submission, committed together under a repository row lock."""

    def __init__(self, coordination: CoordinationRepository) -> None:
        self.coordination = coordination
        self.settings = coordination.settings

    def ensure(self, repository_id: str, enabled: bool | None = None) -> None:
        with self.coordination.pool.connection() as conn:
            conn.execute(
                "INSERT INTO repository_execution(repository_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (repository_id,),
            )
            conn.execute(
                """INSERT INTO repository_watches(repository_id,enabled) VALUES(%s,%s)
                ON CONFLICT DO NOTHING""",
                (repository_id, self.settings.watch_default_enabled if enabled is None else enabled),
            )

    def set_enabled(self, repository_id: str, enabled: bool) -> None:
        self.ensure(repository_id, enabled)
        with self.coordination.pool.connection() as conn:
            conn.execute(
                """UPDATE repository_watches SET enabled=%s,
                submitted_fingerprint=CASE WHEN %s AND NOT enabled THEN NULL ELSE submitted_fingerprint END,
                pending_since=CASE WHEN %s AND NOT enabled THEN clock_timestamp() ELSE pending_since END
                WHERE repository_id=%s""", (enabled, enabled, enabled, repository_id),
            )

    def list(self) -> list[dict[str, Any]]:
        with self.coordination.pool.connection() as conn:
            rows = conn.execute(
                """SELECT w.*, j.status AS job_status,
                last_success.completed_at AS last_indexed_at,
                w.last_checked_at IS NOT NULL AND w.last_checked_at > clock_timestamp()
                    - %s * interval '1 second' AS watcher_online
                FROM repository_watches w LEFT JOIN jobs j ON j.id=w.last_job_id
                LEFT JOIN LATERAL (SELECT completed_at FROM jobs
                    WHERE repository_id=w.repository_id AND status='completed'
                    ORDER BY completed_at DESC LIMIT 1) last_success ON true
                ORDER BY w.repository_id""",
                (max(30, self.settings.watch_poll_seconds * 3),),
            ).fetchall()
        for row in rows:
            if not row["enabled"]:
                state = "paused"
            elif row["error"]:
                state = "scan_error"
            elif not row["watcher_online"]:
                state = "watcher_unavailable"
            elif row["job_status"] in {"queued", "running", "retrying"}:
                state = row["job_status"]
            elif row["observed_fingerprint"] != row["submitted_fingerprint"]:
                state = "pending_changes"
            elif row["job_status"] in {"failed", "cancelled", "completed_with_errors"}:
                state = "needs_attention"
            else:
                state = "watching"
            row["state"] = state
            # Fingerprints are coordination internals, not user-visible content.
            row.pop("observed_fingerprint")
            row.pop("submitted_fingerprint")
        return [serialize(row) for row in rows]

    def record_error(self, repository_id: str, error: str) -> None:
        with self.coordination.pool.connection() as conn:
            conn.execute(
                """UPDATE repository_watches SET error=%s,last_checked_at=clock_timestamp()
                WHERE repository_id=%s AND enabled""", (error[:2000], repository_id),
            )

    def observe(
        self, repository_id: str, fingerprint: str, indexed_fingerprint: str | None = None
    ) -> Job | None:
        with self.coordination.pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM repository_watches WHERE repository_id=%s FOR UPDATE", (repository_id,)
            ).fetchone()
            if not row or not row["enabled"]:
                return None
            # Files can change and revert entirely between scans while a worker reads them.
            # After successful work, compare the persisted manifest as well as observations.
            # Failed/cancelled work keeps its bounded retry policy and needs manual attention.
            if indexed_fingerprint is not None and indexed_fingerprint != fingerprint:
                conn.execute(
                    """UPDATE repository_watches SET submitted_fingerprint=NULL
                    WHERE repository_id=%s AND submitted_fingerprint=%s
                    AND EXISTS(SELECT 1 FROM jobs WHERE id=repository_watches.last_job_id
                        AND status='completed')
                    AND NOT EXISTS(SELECT 1 FROM jobs WHERE repository_id=%s
                        AND status IN ('queued','running','retrying'))""",
                    (repository_id, fingerprint, repository_id),
                )
            conn.execute(
                """UPDATE repository_watches SET
                stable_since=CASE WHEN observed_fingerprint IS DISTINCT FROM %s
                    THEN clock_timestamp() ELSE stable_since END,
                pending_since=CASE WHEN submitted_fingerprint IS NOT DISTINCT FROM %s THEN NULL
                    ELSE COALESCE(pending_since,clock_timestamp()) END,
                observed_fingerprint=%s,last_checked_at=clock_timestamp(),error=NULL
                WHERE repository_id=%s""", (fingerprint, fingerprint, fingerprint, repository_id),
            )
            ready = conn.execute(
                """SELECT 1 FROM repository_watches WHERE repository_id=%s
                AND observed_fingerprint IS DISTINCT FROM submitted_fingerprint
                AND (stable_since <= clock_timestamp() - %s * interval '1 second'
                    OR pending_since <= clock_timestamp() - %s * interval '1 second')
                AND NOT EXISTS(SELECT 1 FROM jobs WHERE repository_id=%s
                    AND status IN ('queued','running','retrying'))""",
                (repository_id, self.settings.watch_debounce_seconds,
                 self.settings.watch_max_wait_seconds, repository_id),
            ).fetchone()
            if not ready:
                return None
            job_id = str(uuid.uuid4())
            job = conn.execute(
                """INSERT INTO jobs(id,repository_id,mode,trigger,max_attempts)
                VALUES(%s,%s,'incremental','watch',%s) RETURNING *""",
                (job_id, repository_id, self.settings.job_max_retries),
            ).fetchone()
            conn.execute(
                """UPDATE repository_watches SET submitted_fingerprint=%s,
                last_job_id=%s,pending_since=NULL WHERE repository_id=%s""",
                (fingerprint, job_id, repository_id),
            )
            assert job is not None
            return Job.model_validate(serialize(job))
