from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient
from qdrant_client.http.exceptions import UnexpectedResponse

from repomesh.api import create_app
from repomesh.config import Settings
from repomesh.coordination.metrics import CoordinationCollector
from repomesh.coordination.repository import CoordinationRepository
from repomesh.indexing import IndexStats
from repomesh.models import Job, utc_now
from repomesh.providers import QdrantVectorStore


def test_legacy_import_is_repeatable_and_recovers_incrementally(
    coordination: CoordinationRepository,
) -> None:
    now = utc_now()
    row = Job(
        id="legacy",
        repository_id="repo",
        kind="index",
        mode="full",
        status="running",
        progress_done=1,
        progress_total=3,
        attempt=1,
        created_at=now,
        updated_at=now,
    ).model_dump()
    assert coordination.import_legacy_jobs([row]) == 1
    assert coordination.import_legacy_jobs([row]) == 0
    session = coordination.register_worker("a", "test", 1, "test")
    claimed = coordination.claim_job("a", session)
    assert claimed and claimed.attempt == 2 and claimed.mode == "full"
    coordination.complete_job(claimed)


def test_metrics_include_durable_other_worker_events(coordination: CoordinationRepository) -> None:
    session = coordination.register_worker("a", "test", 1, "test")
    coordination.submit_job("repo", "full")
    job = coordination.claim_job("a", session)
    assert job
    coordination.update_progress(
        job, IndexStats(total_files=2, indexed_files=2), "a.py", "file error"
    )
    coordination.complete_job(job, with_errors=True)
    metrics = list(CoordinationCollector(coordination).collect())
    samples = {s.name: s.value for metric in metrics for s in metric.samples if not s.labels}
    assert samples["repomesh_jobs_claimed_total"] == 1
    assert samples["repomesh_jobs_completed_total"] == 1
    assert samples["repomesh_job_duration_seconds_count"] == 1
    assert samples["repomesh_queue_depth"] == 0
    assert all("job_id" not in s.labels for metric in metrics for s in metric.samples)


def test_api_coordination_outage_preserves_lexical_control(
    postgres: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", ollama_url="http://127.0.0.1:9"))
    with TestClient(app) as client:

        def unavailable(*_):
            raise psycopg.OperationalError("test unavailable")

        monkeypatch.setattr(app.state.services.coordination, "snapshot", unavailable)
        monkeypatch.setattr(app.state.services.coordination, "get_job", unavailable)
        health = client.get("/v1/health").json()
        assert health["postgres_status"] == "unavailable" and health["degraded_mode"]
        assert client.get("/v1/jobs/missing").status_code == 503
        assert client.get("/v1/repositories").status_code == 200


def test_collection_creation_race_is_safe() -> None:
    store = QdrantVectorStore("http://127.0.0.1:9", "race", 384)
    store.client.close()
    client = Mock()
    client.collection_exists.side_effect = [False, True]
    client.create_collection.side_effect = UnexpectedResponse(
        409, "conflict", b"already exists", {}
    )
    store.client = client
    store.ensure_collection()
    assert client.create_collection.call_count == 1


def test_repository_contention_with_many_jobs(coordination: CoordinationRepository) -> None:
    for index in range(120):
        coordination.submit_job(f"repo-{index % 3}", "full")

    def execute(index: int) -> int:
        name = f"a-{index}"
        session = coordination.register_worker(name, "test", index, "test")
        count = 0
        while (job := coordination.claim_job(name, session)) is not None:
            coordination.complete_job(job)
            count += 1
        return count

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(execute, range(8)))
    assert sum(counts) == 120
    with coordination.pool.connection() as conn:
        overlaps = conn.execute("""SELECT count(*) AS count FROM job_attempts a
            JOIN jobs ja ON ja.id=a.job_id JOIN job_attempts b ON a.claimed_at<b.claimed_at
            JOIN jobs jb ON jb.id=b.job_id WHERE ja.repository_id=jb.repository_id
            AND b.claimed_at<a.ended_at""").fetchone()
        assert overlaps and overlaps["count"] == 0
