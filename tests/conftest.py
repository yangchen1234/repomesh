from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from repomesh.config import Settings
from repomesh.coordination.repository import CoordinationRepository


@pytest.fixture
def postgres(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    dsn = os.environ.get("REPOMESH_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("real PostgreSQL required: set REPOMESH_TEST_POSTGRES_DSN")
    schema = "test_" + uuid.uuid4().hex
    monkeypatch.setenv("REPOMESH_POSTGRES_DSN", dsn)
    monkeypatch.setenv("REPOMESH_POSTGRES_SCHEMA", schema)
    settings = Settings(_env_file=None)
    store = CoordinationRepository(settings)
    try:
        store.migrate()
        yield settings
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def coordination(postgres: Settings) -> Iterator[CoordinationRepository]:
    store = CoordinationRepository(postgres)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def launch_worker():
    processes = []

    def launch(settings: Settings):
        env = os.environ.copy()
        for key, value in settings.model_dump(mode="json").items():
            if value is not None:
                env[f"REPOMESH_{key.upper()}"] = (
                    value if isinstance(value, str) else json.dumps(value)
                )
        process = subprocess.Popen(
            [sys.executable, "-m", "repomesh.cli", "worker"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    yield launch
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / "app.py").write_text(
        "from util import greet\n\nclass Greeter:\n    def hello(self, name: str) -> str:\n        return greet(name)\n",
        encoding="utf-8",
    )
    (root / "util.py").write_text(
        "def greet(name: str) -> str:\n    return f'Hello {name}'\n", encoding="utf-8"
    )
    (root / "notes.md").write_text("Greeter architecture and welcome flow.\n", encoding="utf-8")
    (root / "ignored.py").write_text("SECRET = 'ignored'\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=do-not-index\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@repomesh.local"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "RepoMesh Tests"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "sample repository"], cwd=root, check=True, capture_output=True
    )
    return root
