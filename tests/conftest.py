from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


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
