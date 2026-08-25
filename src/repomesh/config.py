from __future__ import annotations

import json
import socket
from pathlib import Path

from pydantic import Field, field_validator
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
    job_lease_seconds: int = 30
    job_max_retries: int = 3
    rrf_k: int = 60
    context_char_budget: int = 16_000
    node_id: str = Field(default_factory=socket.gethostname)

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
