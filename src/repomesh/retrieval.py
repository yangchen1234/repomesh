from __future__ import annotations

import time

from repomesh.db import Database
from repomesh.models import SearchMode, SearchResponse, SearchResult
from repomesh.providers import Embedder, VectorStore


class Retriever:
    def __init__(
        self, database: Database, embedder: Embedder, vector_store: VectorStore, rrf_k: int = 60
    ) -> None:
        self.database = database
        self.embedder = embedder
        self.vector_store = vector_store
        self.rrf_k = rrf_k

    def search(
        self,
        repository_id: str,
        query: str,
        mode: SearchMode,
        top_k: int,
        lexical_k: int,
        vector_k: int,
    ) -> SearchResponse:
        started = time.perf_counter()
        lexical_rows = (
            self.database.lexical_search(repository_id, query, lexical_k)
            if mode != SearchMode.dense
            else []
        )
        lexical = [(row["id"], 1.0 / (1.0 + abs(float(row["bm25_score"])))) for row in lexical_rows]
        vector: list[tuple[str, float]] = []
        if mode != SearchMode.lexical:
            query_vector = self.embedder.embed([query])[0]
            vector = self.vector_store.search(repository_id, query_vector, vector_k)
        if mode == SearchMode.lexical:
            ranked_ids = [item[0] for item in lexical]
            combined = {chunk_id: score for chunk_id, score in lexical}
        elif mode == SearchMode.dense:
            ranked_ids = [item[0] for item in vector]
            combined = {chunk_id: score for chunk_id, score in vector}
        else:
            combined = reciprocal_rank_fusion([lexical, vector], self.rrf_k)
            ranked_ids = sorted(combined, key=lambda identifier: combined[identifier], reverse=True)
        lexical_scores = dict(lexical)
        vector_scores = dict(vector)
        results = []
        for chunk_id in ranked_ids[:top_k]:
            row = self.database.chunk(chunk_id)
            if not row:
                continue
            results.append(
                SearchResult(
                    chunk_id=chunk_id,
                    repository_id=row["repository_id"],
                    commit_sha=row["commit_sha"],
                    file_path=row["file_path"],
                    language=row["language"],
                    symbol_name=row["symbol_name"],
                    symbol_kind=row["symbol_kind"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    content=row["content"],
                    chunking_strategy=row["chunking_strategy"],
                    score=combined[chunk_id],
                    lexical_score=lexical_scores.get(chunk_id),
                    vector_score=vector_scores.get(chunk_id),
                    rrf_score=combined.get(chunk_id) if mode == SearchMode.hybrid else None,
                    retrieval_method=mode.value,
                )
            )
        latency_ms = (time.perf_counter() - started) * 1000
        self.database.log_query(
            repository_id, query, mode.value, latency_ms, 0, [r.chunk_id for r in results]
        )
        return SearchResponse(
            query=query,
            mode=mode,
            repository_id=repository_id,
            results=results,
            latency_ms=latency_ms,
        )


def reciprocal_rank_fusion(
    rankings: list[list[tuple[str, float]]], k: int = 60
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, (identifier, _score) in enumerate(ranking, start=1):
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (k + rank)
    return scores
