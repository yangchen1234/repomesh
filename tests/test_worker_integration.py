from __future__ import annotations

import multiprocessing
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from benchmarks.distributed_jobs import crash_trial
from repomesh.api import create_app
from repomesh.config import Settings
from repomesh.coordination import LeaseLost
from repomesh.indexing import IndexCancelled
from repomesh.providers import DeterministicEmbedder, ProviderUnavailable
from repomesh.repositories import register_repository
from repomesh.services import Services
from repomesh.worker import Worker


def blocked_worker(values: dict[str, Any], entered: Any, release: Any) -> None:
    services = Services.create(Settings(**values))

    class BlockedEmbedder(DeterministicEmbedder):
        def embed(self, texts: list[str]) -> list[list[float]]:
            entered.set()
            if not release.wait(15):
                raise RuntimeError("test embedding gate timed out")
            return super().embed(texts)

    services.indexer.embedder = BlockedEmbedder()
    worker = Worker(services)
    try:
        assert worker.run_once()
    finally:
        worker.close()
        services.close()


def test_running_cancellation_cross_process_and_heartbeat(
    postgres: Settings, tmp_path: Path, git_repository: Path
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        repository_roots=[tmp_path],
        job_lease_seconds=1,
        job_heartbeat_seconds=0.15,
        embedding_provider="fake",
        vector_provider="memory",
        generation_provider="fake",
    )
    app = create_app(settings)
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    with TestClient(app) as client:
        repo = client.post("/v1/repositories", json={"path": str(git_repository)}).json()
        submitted = client.post(
            f"/v1/repositories/{repo['id']}/index", json={"mode": "full"}
        ).json()
        process = context.Process(
            target=blocked_worker, args=(settings.model_dump(), entered, release)
        )
        process.start()
        try:
            assert entered.wait(15)
            before = client.get(f"/v1/jobs/{submitted['id']}").json()
            time.sleep(1.2)  # Exceeds the initial lease while embedding is still blocked.
            during = client.get(f"/v1/jobs/{submitted['id']}").json()
            assert during["heartbeat_at"] > before["heartbeat_at"]
            assert during["lease_generation"] == before["lease_generation"]
            assert during["status"] == "running"
            cancelled = client.post(f"/v1/jobs/{submitted['id']}/cancel").json()
            assert cancelled["cancel_requested"]
            release.set()
            process.join(15)
            assert process.exitcode == 0
            result = client.get(f"/v1/jobs/{submitted['id']}").json()
            assert result["status"] == "cancelled"
            assert app.state.services.database.chunks(repo["id"]) == []
            assert app.state.services.coordination.snapshot()["active_jobs"] == 0
        finally:
            release.set()
            if process.is_alive():
                process.kill()
            process.join(10)


def test_real_abrupt_worker_crash_recovers(postgres: Settings, tmp_path: Path) -> None:
    result = crash_trial(postgres, tmp_path, copies=8)
    assert result["successful_recoveries"] == 1
    assert result["reclaimed_jobs"] == 1
    assert result["files_skipped_on_recovery"] > 0
    assert result["duplicate_active_claims"] == 0


def test_control_plane_does_not_execute_or_recover_on_startup(
    postgres: Settings, tmp_path: Path, git_repository: Path
) -> None:
    settings = Settings(data_dir=tmp_path / "data", repository_roots=[tmp_path])
    app = create_app(settings)
    with TestClient(app) as client:
        repo = client.post("/v1/repositories", json={"path": str(git_repository)}).json()
        job = client.post(f"/v1/repositories/{repo['id']}/index", json={"mode": "full"}).json()
        waited = app.state.services.jobs.wait(job["id"], timeout=0.1)
        assert waited.status == "queued" and waited.attempt == 0
        assert not hasattr(app.state.services.jobs, "executor")
    restarted = create_app(settings)
    with TestClient(restarted) as client:
        current = client.get(f"/v1/jobs/{job['id']}").json()
        assert current["status"] == "queued" and current["attempt"] == 0
        assert client.get("/v1/workers").json() == []


@pytest.mark.parametrize("error_type", [LeaseLost, IndexCancelled])
def test_indexer_does_not_swallow_lost_authority(
    postgres: Settings, tmp_path: Path, git_repository: Path, error_type: type[Exception]
) -> None:
    services = Services.create(Settings(data_dir=tmp_path / "data", repository_roots=[tmp_path]))
    repo = register_repository(git_repository)
    services.database.add_repository(repo)

    def reject(_: list[str]) -> list[list[float]]:
        raise error_type("stop immediately")

    services.indexer.embedder.embed = reject
    try:
        with pytest.raises(error_type):
            services.indexer.index(repo.id, "full", lambda *_: None, lambda: False)
        assert services.database.chunks(repo.id) == []
    finally:
        services.close()


def test_vector_deletion_tombstone_survives_outage(
    postgres: Settings, tmp_path: Path, git_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = Services.create(Settings(data_dir=tmp_path / "data", repository_roots=[tmp_path]))
    repo = register_repository(git_repository)
    services.database.add_repository(repo)
    indexer = services.indexer
    try:
        indexer.index(repo.id, "full", lambda *_: None, lambda: False)
        (git_repository / "util.py").unlink()
        original = indexer.vector_store.delete_file

        def unavailable(*_: Any) -> None:
            raise ProviderUnavailable("offline")

        monkeypatch.setattr(indexer.vector_store, "delete_file", unavailable)
        indexer.index(repo.id, "incremental", lambda *_: None, lambda: False)
        assert "util.py" not in services.database.file_manifest(repo.id)
        assert services.database.pending_deletions(repo.id) == {"util.py"}
        monkeypatch.setattr(indexer.vector_store, "delete_file", original)
        indexer.index(repo.id, "incremental", lambda *_: None, lambda: False)
        assert services.database.pending_deletions(repo.id) == set()
    finally:
        services.close()


def test_interrupted_vector_replace_leaves_manifest_dirty(
    postgres: Settings, tmp_path: Path, git_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = Services.create(Settings(data_dir=tmp_path / "data", repository_roots=[tmp_path]))
    repo = register_repository(git_repository)
    services.database.add_repository(repo)
    try:
        services.indexer.index(repo.id, "full", lambda *_: None, lambda: False)

        def crash(*_: Any) -> None:
            raise LeaseLost("crash after vector deletion")

        monkeypatch.setattr(services.vector_store, "upsert", crash)
        with pytest.raises(LeaseLost):
            services.indexer.index(repo.id, "full", lambda *_: None, lambda: False)
        manifest = services.database.file_manifest(repo.id)
        assert any(not file["vector_synced"] for file in manifest.values())
    finally:
        services.close()
