from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from repomesh.chunking import CodeChunker
from repomesh.db import Database
from repomesh.providers import Embedder, ProviderUnavailable, VectorStore
from repomesh.repositories import current_commit, discover_files, file_sha256

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IndexStats:
    total_files: int = 0
    indexed_files: int = 0
    indexed_chunks: int = 0
    deleted_files: int = 0
    skipped_files: int = 0
    error_count: int = 0


class IndexCancelled(RuntimeError):
    pass


ProgressCallback = Callable[[IndexStats, str | None, str | None], None]
CancelCallback = Callable[[], bool]


class RepositoryIndexer:
    def __init__(
        self,
        database: Database,
        chunker: CodeChunker,
        embedder: Embedder,
        vector_store: VectorStore,
        max_file_bytes: int,
    ) -> None:
        self.database = database
        self.chunker = chunker
        self.embedder = embedder
        self.vector_store = vector_store
        self.max_file_bytes = max_file_bytes

    def index(
        self,
        repository_id: str,
        mode: str,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> IndexStats:
        repository = self.database.repository(repository_id)
        if not repository:
            raise ValueError("repository not found")
        root = Path(repository.root)
        commit_sha = current_commit(root)
        files = discover_files(root, self.max_file_bytes)
        relative_map = {path.relative_to(root).as_posix(): path for path in files}
        manifest = self.database.file_manifest(repository_id)
        deleted = sorted(set(manifest) - set(relative_map))
        stats = IndexStats(total_files=len(files) + len(deleted))
        progress(stats, None, None)

        for relative_path in deleted:
            if cancelled():
                raise IndexCancelled("index job cancelled")
            self.database.delete_file(repository_id, relative_path)
            try:
                self.vector_store.delete_file(repository_id, relative_path)
            except ProviderUnavailable as exc:
                logger.warning(
                    "vector deletion deferred", extra={"path": relative_path, "error": str(exc)}
                )
            stats.deleted_files += 1
            progress(stats, relative_path, None)

        for relative_path, path in relative_map.items():
            if cancelled():
                raise IndexCancelled("index job cancelled")
            try:
                digest = file_sha256(path)
                if (
                    mode == "incremental"
                    and manifest.get(relative_path, {}).get("sha256") == digest
                ):
                    stats.skipped_files += 1
                    progress(stats, relative_path, None)
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                chunks = self.chunker.chunk(
                    repository_id,
                    commit_sha,
                    relative_path,
                    content,
                    self.embedder.model,
                    self.embedder.version,
                )
                texts = [
                    f"{chunk.file_path}\n{chunk.symbol_name or ''}\n{chunk.content}"
                    for chunk in chunks
                ]
                vectors = self.embedder.embed(texts) if texts else []
                chunk_dicts = []
                for chunk, vector in zip(chunks, vectors, strict=True):
                    item = chunk.to_dict()
                    item["vector_json"] = json.dumps(vector)
                    chunk_dicts.append(item)
                self.database.replace_file_chunks(
                    repository_id, relative_path, digest, commit_sha, chunk_dicts
                )
                try:
                    self.vector_store.delete_file(repository_id, relative_path)
                    self.vector_store.upsert(chunk_dicts, vectors)
                except ProviderUnavailable as exc:
                    logger.warning(
                        "vector sync deferred", extra={"path": relative_path, "error": str(exc)}
                    )
                stats.indexed_files += 1
                stats.indexed_chunks += len(chunks)
                progress(stats, relative_path, None)
            except Exception as exc:
                stats.error_count += 1
                progress(stats, relative_path, str(exc))
        self.database.update_repository(repository_id, commit_sha=commit_sha, status="indexed")
        return stats
