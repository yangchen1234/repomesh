from __future__ import annotations

import logging
from dataclasses import dataclass

import structlog

from repomesh.chunking import CodeChunker
from repomesh.config import Settings
from repomesh.coordination.repository import CoordinationRepository
from repomesh.db import Database
from repomesh.indexing import RepositoryIndexer
from repomesh.jobs import JobManager
from repomesh.providers import (
    DeterministicEmbedder,
    DeterministicGenerator,
    Embedder,
    Generator,
    OllamaEmbedder,
    OllamaGenerator,
    QdrantVectorStore,
    SQLiteVectorStore,
    VectorStore,
)
from repomesh.retrieval import Retriever


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


@dataclass(slots=True)
class Services:
    settings: Settings
    database: Database
    embedder: Embedder
    generator: Generator
    vector_store: VectorStore
    indexer: RepositoryIndexer
    jobs: JobManager
    coordination: CoordinationRepository
    retriever: Retriever

    @classmethod
    def create(cls, settings: Settings) -> Services:
        data_dir = settings.resolved_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        database = Database(data_dir / "repomesh.sqlite3")
        embedder: Embedder
        if settings.embedding_provider == "ollama":
            embedder = OllamaEmbedder(
                settings.ollama_url,
                settings.embedding_model,
                settings.embedding_version,
                settings.embedding_dimensions,
            )
        else:
            embedder = DeterministicEmbedder(settings.embedding_dimensions)
        generator: Generator
        if settings.generation_provider == "ollama":
            generator = OllamaGenerator(settings.ollama_url, settings.generation_model)
        else:
            generator = DeterministicGenerator()
        vector_store: VectorStore
        if settings.vector_provider == "qdrant":
            vector_store = QdrantVectorStore(
                settings.qdrant_url, settings.qdrant_collection, settings.embedding_dimensions
            )
        else:
            vector_store = SQLiteVectorStore(database)
        chunker = CodeChunker(
            settings.chunk_max_lines,
            settings.fallback_chunk_lines,
            settings.fallback_overlap_lines,
        )
        indexer = RepositoryIndexer(
            database, chunker, embedder, vector_store, settings.max_file_bytes
        )
        coordination = CoordinationRepository(settings)
        try:
            coordination.migrate()
        except Exception:
            coordination.close()
            database.close()
            raise
        jobs = JobManager(coordination)
        retriever = Retriever(database, embedder, vector_store, settings.rrf_k)
        return cls(
            settings,
            database,
            embedder,
            generator,
            vector_store,
            indexer,
            jobs,
            coordination,
            retriever,
        )

    def close(self) -> None:
        self.coordination.close()
        if isinstance(self.vector_store, QdrantVectorStore):
            self.vector_store.client.close()
        self.database.close()
