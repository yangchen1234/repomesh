from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from repomesh.models import Job, Repository, utc_now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root TEXT NOT NULL UNIQUE,
    commit_sha TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'registered',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    vector_synced INTEGER NOT NULL DEFAULT 1,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY(repository_id, path)
);
CREATE TABLE IF NOT EXISTS chunks (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,
    symbol_name TEXT,
    symbol_kind TEXT,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    imports_json TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    chunking_strategy TEXT NOT NULL,
    vector_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_repo_path ON chunks(repository_id, file_path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content, symbol_name, file_path, content='chunks', content_rowid='rowid',
    tokenize='unicode61 tokenchars ''_'''
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, content, symbol_name, file_path)
  VALUES (new.rowid, new.content, new.symbol_name, new.file_path);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, content, symbol_name, file_path)
  VALUES ('delete', old.rowid, old.content, old.symbol_name, old.file_path);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, content, symbol_name, file_path)
  VALUES ('delete', old.rowid, old.content, old.symbol_name, old.file_path);
  INSERT INTO chunks_fts(rowid, content, symbol_name, file_path)
  VALUES (new.rowid, new.content, new.symbol_name, new.file_path);
END;
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    progress_done INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    indexed_files INTEGER NOT NULL DEFAULT 0,
    indexed_chunks INTEGER NOT NULL DEFAULT 0,
    deleted_files INTEGER NOT NULL DEFAULT 0,
    skipped_files INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT UNIQUE,
    heartbeat_at TEXT,
    lease_expires_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_file_errors (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    error TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS query_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id TEXT NOT NULL,
    query TEXT NOT NULL,
    mode TEXT NOT NULL,
    retrieval_ms REAL NOT NULL,
    generation_ms REAL NOT NULL DEFAULT 0,
    chunk_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(SCHEMA)
            columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(files)").fetchall()
            }
            if "vector_synced" not in columns:
                self._connection.execute(
                    "ALTER TABLE files ADD COLUMN vector_synced INTEGER NOT NULL DEFAULT 1"
                )
                self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._connection.execute(sql, tuple(params))
            self._connection.commit()
            return cursor

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def transaction(self) -> sqlite3.Connection:
        return self._connection

    def add_repository(self, repository: Repository) -> None:
        self.execute(
            """INSERT INTO repositories(id,name,root,commit_sha,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            (
                repository.id,
                repository.name,
                repository.root,
                repository.commit_sha,
                repository.status,
                repository.created_at,
                repository.updated_at,
            ),
        )

    def repository(self, repository_id: str) -> Repository | None:
        row = self.fetchone(
            """SELECT r.*, COUNT(DISTINCT f.path) indexed_files, COUNT(DISTINCT c.id) indexed_chunks
            FROM repositories r LEFT JOIN files f ON f.repository_id=r.id
            LEFT JOIN chunks c ON c.repository_id=r.id WHERE r.id=? GROUP BY r.id""",
            (repository_id,),
        )
        return Repository.model_validate(row) if row else None

    def repositories(self) -> list[Repository]:
        rows = self.fetchall(
            """SELECT r.*, COUNT(DISTINCT f.path) indexed_files, COUNT(DISTINCT c.id) indexed_chunks
            FROM repositories r LEFT JOIN files f ON f.repository_id=r.id
            LEFT JOIN chunks c ON c.repository_id=r.id GROUP BY r.id ORDER BY r.created_at"""
        )
        return [Repository.model_validate(row) for row in rows]

    def update_repository(self, repository_id: str, **fields: Any) -> None:
        allowed = {"commit_sha", "status", "updated_at"}
        values = {key: value for key, value in fields.items() if key in allowed}
        values.setdefault("updated_at", utc_now())
        assignments = ",".join(f"{key}=?" for key in values)
        self.execute(
            f"UPDATE repositories SET {assignments} WHERE id=?",  # noqa: S608
            (*values.values(), repository_id),
        )

    def file_manifest(self, repository_id: str) -> dict[str, dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM files WHERE repository_id=?", (repository_id,))
        return {row["path"]: row for row in rows}

    def replace_file_chunks(
        self,
        repository_id: str,
        path: str,
        sha256: str,
        commit_sha: str,
        chunks: list[dict[str, Any]],
        vector_synced: bool = True,
    ) -> None:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM chunks WHERE repository_id=? AND file_path=?", (repository_id, path)
            )
            for chunk in chunks:
                self._connection.execute(
                    """INSERT INTO chunks(
                    id,repository_id,commit_sha,file_path,language,symbol_name,symbol_kind,
                    start_line,end_line,content,content_sha256,imports_json,embedding_model,
                    embedding_version,chunking_strategy,vector_json)
                    VALUES(:id,:repository_id,:commit_sha,:file_path,:language,:symbol_name,
                    :symbol_kind,:start_line,:end_line,:content,:content_sha256,:imports_json,
                    :embedding_model,:embedding_version,:chunking_strategy,:vector_json)""",
                    chunk,
                )
            self._connection.execute(
                """INSERT INTO files(repository_id,path,sha256,commit_sha,chunk_count,vector_synced,indexed_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(repository_id,path) DO UPDATE SET
                sha256=excluded.sha256,commit_sha=excluded.commit_sha,
                chunk_count=excluded.chunk_count,vector_synced=excluded.vector_synced,
                indexed_at=excluded.indexed_at""",
                (repository_id, path, sha256, commit_sha, len(chunks), int(vector_synced), now),
            )

    def delete_file(self, repository_id: str, path: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM chunks WHERE repository_id=? AND file_path=?", (repository_id, path)
            )
            self._connection.execute(
                "DELETE FROM files WHERE repository_id=? AND path=?", (repository_id, path)
            )

    def update_file_commit(self, repository_id: str, path: str, commit_sha: str) -> None:
        """Advance unchanged file/chunk provenance without re-embedding content."""
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE files SET commit_sha=? WHERE repository_id=? AND path=?",
                (commit_sha, repository_id, path),
            )
            self._connection.execute(
                "UPDATE chunks SET commit_sha=? WHERE repository_id=? AND file_path=?",
                (commit_sha, repository_id, path),
            )

    def chunk(self, chunk_id: str) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM chunks WHERE id=?", (chunk_id,))

    def chunks(self, repository_id: str) -> list[dict[str, Any]]:
        return self.fetchall("SELECT * FROM chunks WHERE repository_id=?", (repository_id,))

    def lexical_search(self, repository_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        tokens = [token.replace('"', "") for token in query.split() if token.strip()]
        match_query = " OR ".join(f'"{token}"' for token in tokens)
        if not match_query:
            return []
        try:
            return self.fetchall(
                """SELECT c.*, bm25(chunks_fts, 1.0, 4.0, 2.0) AS bm25_score
                FROM chunks_fts JOIN chunks c ON c.rowid=chunks_fts.rowid
                WHERE chunks_fts MATCH ? AND c.repository_id=? ORDER BY bm25_score LIMIT ?""",
                (match_query, repository_id, limit),
            )
        except sqlite3.OperationalError:
            return []

    def add_job(self, job: Job) -> None:
        values = job.model_dump()
        values["cancel_requested"] = int(job.cancel_requested)
        columns = ",".join(values)
        placeholders = ",".join("?" for _key in values)
        self.execute(f"INSERT INTO jobs({columns}) VALUES({placeholders})", values.values())  # noqa: S608

    def job(self, job_id: str) -> Job | None:
        row = self.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,))
        return Job.model_validate(row) if row else None

    def job_by_key(self, key: str) -> Job | None:
        row = self.fetchone("SELECT * FROM jobs WHERE idempotency_key=?", (key,))
        return Job.model_validate(row) if row else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = set(Job.model_fields) - {"id", "repository_id", "kind", "mode", "created_at"}
        values = {
            key: int(value) if key == "cancel_requested" else value
            for key, value in fields.items()
            if key in allowed
        }
        values.setdefault("updated_at", utc_now())
        assignments = ",".join(f"{key}=?" for key in values)
        self.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values.values(), job_id))  # noqa: S608

    def recoverable_jobs(self) -> list[Job]:
        rows = self.fetchall("SELECT * FROM jobs WHERE status IN ('queued','running','retrying')")
        return [Job.model_validate(row) for row in rows]

    def job_counts(self) -> tuple[int, int]:
        rows = self.fetchall("SELECT status,COUNT(*) count FROM jobs GROUP BY status")
        counts = {row["status"]: row["count"] for row in rows}
        return counts.get("running", 0), counts.get("queued", 0) + counts.get("retrying", 0)

    def log_query(
        self,
        repository_id: str,
        query: str,
        mode: str,
        retrieval_ms: float,
        generation_ms: float,
        chunk_ids: list[str],
    ) -> None:
        self.execute(
            """INSERT INTO query_logs(repository_id,query,mode,retrieval_ms,generation_ms,
            chunk_ids_json,created_at) VALUES(?,?,?,?,?,?,?)""",
            (
                repository_id,
                query,
                mode,
                retrieval_ms,
                generation_ms,
                json.dumps(chunk_ids),
                utc_now(),
            ),
        )
