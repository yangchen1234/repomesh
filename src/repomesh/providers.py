from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from typing import Any

import httpx
from qdrant_client import QdrantClient, models


class ProviderUnavailable(RuntimeError):
    pass


class Embedder(ABC):
    model: str
    version: str
    dimensions: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def healthy(self) -> bool: ...


class DeterministicEmbedder(Embedder):
    def __init__(self, dimensions: int = 384) -> None:
        self.model = "deterministic-hash"
        self.version = "1"
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            words = text.lower().replace("_", " ").split()
            for word in words:
                digest = hashlib.sha256(word.encode()).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors

    def healthy(self) -> bool:
        return True


class OllamaEmbedder(Embedder):
    def __init__(self, base_url: str, model: str, version: str, dimensions: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.version = version
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = httpx.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts},
                timeout=120,
            )
            response.raise_for_status()
            vectors = response.json()["embeddings"]
            if vectors:
                self.dimensions = len(vectors[0])
            return vectors
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderUnavailable(f"Ollama embedding unavailable: {exc}") from exc

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.base_url}/api/tags", timeout=2).is_success
        except httpx.HTTPError:
            return False


class Generator(ABC):
    model: str

    @abstractmethod
    def generate(self, query: str, contexts: list[dict[str, Any]]) -> str: ...

    @abstractmethod
    def healthy(self) -> bool: ...


class DeterministicGenerator(Generator):
    model = "deterministic-extractive"

    def generate(self, query: str, contexts: list[dict[str, Any]]) -> str:
        if not contexts:
            return "Evidence insufficient: no indexed source matched the question."
        citations = " ".join(
            f"[{item['file_path']}:{item['start_line']}-{item['end_line']}]"
            for item in contexts[:3]
        )
        symbols = ", ".join(item["symbol_name"] or item["file_path"] for item in contexts[:3])
        return f"The strongest indexed evidence for '{query}' is in {symbols}. {citations}"

    def healthy(self) -> bool:
        return True


class OllamaGenerator(Generator):
    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def generate(self, query: str, contexts: list[dict[str, Any]]) -> str:
        if not contexts:
            return "Evidence insufficient: no indexed source matched the question."
        blocks = []
        for item in contexts:
            citation = f"[{item['file_path']}:{item['start_line']}-{item['end_line']}]"
            blocks.append(f"SOURCE {citation}\n{item['content']}")
        prompt = (
            "Answer only from the supplied source code. Cite every factual claim using an exact "
            "SOURCE label. Never invent paths or line numbers. If evidence is inadequate, begin "
            "with 'Evidence insufficient'.\n\nQUESTION:\n" + query + "\n\n" + "\n\n".join(blocks)
        )
        try:
            response = httpx.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=180,
            )
            response.raise_for_status()
            return str(response.json()["response"]).strip()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderUnavailable(f"Ollama generation unavailable: {exc}") from exc

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.base_url}/api/tags", timeout=2).is_success
        except httpx.HTTPError:
            return False


class VectorStore(ABC):
    @abstractmethod
    def upsert(self, chunks: list[dict[str, Any]], vectors: list[list[float]]) -> None: ...

    @abstractmethod
    def delete_file(self, repository_id: str, file_path: str) -> None: ...

    @abstractmethod
    def search(
        self, repository_id: str, vector: list[float], limit: int
    ) -> list[tuple[str, float]]: ...

    @abstractmethod
    def healthy(self) -> bool: ...

    def version(self) -> str:
        return "n/a"


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


class SQLiteVectorStore(VectorStore):
    def __init__(self, database: Any) -> None:
        self.database = database

    def upsert(self, chunks: list[dict[str, Any]], vectors: list[list[float]]) -> None:
        return None

    def delete_file(self, repository_id: str, file_path: str) -> None:
        return None

    def search(
        self, repository_id: str, vector: list[float], limit: int
    ) -> list[tuple[str, float]]:
        import json

        scored = []
        for chunk in self.database.chunks(repository_id):
            if chunk["vector_json"]:
                scored.append((chunk["id"], cosine(vector, json.loads(chunk["vector_json"]))))
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]

    def healthy(self) -> bool:
        return True


class QdrantVectorStore(VectorStore):
    def __init__(self, url: str, collection: str, dimensions: int) -> None:
        self.url = url
        self.collection = collection
        self.dimensions = dimensions
        self.client = QdrantClient(url=url, timeout=5)

    def ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(
                    size=self.dimensions, distance=models.Distance.COSINE
                ),
            )

    def upsert(self, chunks: list[dict[str, Any]], vectors: list[list[float]]) -> None:
        try:
            self.ensure_collection()
            points = [
                models.PointStruct(
                    id=chunk["id"],
                    vector=vector,
                    payload={
                        "repository_id": chunk["repository_id"],
                        "file_path": chunk["file_path"],
                    },
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
            if points:
                self.client.upsert(self.collection, points=points, wait=True)
        except Exception as exc:
            raise ProviderUnavailable(f"Qdrant upsert unavailable: {exc}") from exc

    def delete_file(self, repository_id: str, file_path: str) -> None:
        try:
            if not self.client.collection_exists(self.collection):
                return
            self.client.delete(
                self.collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="repository_id", match=models.MatchValue(value=repository_id)
                            ),
                            models.FieldCondition(
                                key="file_path", match=models.MatchValue(value=file_path)
                            ),
                        ]
                    )
                ),
                wait=True,
            )
        except Exception as exc:
            raise ProviderUnavailable(f"Qdrant delete unavailable: {exc}") from exc

    def search(
        self, repository_id: str, vector: list[float], limit: int
    ) -> list[tuple[str, float]]:
        try:
            results = self.client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="repository_id", match=models.MatchValue(value=repository_id)
                        )
                    ]
                ),
                limit=limit,
            ).points
            return [(str(point.id), point.score) for point in results]
        except Exception as exc:
            raise ProviderUnavailable(f"Qdrant search unavailable: {exc}") from exc

    def healthy(self) -> bool:
        try:
            self.client.get_collections()
            return True
        except Exception:
            return False

    def version(self) -> str:
        try:
            return httpx.get(self.url, timeout=2).json().get("version", "unknown")
        except Exception:
            return "unavailable"
