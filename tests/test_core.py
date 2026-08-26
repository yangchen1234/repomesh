from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from repomesh.api import create_app
from repomesh.chunking import CodeChunker
from repomesh.config import Settings
from repomesh.db import Database
from repomesh.indexing import IndexStats
from repomesh.jobs import JobManager
from repomesh.models import Job, SearchMode, utc_now
from repomesh.repositories import (
    RepositoryValidationError,
    discover_files,
    register_repository,
    validate_repository_path,
)
from repomesh.retrieval import reciprocal_rank_fusion
from repomesh.services import Services


def settings_for(tmp_path: Path, root: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "repository_roots": [root],
        "api_token": None,
        "embedding_provider": "fake",
        "generation_provider": "fake",
        "vector_provider": "memory",
        "ollama_url": "http://127.0.0.1:9",
    }
    values.update(overrides)
    return Settings(**values)


def indexed_services(tmp_path: Path, root: Path) -> tuple[Services, str]:
    services = Services.create(settings_for(tmp_path, root))
    repository = register_repository(root)
    services.database.add_repository(repository)
    job = services.jobs.submit(repository.id, "full", "initial")
    completed = services.jobs.wait(job.id)
    assert completed.status == "completed"
    return services, repository.id


def test_repository_path_accepts_allowlisted_git_repo(git_repository: Path) -> None:
    assert validate_repository_path(str(git_repository), [git_repository.parent]) == git_repository


def test_repository_path_rejects_outside_allowlist(git_repository: Path, tmp_path: Path) -> None:
    with pytest.raises(RepositoryValidationError, match="outside"):
        validate_repository_path(str(git_repository), [tmp_path / "different"])


def test_repository_path_rejects_non_git_directory(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RepositoryValidationError, match="Git"):
        validate_repository_path(str(plain), [tmp_path])


def test_discovery_respects_gitignore_and_secret_rules(git_repository: Path) -> None:
    paths = {path.name for path in discover_files(git_repository, 1_000_000)}
    assert "ignored.py" not in paths
    assert ".env" not in paths
    assert {"app.py", "util.py"} <= paths


@pytest.mark.parametrize(
    ("path", "source", "symbol"),
    [
        ("a.py", "def alpha():\n    return 1\n", "alpha"),
        ("A.java", "class Alpha { int value() { return 1; } }", "Alpha"),
        ("a.js", "function alpha() { return 1; }", "alpha"),
        ("a.ts", "interface Alpha { value: number }", "Alpha"),
        ("a.cpp", "int alpha() { return 1; }", "alpha"),
    ],
)
def test_tree_sitter_symbol_boundaries(path: str, source: str, symbol: str) -> None:
    chunks = CodeChunker().chunk("repo", "commit", path, source, "fake", "1")
    assert any(chunk.symbol_name == symbol and chunk.start_line == 1 for chunk in chunks)
    assert all(chunk.chunking_strategy.startswith("tree-sitter") for chunk in chunks)


def test_fallback_chunking_marks_strategy() -> None:
    chunks = CodeChunker(fallback_lines=2, overlap=1).chunk(
        "repo", "commit", "notes.md", "one\ntwo\nthree\n", "fake", "1"
    )
    assert len(chunks) == 2
    assert all(chunk.chunking_strategy == "line-fallback" for chunk in chunks)
    assert chunks[0].start_line == 1 and chunks[0].end_line == 2


def test_chunk_ids_are_deterministic() -> None:
    chunker = CodeChunker()
    args = ("repo", "commit", "a.py", "def alpha():\n    return 1\n", "fake", "1")
    assert chunker.chunk(*args)[0].id == chunker.chunk(*args)[0].id


def test_full_indexing_creates_manifest_and_chunks(tmp_path: Path, git_repository: Path) -> None:
    services, repository_id = indexed_services(tmp_path, git_repository)
    try:
        repository = services.database.repository(repository_id)
        assert repository and repository.indexed_files == 4
        assert repository.indexed_chunks >= 4
    finally:
        services.close()


def test_incremental_add_modify_delete(tmp_path: Path, git_repository: Path) -> None:
    services, repository_id = indexed_services(tmp_path, git_repository)
    try:
        (git_repository / "app.py").write_text(
            "def changed():\n    return 'modified'\n", encoding="utf-8"
        )
        (git_repository / "new.py").write_text("def added():\n    return 'new'\n", encoding="utf-8")
        (git_repository / "util.py").unlink()
        job = services.jobs.submit(repository_id, "incremental", "changes")
        completed = services.jobs.wait(job.id)
        assert completed.indexed_files == 2
        assert completed.deleted_files == 1
        assert completed.skipped_files == 2
        manifest = services.database.file_manifest(repository_id)
        assert "new.py" in manifest and "util.py" not in manifest
    finally:
        services.close()


def test_repeated_index_has_no_duplicate_chunks(tmp_path: Path, git_repository: Path) -> None:
    services, repository_id = indexed_services(tmp_path, git_repository)
    try:
        before = len(services.database.chunks(repository_id))
        job = services.jobs.submit(repository_id, "full", "second-full")
        services.jobs.wait(job.id)
        chunks = services.database.chunks(repository_id)
        assert len(chunks) == before
        assert len({chunk["id"] for chunk in chunks}) == len(chunks)
    finally:
        services.close()


def test_unsynced_vector_file_is_retried_incrementally(
    tmp_path: Path, git_repository: Path
) -> None:
    services, repository_id = indexed_services(tmp_path, git_repository)
    try:
        services.database.execute(
            "UPDATE files SET vector_synced=0 WHERE repository_id=? AND path='util.py'",
            (repository_id,),
        )
        job = services.jobs.submit(repository_id, "incremental", "vector-resync")
        completed = services.jobs.wait(job.id)
        assert completed.indexed_files == 1
        assert completed.skipped_files == 3
        assert services.database.file_manifest(repository_id)["util.py"]["vector_synced"] == 1
    finally:
        services.close()


def test_lexical_search_returns_exact_source_lines(tmp_path: Path, git_repository: Path) -> None:
    services, repository_id = indexed_services(tmp_path, git_repository)
    try:
        response = services.retriever.search(repository_id, "greet", SearchMode.lexical, 5, 10, 10)
        assert response.results
        assert any(
            result.file_path == "util.py" and result.start_line == 1 for result in response.results
        )
        assert all(result.retrieval_method == "lexical" for result in response.results)
    finally:
        services.close()


def test_dense_search_with_deterministic_provider(tmp_path: Path, git_repository: Path) -> None:
    services, repository_id = indexed_services(tmp_path, git_repository)
    try:
        response = services.retriever.search(
            repository_id, "welcome greet", SearchMode.dense, 3, 10, 10
        )
        assert response.results and response.results[0].vector_score is not None
    finally:
        services.close()


def test_rrf_fusion_adds_reciprocal_ranks() -> None:
    scores = reciprocal_rank_fusion([[("a", 9), ("b", 8)], [("b", 1), ("a", 0)]], k=60)
    assert scores["a"] == pytest.approx(scores["b"])
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)


def test_answer_contains_only_real_citations(tmp_path: Path, git_repository: Path) -> None:
    app = create_app(settings_for(tmp_path, git_repository.parent))
    with TestClient(app) as client:
        repository = client.post("/v1/repositories", json={"path": str(git_repository)}).json()
        indexed = client.post(
            f"/v1/repositories/{repository['id']}/index", json={"mode": "full", "wait": True}
        )
        assert indexed.status_code == 202
        response = client.post(
            "/v1/answer",
            json={"repository_id": repository["id"], "query": "where is greet implemented?"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["citations"]
        for citation in payload["citations"]:
            label = f"[{citation['file_path']}:{citation['start_line']}-{citation['end_line']}]"
            assert label in payload["answer"] or citation != payload["citations"][0]


def test_api_authentication(tmp_path: Path, git_repository: Path) -> None:
    app = create_app(settings_for(tmp_path, git_repository.parent, api_token="correct-token"))
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/repositories").status_code == 401
        assert (
            client.get("/v1/repositories", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        assert (
            client.get("/v1/repositories", headers={"X-API-Token": "correct-token"}).status_code
            == 200
        )


def test_job_idempotency_key_returns_same_job(tmp_path: Path, git_repository: Path) -> None:
    services, repository_id = indexed_services(tmp_path, git_repository)
    try:
        first = services.jobs.submit(repository_id, "incremental", "same-key")
        second = services.jobs.submit(repository_id, "incremental", "same-key")
        assert first.id == second.id
    finally:
        services.close()


class SlowIndexer:
    def index(self, repository_id: str, mode: str, progress: Any, cancelled: Any) -> IndexStats:
        stats = IndexStats(total_files=100)
        for index in range(100):
            if cancelled():
                from repomesh.indexing import IndexCancelled

                raise IndexCancelled()
            stats.skipped_files = index + 1
            progress(stats, f"{index}.py", None)
            time.sleep(0.005)
        return stats


def test_job_cancellation(tmp_path: Path, git_repository: Path) -> None:
    database = Database(tmp_path / "cancel.sqlite3")
    repository = register_repository(git_repository)
    database.add_repository(repository)
    manager = JobManager(database, cast(Any, SlowIndexer()), 30, 1)
    try:
        job = manager.submit(repository.id, "full", None)
        time.sleep(0.03)
        manager.cancel(job.id)
        completed = manager.wait(job.id)
        assert completed.status == "cancelled"
    finally:
        manager.shutdown()
        database.close()


class ImmediateIndexer:
    def __init__(self) -> None:
        self.modes: list[str] = []

    def index(self, repository_id: str, mode: str, progress: Any, cancelled: Any) -> IndexStats:
        self.modes.append(mode)
        stats = IndexStats(total_files=1, skipped_files=1)
        progress(stats, "already.py", None)
        return stats


def test_restart_recovery_resumes_running_job(tmp_path: Path, git_repository: Path) -> None:
    database = Database(tmp_path / "recovery.sqlite3")
    repository = register_repository(git_repository)
    database.add_repository(repository)
    now = utc_now()
    job = Job(
        id="recover-me",
        repository_id=repository.id,
        kind="index",
        mode="full",
        status="running",
        progress_done=0,
        progress_total=1,
        attempt=1,
        created_at=now,
        updated_at=now,
    )
    database.add_job(job)
    indexer = ImmediateIndexer()
    manager = JobManager(database, cast(Any, indexer), 30, 2)
    try:
        assert manager.recover() == 1
        recovered = manager.wait(job.id)
        assert recovered.status == "completed"
        assert recovered.mode == "full"
        assert indexer.modes == ["incremental"]
    finally:
        manager.shutdown()
        database.close()


def test_ollama_offline_degrades_vector_but_not_lexical(
    tmp_path: Path, git_repository: Path
) -> None:
    seed, repository_id = indexed_services(tmp_path, git_repository)
    seed.close()
    offline = settings_for(
        tmp_path,
        git_repository.parent,
        embedding_provider="ollama",
        generation_provider="ollama",
    )
    app = create_app(offline)
    with TestClient(app) as client:
        health = client.get("/v1/health").json()
        assert health["degraded_mode"] is True and health["ollama_status"] == "unavailable"
        assert (
            client.post(
                "/v1/search",
                json={"repository_id": repository_id, "query": "greet", "mode": "lexical"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/search",
                json={"repository_id": repository_id, "query": "greet", "mode": "dense"},
            ).status_code
            == 503
        )
        assert (
            client.post(
                "/v1/answer", json={"repository_id": repository_id, "query": "greet"}
            ).status_code
            == 503
        )


def test_qdrant_offline_does_not_break_lexical_api(tmp_path: Path, git_repository: Path) -> None:
    seed, repository_id = indexed_services(tmp_path, git_repository)
    seed.close()
    app = create_app(
        settings_for(
            tmp_path,
            git_repository.parent,
            vector_provider="qdrant",
            qdrant_url="http://127.0.0.1:9",
        )
    )
    with TestClient(app) as client:
        health = client.get("/v1/health").json()
        assert health["degraded_mode"] is True and health["qdrant_status"] == "unavailable"
        assert (
            client.post(
                "/v1/search",
                json={"repository_id": repository_id, "query": "greet", "mode": "lexical"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/search",
                json={"repository_id": repository_id, "query": "greet", "mode": "dense"},
            ).status_code
            == 503
        )


def test_api_openapi_and_metrics_are_available(tmp_path: Path, git_repository: Path) -> None:
    app = create_app(settings_for(tmp_path, git_repository.parent))
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "repomesh_http_requests_total" in metrics.text
