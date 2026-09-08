from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class SearchMode(StrEnum):
    lexical = "lexical"
    dense = "dense"
    hybrid = "hybrid"


class RegisterRepositoryRequest(BaseModel):
    path: str
    name: str | None = None


class Repository(BaseModel):
    id: str
    name: str
    root: str
    commit_sha: str
    status: str
    indexed_files: int = 0
    indexed_chunks: int = 0
    created_at: str
    updated_at: str


class IndexRequest(BaseModel):
    mode: Literal["full", "incremental"] = "incremental"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int = Field(default=0, ge=-100, le=100)
    wait: bool = False


class Job(BaseModel):
    id: str
    repository_id: str
    kind: str
    mode: str
    status: str
    progress_done: int
    progress_total: int
    indexed_files: int = 0
    indexed_chunks: int = 0
    deleted_files: int = 0
    skipped_files: int = 0
    error_count: int = 0
    attempt: int = 0
    max_attempts: int = 3
    priority: int = 0
    worker_id: str | None = None
    lease_generation: int = 0
    available_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    idempotency_key: str | None = None
    heartbeat_at: str | None = None
    lease_expires_at: str | None = None
    cancel_requested: bool = False
    error: str | None = None
    created_at: str
    updated_at: str


class SearchRequest(BaseModel):
    repository_id: str
    query: str = Field(min_length=1, max_length=4000)
    mode: SearchMode = SearchMode.hybrid
    top_k: int = Field(default=5, ge=1, le=50)
    lexical_k: int = Field(default=20, ge=1, le=100)
    vector_k: int = Field(default=20, ge=1, le=100)


class SearchResult(BaseModel):
    chunk_id: str
    repository_id: str
    commit_sha: str
    file_path: str
    language: str
    symbol_name: str | None
    symbol_kind: str | None
    start_line: int
    end_line: int
    content: str
    chunking_strategy: str
    score: float
    lexical_score: float | None = None
    vector_score: float | None = None
    rrf_score: float | None = None
    retrieval_method: str


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    repository_id: str
    results: list[SearchResult]
    latency_ms: float


class AnswerRequest(BaseModel):
    repository_id: str
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=6, ge=1, le=20)


class Citation(BaseModel):
    chunk_id: str
    file_path: str
    start_line: int
    end_line: int

    @property
    def label(self) -> str:
        return f"[{self.file_path}:{self.start_line}-{self.end_line}]"


class AnswerResponse(BaseModel):
    answer: str
    citations: list[Citation]
    chunks: list[SearchResult]
    evidence_sufficient: bool
    retrieval_latency_ms: float
    generation_latency_ms: float
    total_latency_ms: float


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=128)


class EmbedResponse(BaseModel):
    model: str
    dimensions: int
    vectors: list[list[float]]


class HealthResponse(BaseModel):
    node_id: str
    version: str
    uptime_seconds: float
    qdrant_status: str
    ollama_status: str
    embedding_model: str
    generation_model: str
    active_jobs: int
    queued_jobs: int
    degraded_mode: bool
    postgres_status: str = "unknown"
    active_workers: int = 0


class CapabilitiesResponse(BaseModel):
    api_version: str = "v1"
    modes: list[str]
    languages: list[str]
    providers: dict[str, str]
    limits: dict[str, Any]
