from __future__ import annotations

import hashlib
import subprocess
import uuid
from pathlib import Path

from repomesh.models import Repository, utc_now


class RepositoryValidationError(ValueError):
    pass


DEFAULT_IGNORED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "build",
    "dist",
    "target",
    "coverage",
    ".next",
    "vendor",
    "__pycache__",
}
SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
    ".npmrc",
    ".pypirc",
}
BINARY_EXTENSIONS = {
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".class",
    ".jar",
    ".pyc",
    ".lock",
}
GENERATED_SUFFIXES = {".min.js", ".min.css", ".map", ".generated.py", ".g.cs"}


def validate_repository_path(path_text: str, allow_roots: list[Path]) -> Path:
    path = Path(path_text).expanduser().resolve()
    if not path.is_dir():
        raise RepositoryValidationError("repository path does not exist or is not a directory")
    if not any(path == root or path.is_relative_to(root) for root in allow_roots):
        raise RepositoryValidationError("repository path is outside REPOMESH_REPOSITORY_ROOTS")
    try:
        output = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={path.as_posix()}",
                "-C",
                str(path),
                "rev-parse",
                "--show-toplevel",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        raise RepositoryValidationError("path is not a readable Git repository") from exc
    if Path(output).resolve() != path:
        raise RepositoryValidationError("register the Git repository root, not a subdirectory")
    return path


def current_commit(path: Path) -> str:
    try:
        return subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={path.as_posix()}",
                "-C",
                str(path),
                "rev-parse",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except subprocess.SubprocessError:
        return "UNCOMMITTED"


def register_repository(path: Path, name: str | None = None) -> Repository:
    now = utc_now()
    normalized = str(path).lower().replace("\\", "/")
    return Repository(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, normalized)),
        name=name or path.name,
        root=str(path),
        commit_sha=current_commit(path),
        status="registered",
        created_at=now,
        updated_at=now,
    )


def discover_files(path: Path, max_bytes: int) -> list[Path]:
    try:
        output = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={path.as_posix()}",
                "-C",
                str(path),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
        relative_paths = [
            Path(part.decode("utf-8", errors="replace")) for part in output.split(b"\0") if part
        ]
    except subprocess.SubprocessError as exc:
        raise RepositoryValidationError(f"git file discovery failed: {exc}") from exc
    files = []
    for relative in relative_paths:
        candidate = (path / relative).resolve()
        if not candidate.is_file() or not candidate.is_relative_to(path):
            continue
        lowered_parts = {part.lower() for part in relative.parts}
        lowered_name = relative.name.lower()
        if lowered_parts & DEFAULT_IGNORED_PARTS or lowered_name in SECRET_NAMES:
            continue
        if relative.suffix.lower() in BINARY_EXTENSIONS:
            continue
        if any(lowered_name.endswith(suffix) for suffix in GENERATED_SUFFIXES):
            continue
        try:
            if candidate.stat().st_size > max_bytes:
                continue
            prefix = candidate.read_bytes()[:8192]
            if b"\0" in prefix:
                continue
        except OSError:
            continue
        files.append(candidate)
    return sorted(files)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
