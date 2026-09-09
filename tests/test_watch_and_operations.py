from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from repomesh.api import create_app
from repomesh.config import Settings
from repomesh.coordination.watches import WatchRepository
from repomesh.indexing import IndexStats
from repomesh.repositories import register_repository
from repomesh.services import Services
from repomesh.watcher import RepositoryWatcher, repository_fingerprint
from repomesh.worker import Worker


@pytest.fixture
def watched(postgres: Settings, git_repository: Path, tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data", repository_roots=[git_repository],
        embedding_provider="fake", generation_provider="fake", vector_provider="memory",
        watch_poll_seconds=0.1, watch_debounce_seconds=0, job_poll_seconds=0.05,
        ollama_url="http://127.0.0.1:9",
    )
    services = Services.create(settings)
    repo = register_repository(git_repository)
    services.database.add_repository(repo)
    try:
        yield services, repo.id, git_repository
    finally:
        services.close()


def test_fingerprint_matches_filters_and_detects_content_and_head(git_repository: Path):
    before = repository_fingerprint(git_repository, 1_000_000)
    for name in (".env", "ignored.py", "bundle.min.js"):
        (git_repository / name).write_text("changed secret or generated content")
    assert repository_fingerprint(git_repository, 1_000_000) == before
    path = git_repository / "app.py"
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b"hello", b"world"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    changed = repository_fingerprint(git_repository, 1_000_000)
    assert changed != before
    subprocess.run(["git", "commit", "--allow-empty", "-m", "new revision"],
                   cwd=git_repository, check=True, capture_output=True)
    assert repository_fingerprint(git_repository, 1_000_000) != changed


def test_watcher_indexes_add_modify_delete_and_coalesces_during_job(watched):
    services, repo, root = watched
    watcher = RepositoryWatcher(services)
    worker = Worker(services)
    try:
        assert watcher.run_once() == 1
        first = services.coordination.latest_job(repo)
        assert first and first.trigger == "watch"
        # Repeated scans don't accumulate jobs while the first one is queued.
        (root / "new.py").write_text("def new_feature(): return 17\n")
        (root / "util.py").unlink()
        assert watcher.run_once() == 0
        assert watcher.run_once() == 0
        assert worker.run_once()
        assert watcher.run_once() == 1  # Exactly one follow-up for edits since submission.
        assert worker.run_once()
        assert watcher.run_once() == 0
        manifest = services.database.file_manifest(repo)
        assert "new.py" in manifest and "util.py" not in manifest
        assert services.coordination.list_jobs(repo)["total"] == 2
        assert watcher.watches.list()[0]["last_indexed_at"]
    finally:
        worker.close()


def test_watch_debounce_persists_across_restart_and_max_wait(watched):
    services, repo, _ = watched
    services.settings.watch_debounce_seconds = 10
    watch = WatchRepository(services.coordination)
    watch.ensure(repo)
    assert watch.observe(repo, "first") is None
    # A restarted watcher sees the durable debounce timestamp.
    watch = WatchRepository(services.coordination)
    with services.coordination.pool.connection() as conn:
        conn.execute("UPDATE repository_watches SET stable_since=clock_timestamp()-interval '11 seconds'")
    job = watch.observe(repo, "first")
    assert job
    services.coordination.request_cancel(job.id)
    assert watch.observe(repo, "first") is None  # Cancellation isn't undone by a rescan.
    assert watch.observe(repo, "second") is None
    with services.coordination.pool.connection() as conn:
        conn.execute("UPDATE repository_watches SET pending_since=clock_timestamp()-interval '31 seconds'")
    assert watch.observe(repo, "third")  # Continuous edits cannot postpone forever.


def test_watch_atomic_observation_and_failure_does_not_loop(watched):
    services, repo, _ = watched
    watch = WatchRepository(services.coordination)
    watch.ensure(repo)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: watch.observe(repo, "same"), range(20)))
    assert sum(job is not None for job in results) == 1
    store = services.coordination
    session = store.register_worker("test-worker", "host", 1, "test")
    job = store.claim_job("test-worker", session)
    assert job
    store.fail_job(job, "provider unavailable")
    assert watch.observe(repo, "same") is None
    assert watch.list()[0]["state"] == "needs_attention"
    successor = store.resubmit_job(job.id)
    assert watch.list()[0]["last_job_id"] == successor.id


def test_watch_pause_enable_and_scan_failure_isolation(watched):
    services, repo, root = watched
    watcher = RepositoryWatcher(services)
    watcher.watches.set_enabled(repo, False)
    assert watcher.run_once() == 0
    watcher.watches.set_enabled(repo, True)
    assert watcher.run_once() == 1
    job = services.coordination.latest_job(repo)
    assert job
    services.coordination.request_cancel(job.id)
    services.settings.repository_roots = [root / "outside"]
    assert watcher.run_once() == 0
    state = watcher.watches.list()[0]
    assert state["state"] == "scan_error" and "outside" in state["error"]
    services.settings.repository_roots = [root]
    assert watcher.run_once() == 0
    assert watcher.watches.list()[0]["error"] is None


def test_only_one_watcher_scans_and_lock_releases(watched):
    services, _, _ = watched
    watcher = RepositoryWatcher(services)
    with services.coordination.pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                     ("repomesh-watch:" + services.settings.postgres_schema,))
        assert watcher.run_once() == 0
    assert watcher.run_once() == 1
    assert watcher.run_once() == 0


def test_operations_history_filters_auth_and_idempotent_retry(watched):
    services, repo, _ = watched
    store = services.coordination
    failed = store.submit_job(repo, "full")
    session = store.register_worker("operations-test", "host", 1, "test")
    owned = store.claim_job("operations-test", session)
    assert owned
    store.update_progress(owned, IndexStats(total_files=1, error_count=1), "bad.py", "parse failed")
    store.fail_job(owned, "embedding offline")
    services.settings.api_token = "test-operations-token"
    with TestClient(create_app(services.settings)) as client:
        for path in ("/v1/jobs", "/v1/watches", "/v1/workers", f"/v1/jobs/{failed.id}/history"):
            assert client.get(path).status_code == 401
        assert client.post(f"/v1/jobs/{failed.id}/retry").status_code == 401
        assert client.put(f"/v1/repositories/{repo}/watch", json={"enabled": False}).status_code == 401
        client.headers["Authorization"] = "Bearer test-operations-token"
        history = client.get(f"/v1/jobs/{failed.id}/history").json()
        assert history["attempts"][0]["error"] == "embedding offline"
        assert history["file_errors"][0]["file_path"] == "bad.py"
        assert history["file_error_total"] == 1
        assert client.get("/v1/jobs?status=failed&limit=1").json()["total"] == 1
        assert client.get("/v1/jobs?status=active").json()["total"] == 0
        for query in ("limit=0", "limit=101", "offset=-1", "status=invalid"):
            assert client.get("/v1/jobs?" + query).status_code == 422
        assert client.get("/v1/jobs/missing/history").status_code == 404
        assert client.post("/v1/jobs/missing/retry").status_code == 404
        successor = client.post(f"/v1/jobs/{failed.id}/retry").json()
        assert successor["parent_job_id"] == failed.id and successor["attempt"] == 0
        assert client.post(f"/v1/jobs/{failed.id}/retry").json()["id"] == successor["id"]
        assert client.post(f"/v1/jobs/{successor['id']}/retry").status_code == 409
        assert client.get(f"/v1/jobs?repository_id={repo}&offset=1&limit=1").json()["jobs"][0]["id"] == failed.id
        assert client.get("/v1/jobs?repository_id=missing").json()["total"] == 0
        assert client.put(f"/v1/repositories/{repo}/watch", json={"enabled": False}).json()["state"] == "paused"
        assert client.put("/v1/repositories/missing/watch", json={"enabled": True}).status_code == 404
        assert client.post(f"/v1/jobs/{successor['id']}/cancel").json()["status"] == "cancelled"


def test_retry_click_race_creates_one_successor(watched):
    services, repo, _ = watched
    store = services.coordination
    old = store.submit_job(repo, "full")
    store.request_cancel(old.id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        retries = list(pool.map(lambda _: store.resubmit_job(old.id), range(16)))
    assert len({job.id for job in retries}) == 1
    assert store.list_jobs(repo)["total"] == 2


def test_separate_watcher_and_worker_processes_update_live_files(watched):
    services, repo, root = watched
    env = os.environ.copy()
    for key, value in services.settings.model_dump(mode="json").items():
        if value is not None:
            env["REPOMESH_" + key.upper()] = value if isinstance(value, str) else json.dumps(value)
    processes = []

    def wait_for(predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        pytest.fail("separate watcher/worker did not converge")

    try:
        for command in ("watch", "worker"):
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "repomesh.cli", command], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
        wait_for(lambda: bool(services.database.file_manifest(repo)))
        (root / "new.py").write_text("def automatic_feature(): return 'searchable'\n")
        (root / "util.py").unlink()
        wait_for(lambda: "new.py" in services.database.file_manifest(repo)
                 and "util.py" not in services.database.file_manifest(repo))
        wait_for(lambda: not services.coordination.list_jobs(repo, "active")["jobs"])
        assert services.coordination.list_jobs(repo, "completed")["total"] >= 2
    finally:
        for process in processes:
            if process.poll() is None:
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
                psutil.wait_procs(children, timeout=5)
            process.wait(timeout=5)
