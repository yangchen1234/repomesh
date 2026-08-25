from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from repomesh import __version__
from repomesh.config import Settings
from repomesh.models import (
    AnswerRequest,
    AnswerResponse,
    CapabilitiesResponse,
    Citation,
    EmbedRequest,
    EmbedResponse,
    HealthResponse,
    IndexRequest,
    Job,
    RegisterRepositoryRequest,
    Repository,
    SearchMode,
    SearchRequest,
    SearchResponse,
)
from repomesh.providers import ProviderUnavailable, QdrantVectorStore
from repomesh.repositories import (
    RepositoryValidationError,
    register_repository,
    validate_repository_path,
)
from repomesh.services import Services, configure_logging

logger = structlog.get_logger()
REQUEST_COUNT = Counter(
    "repomesh_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
REQUEST_LATENCY = Histogram(
    "repomesh_http_request_duration_seconds", "HTTP request duration", ["path"]
)
SEARCH_LATENCY = Histogram("repomesh_search_duration_seconds", "Search latency", ["mode"])
INDEXED_FILES = Counter("repomesh_indexed_files_total", "Files indexed")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging()
    services = Services.create(settings)
    started_at = time.monotonic()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        recovered = services.jobs.recover()
        logger.info("repomesh_started", node_id=settings.node_id, recovered_jobs=recovered)
        yield
        services.close()

    app = FastAPI(
        title="RepoMesh Compute Node API",
        version=__version__,
        description="Versioned API for standalone and remote codebase RAG compute operations.",
        lifespan=lifespan,
    )
    app.state.services = services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            f"http://127.0.0.1:{settings.port}",
        ],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Token"],
    )

    @app.middleware("http")
    async def observe_requests(request: Request, call_next: Any) -> Response:
        start = time.perf_counter()
        response: Response
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_failed", method=request.method, path=request.url.path)
            raise
        elapsed = time.perf_counter() - start
        REQUEST_COUNT.labels(request.method, request.url.path, str(response.status_code)).inc()
        REQUEST_LATENCY.labels(request.url.path).observe(elapsed)
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            latency_ms=round(elapsed * 1000, 3),
        )
        return response

    def require_token(
        authorization: Annotated[str | None, Header()] = None,
        x_api_token: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = settings.api_token
        if not expected:
            return
        supplied = x_api_token
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        import secrets

        if supplied is None or not secrets.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API token"
            )

    @app.exception_handler(ProviderUnavailable)
    async def unavailable_handler(_request: Request, exc: ProviderUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc), "degraded": True})

    @app.get("/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        qdrant_status = "not_configured"
        if isinstance(services.vector_store, QdrantVectorStore):
            qdrant_status = "healthy" if services.vector_store.healthy() else "unavailable"
        try:
            ollama_status = (
                "healthy"
                if httpx.get(f"{settings.ollama_url.rstrip('/')}/api/tags", timeout=1).is_success
                else "unavailable"
            )
        except httpx.HTTPError:
            ollama_status = "unavailable"
        active, queued = services.database.job_counts()
        required_ollama = (
            settings.embedding_provider == "ollama" or settings.generation_provider == "ollama"
        )
        required_qdrant = settings.vector_provider == "qdrant"
        degraded = (required_ollama and ollama_status != "healthy") or (
            required_qdrant and qdrant_status != "healthy"
        )
        return HealthResponse(
            node_id=settings.node_id,
            version=__version__,
            uptime_seconds=time.monotonic() - started_at,
            qdrant_status=qdrant_status,
            ollama_status=ollama_status,
            embedding_model=services.embedder.model,
            generation_model=services.generator.model,
            active_jobs=active,
            queued_jobs=queued,
            degraded_mode=degraded,
        )

    @app.get("/v1/capabilities", response_model=CapabilitiesResponse)
    def capabilities(_auth: None = Depends(require_token)) -> CapabilitiesResponse:
        return CapabilitiesResponse(
            modes=["standalone", "worker", "lexical", "dense", "hybrid"],
            languages=["python", "java", "javascript", "typescript", "cpp", "html", "css"],
            providers={
                "embedding": settings.embedding_provider,
                "generation": settings.generation_provider,
                "vector_store": settings.vector_provider,
            },
            limits={"max_file_bytes": settings.max_file_bytes, "max_search_top_k": 50},
        )

    @app.get("/v1/repositories", response_model=list[Repository])
    def list_repositories(_auth: None = Depends(require_token)) -> list[Repository]:
        return services.database.repositories()

    @app.post("/v1/repositories", response_model=Repository, status_code=201)
    def add_repository(
        body: RegisterRepositoryRequest, _auth: None = Depends(require_token)
    ) -> Repository:
        try:
            path = validate_repository_path(body.path, settings.resolved_roots())
            repository = register_repository(path, body.name)
            existing = services.database.repository(repository.id)
            if existing:
                return existing
            services.database.add_repository(repository)
            return repository
        except RepositoryValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/repositories/{repository_id}/index", response_model=Job, status_code=202)
    def index_repository(
        repository_id: str, body: IndexRequest, _auth: None = Depends(require_token)
    ) -> Job:
        if not services.database.repository(repository_id):
            raise HTTPException(status_code=404, detail="repository not found")
        try:
            job = services.jobs.submit(repository_id, body.mode, body.idempotency_key)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if body.wait:
            job = services.jobs.wait(job.id)
            INDEXED_FILES.inc(job.indexed_files)
        return job

    @app.get("/v1/repositories/{repository_id}/status")
    def repository_status(
        repository_id: str, _auth: None = Depends(require_token)
    ) -> dict[str, Any]:
        repository = services.database.repository(repository_id)
        if not repository:
            raise HTTPException(status_code=404, detail="repository not found")
        latest = services.database.fetchone(
            "SELECT * FROM jobs WHERE repository_id=? ORDER BY created_at DESC LIMIT 1",
            (repository_id,),
        )
        return {
            "repository": repository.model_dump(),
            "latest_job": Job.model_validate(latest).model_dump() if latest else None,
        }

    @app.post("/v1/search", response_model=SearchResponse)
    def search(body: SearchRequest, _auth: None = Depends(require_token)) -> SearchResponse:
        if not services.database.repository(body.repository_id):
            raise HTTPException(status_code=404, detail="repository not found")
        with SEARCH_LATENCY.labels(body.mode.value).time():
            return services.retriever.search(
                body.repository_id,
                body.query,
                body.mode,
                body.top_k,
                body.lexical_k,
                body.vector_k,
            )

    @app.post("/v1/answer", response_model=AnswerResponse)
    def answer(body: AnswerRequest, _auth: None = Depends(require_token)) -> AnswerResponse:
        total_started = time.perf_counter()
        retrieval_started = time.perf_counter()
        search_response = services.retriever.search(
            body.repository_id, body.query, SearchMode.hybrid, body.top_k, 20, 20
        )
        retrieval_ms = (time.perf_counter() - retrieval_started) * 1000
        budget = settings.context_char_budget
        contexts: list[dict[str, Any]] = []
        for result in search_response.results:
            item = result.model_dump()
            if len(item["content"]) > budget:
                break
            contexts.append(item)
            budget -= len(item["content"])
        generation_started = time.perf_counter()
        generated = services.generator.generate(body.query, contexts)
        generation_ms = (time.perf_counter() - generation_started) * 1000
        citations = [
            Citation(
                chunk_id=item["chunk_id"],
                file_path=item["file_path"],
                start_line=item["start_line"],
                end_line=item["end_line"],
            )
            for item in contexts
        ]
        allowed = {citation.label for citation in citations}
        for label in re.findall(r"\[[^\]\n]+:\d+-\d+\]", generated):
            if label not in allowed:
                generated = generated.replace(label, "[unverified citation removed]")
        if citations and not any(citation.label in generated for citation in citations):
            generated = (
                generated.rstrip() + "\n\nSources: " + " ".join(c.label for c in citations[:3])
            )
        if not citations:
            generated = "Evidence insufficient: no indexed source matched the question."
        total_ms = (time.perf_counter() - total_started) * 1000
        services.database.log_query(
            body.repository_id,
            body.query,
            "answer",
            retrieval_ms,
            generation_ms,
            [item["chunk_id"] for item in contexts],
        )
        return AnswerResponse(
            answer=generated,
            citations=citations,
            chunks=search_response.results,
            evidence_sufficient=bool(citations)
            and not generated.lower().startswith("evidence insufficient"),
            retrieval_latency_ms=retrieval_ms,
            generation_latency_ms=generation_ms,
            total_latency_ms=total_ms,
        )

    @app.post("/v1/embed", response_model=EmbedResponse)
    def embed(body: EmbedRequest, _auth: None = Depends(require_token)) -> EmbedResponse:
        vectors = services.embedder.embed(body.texts)
        return EmbedResponse(
            model=services.embedder.model, dimensions=len(vectors[0]), vectors=vectors
        )

    @app.get("/v1/jobs/{job_id}", response_model=Job)
    def get_job(job_id: str, _auth: None = Depends(require_token)) -> Job:
        job = services.database.job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.post("/v1/jobs/{job_id}/cancel", response_model=Job)
    def cancel_job(job_id: str, _auth: None = Depends(require_token)) -> Job:
        try:
            return services.jobs.cancel(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    dashboard_dist = Path(__file__).resolve().parents[2] / "dashboard" / "dist"
    if dashboard_dist.exists():
        app.mount("/assets", StaticFiles(directory=dashboard_dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def dashboard() -> FileResponse:
            return FileResponse(dashboard_dist / "index.html")

    return app


app = create_app()
