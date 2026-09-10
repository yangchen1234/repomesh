from __future__ import annotations

import json
import socket
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REPOMESH_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8787
    data_dir: Path = Path("data")
    repository_roots: list[Path] = Field(default_factory=lambda: [Path.cwd()])
    api_token: str | None = None
    max_file_bytes: int = 1_000_000
    chunk_max_lines: int = 180
    fallback_chunk_lines: int = 80
    fallback_overlap_lines: int = 10
    embedding_provider: str = "fake"
    embedding_model: str = "nomic-embed-text"
    embedding_version: str = "latest"
    embedding_dimensions: int = 384
    generation_provider: str = "fake"
    generation_model: str = "qwen2.5-coder:1.5b"
    ollama_url: str = "http://127.0.0.1:11434"
    vector_provider: str = "memory"
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "repomesh_chunks"
    postgres_dsn: str = Field(
        default="postgresql://repomesh:repomesh@127.0.0.1:5432/repomesh", repr=False
    )
    postgres_schema: str = "repomesh"
    job_lease_seconds: float = Field(default=30, gt=0)
    job_heartbeat_seconds: float = Field(default=10, gt=0)
    job_poll_seconds: float = Field(default=1, gt=0)
    job_max_retries: int = Field(default=3, ge=1)
    job_retry_base_seconds: float = Field(default=1, gt=0)
    job_retry_max_seconds: float = Field(default=60, gt=0)
    worker_offline_seconds: float = Field(default=60, gt=0)
    watch_poll_seconds: float = Field(default=5, gt=0)
    watch_debounce_seconds: float = Field(default=2, ge=0)
    watch_max_wait_seconds: float = Field(default=30, gt=0)
    watch_default_enabled: bool = True
    rrf_k: int = 60
    context_char_budget: int = 16_000
    node_id: str = Field(default_factory=socket.gethostname)

    @model_validator(mode="after")
    def validate_coordination(self) -> Settings:
        import re

        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", self.postgres_schema):
            raise ValueError("postgres_schema must be a simple lowercase SQL identifier")
        if self.job_heartbeat_seconds >= self.job_lease_seconds:
            raise ValueError("job_heartbeat_seconds must be less than job_lease_seconds")
        if self.worker_offline_seconds <= self.job_heartbeat_seconds:
            raise ValueError("worker_offline_seconds must exceed job_heartbeat_seconds")
        if self.watch_max_wait_seconds < self.watch_debounce_seconds:
            raise ValueError("watch_max_wait_seconds must be at least watch_debounce_seconds")
        return self

    @field_validator("repository_roots", mode="before")
    @classmethod
    def parse_roots(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return [Path(item) for item in json.loads(text)]
            return [Path(item.strip()) for item in text.split(";") if item.strip()]
        return value

    def resolved_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()

    def resolved_roots(self) -> list[Path]:
        return [path.expanduser().resolve() for path in self.repository_roots]
