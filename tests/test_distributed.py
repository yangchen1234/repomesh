from __future__ import annotations

import json
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from repomesh.config import Settings
from repomesh.coordination import LeaseLost, WorkerIdentityInUse
from repomesh.coordination.repository import CoordinationRepository, serialize
from repomesh.indexing import IndexStats


def register(store: CoordinationRepository, name: str) -> str:
    return store.register_worker(name, "test", os.getpid(), "test")


def claim_process(values: dict[str, Any], index: int) -> list[tuple[str, int, int]]:
    store = CoordinationRepository(Settings(**values))
    name = f"competitor-{index}"
    session = register(store, name)
    claims = []
    try:
        while (job := store.claim_job(name, session)) is not None:
            claims.append((job.id, job.lease_generation, os.getpid()))
            store.complete_job(job)
        return claims
    finally:
        store.stop_worker(name, session)
        store.close()


def test_exclusive_claims_1000_jobs_eight_processes(coordination: CoordinationRepository) -> None:
    jobs = [coordination.submit_job(f"repo-{index}", "full") for index in range(1000)]
    values = coordination.settings.model_dump()
    with ProcessPoolExecutor(
        max_workers=8, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        results = list(pool.map(claim_process, [values] * 8, range(8)))
    claims = [claim for batch in results for claim in batch]
    assert len(claims) == 1000
    assert {claim[0] for claim in claims} == {job.id for job in jobs}
    assert len({claim[0] for claim in claims}) == len(claims)
    assert len({claim[2] for claim in claims}) > 1
    assert coordination.snapshot()["completed"] == 1000
    if output := os.environ.get("REPOMESH_CLAIM_AUDIT_PATH"):
        with coordination.pool.connection() as conn:
            attempts = conn.execute("SELECT * FROM job_attempts ORDER BY claimed_at").fetchall()
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "kind": "exclusive_claim_correctness",
                    "jobs": 1000,
                    "processes": 8,
                    "duplicate_active_claims": len(claims) - len({claim[0] for claim in claims}),
                    "completed": coordination.snapshot()["completed"],
                    "attempts": [serialize(row) for row in attempts],
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def expire(store: CoordinationRepository, job_id: str) -> None:
    # Controlled clock fixture; separate tests/benchmarks wait for real expiry after kill.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (job_id,),
        )


def test_crash_reclaim_and_all_stale_updates_fenced(coordination: CoordinationRepository) -> None:
    a, b = register(coordination, "a"), register(coordination, "b")
    submitted = coordination.submit_job("repo", "full")
    old = coordination.claim_job("a", a)
    assert old is not None
    expire(coordination, submitted.id)
    new = coordination.claim_job("b", b)
    assert new and new.worker_id == "b" and new.lease_generation == old.lease_generation + 1
    for operation in (
        lambda: coordination.renew_lease(old),
        lambda: coordination.update_progress(old, IndexStats()),
        lambda: coordination.complete_job(old),
        lambda: coordination.fail_job(old, "stale"),
        lambda: coordination.schedule_retry(old, "stale"),
        lambda: coordination.cancel_job(old),
        lambda: coordination.assert_ownership(old),
    ):
        with pytest.raises(LeaseLost):
            operation()
    with pytest.raises(LeaseLost), coordination.mutation_guard(old):
        pytest.fail("stale worker entered a repository mutation")
    assert coordination.get_job(new.id).worker_id == "b"
    assert coordination.complete_job(new).status == "completed"


def test_expired_lease_cannot_be_resurrected(coordination: CoordinationRepository) -> None:
    session = register(coordination, "a")
    coordination.submit_job("repo", "full")
    job = coordination.claim_job("a", session)
    assert job
    expire(coordination, job.id)
    with pytest.raises(LeaseLost):
        coordination.renew_lease(job)
    with pytest.raises(LeaseLost):
        coordination.complete_job(job)


def test_concurrent_idempotency(coordination: CoordinationRepository) -> None:
    with ThreadPoolExecutor(max_workers=20) as pool:
        jobs = list(
            pool.map(lambda _: coordination.submit_job("same-repo", "full", "same-key"), range(100))
        )
    assert len({job.id for job in jobs}) == 1
    with pytest.raises(ValueError, match="different request"):
        coordination.submit_job("other", "full", "same-key")
    with pytest.raises(ValueError, match="different request"):
        coordination.submit_job("same-repo", "incremental", "same-key")
    assert coordination.snapshot()["queue_depth"] == 1


def test_repository_exclusion_and_different_repos_parallel(
    coordination: CoordinationRepository,
) -> None:
    a, b = register(coordination, "a"), register(coordination, "b")
    first = coordination.submit_job("repo-a", "full", priority=10)
    second = coordination.submit_job("repo-a", "incremental", priority=9)
    third = coordination.submit_job("repo-b", "full")
    owned_a = coordination.claim_job("a", a)
    owned_b = coordination.claim_job("b", b)
    assert owned_a and owned_b and owned_a.id == first.id and owned_b.id == third.id
    assert coordination.get_job(second.id).status == "queued"
    coordination.complete_job(owned_a)
    assert coordination.claim_job("a", a).id == second.id


def test_mutation_guard_blocks_takeover_but_not_other_repository(
    coordination: CoordinationRepository,
) -> None:
    a, b = register(coordination, "a"), register(coordination, "b")
    coordination.submit_job("repo-a", "full", priority=10)
    owned = coordination.claim_job("a", a)
    assert owned
    with coordination.mutation_guard(owned):
        expire(coordination, owned.id)
        coordination.submit_job("repo-b", "full")
        other = coordination.claim_job("b", b)
        assert other and other.repository_id == "repo-b"
        coordination.complete_job(other)
        assert coordination.claim_job("b", b) is None
    recovered = coordination.claim_job("b", b)
    assert recovered and recovered.id == owned.id


def test_durable_retry_and_attempt_budget(coordination: CoordinationRepository) -> None:
    coordination.settings.job_retry_base_seconds = 0.05
    coordination.settings.job_retry_max_seconds = 0.1
    a, b = register(coordination, "a"), register(coordination, "b")
    coordination.submit_job("repo", "full")
    job = coordination.claim_job("a", a)
    assert job
    retry = coordination.schedule_retry(job, "transient")
    assert retry.status == "retrying" and retry.worker_id is None and retry.lease_expires_at is None
    assert datetime.fromisoformat(retry.available_at) > datetime.now(UTC)
    assert coordination.claim_job("b", b) is None
    time.sleep(0.12)
    next_job = coordination.claim_job("b", b)
    assert next_job and next_job.attempt == job.attempt + 1
    with coordination.pool.connection() as conn:
        conn.execute("UPDATE jobs SET max_attempts=attempt WHERE id=%s", (job.id,))
    next_job.max_attempts = next_job.attempt
    assert coordination.schedule_retry(next_job, "again").status == "failed"
    assert coordination.claim_job("a", a) is None


def test_cancellation_in_every_runnable_state(coordination: CoordinationRepository) -> None:
    session = register(coordination, "a")
    queued = coordination.submit_job("queued", "full")
    assert coordination.request_cancel(queued.id).status == "cancelled"
    coordination.submit_job("running", "full")
    job = coordination.claim_job("a", session)
    assert job
    assert coordination.request_cancel(job.id).cancel_requested
    assert coordination.renew_lease(job)
    # Completion racing cancellation must resolve to cancellation.
    assert coordination.complete_job(job).status == "cancelled"
    coordination.submit_job("retry", "full")
    retry_job = coordination.claim_job("a", session)
    assert retry_job
    coordination.schedule_retry(retry_job, "retry")
    assert coordination.request_cancel(retry_job.id).status == "cancelled"
    assert coordination.claim_job("a", session) is None


def test_expired_cancel_and_exhausted_jobs_release_repository(
    coordination: CoordinationRepository,
) -> None:
    session = register(coordination, "a")
    for cancel in (True, False):
        coordination.submit_job("repo", "full")
        job = coordination.claim_job("a", session)
        assert job
        if cancel:
            coordination.request_cancel(job.id)
        else:
            with coordination.pool.connection() as conn:
                conn.execute("UPDATE jobs SET max_attempts=attempt WHERE id=%s", (job.id,))
        expire(coordination, job.id)
        assert coordination.claim_job("a", session) is None
        assert coordination.get_job(job.id).status == ("cancelled" if cancel else "failed")


def test_worker_registry_session_fences_duplicate_identity(
    coordination: CoordinationRepository,
) -> None:
    old = register(coordination, "same")
    with pytest.raises(WorkerIdentityInUse):
        register(coordination, "same")
    with coordination.pool.connection() as conn:
        conn.execute("UPDATE workers SET last_seen_at=clock_timestamp()-interval '1 hour'")
    assert coordination.list_workers()[0]["status"] == "offline"
    new = register(coordination, "same")
    assert old != new
    with pytest.raises(LeaseLost):
        coordination.heartbeat_worker("same", old)
    with pytest.raises(LeaseLost):
        coordination.claim_job("same", old)
    coordination.stop_worker("same", old)
    assert coordination.list_workers()[0]["status"] == "idle"


def test_migrations_safe_under_concurrent_startup(postgres: Settings) -> None:
    def migrate(_: int) -> None:
        store = CoordinationRepository(postgres)
        try:
            store.migrate()
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(migrate, range(16)))
