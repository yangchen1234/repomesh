# RepoMesh Compute Node Protocol v1

## Transport and compatibility

- Base URL: `http://127.0.0.1:8787` by default.
- Media type: `application/json` except `/metrics` and Dashboard assets.
- Version prefix: `/v1`. Fields may be added compatibly; removal/semantic changes require `/v2`.
- Paths in payloads are repository-relative with `/` separators. Lines are one-based and inclusive.
- OpenAPI is served at `/openapi.json`; Swagger UI is `/docs`.
- Authentication: `Authorization: Bearer <REPOMESH_API_TOKEN>` or `X-API-Token: <REPOMESH_API_TOKEN>`. Health and metrics are intentionally unprotected for private probes; other v1 operations require the token when configured. A token is mandatory for supported non-loopback native binds.

Errors use `{"detail":"..."}`. Provider failures return `503` and `degraded: true`; validation returns `400` or `422`; missing resources return `404`; conflicting idempotency use returns `409`.

## Stable endpoints

### `GET /v1/health`

Returns node ID, application version, uptime seconds, PostgreSQL/Qdrant/Ollama status and active worker count, configured model names, active/queued job counts, and `degraded_mode`. This is the Mac's readiness probe.

### `GET /v1/capabilities`

Returns API version, standalone/worker/retrieval modes, supported languages, active providers, and advertised limits.

### `GET /v1/repositories`

Lists repositories with ID, name, Windows root, current indexed commit, state, indexed file/chunk counts, and timestamps.

### `POST /v1/repositories`

```json
{"path":"<REPOSITORY_PATH>","name":"optional display name"}
```

The path must be an allowlisted Git root. Re-registering the same normalized root returns the existing repository.

### `POST /v1/repositories/{id}/index`

```json
{"mode":"incremental","idempotency_key":"mac-run-2026-08-25-001","wait":false}
```

`mode` is `full` or `incremental`. Both are duplicate-safe; full reprocesses every current file while incremental processes hash changes only. `wait=true` polls PostgreSQL for up to 300 seconds and returns the current state on timeout. It never executes indexing in the API; independent workers must be running. Clients should normally use `wait=false` and poll. Optional `priority` ranges from -100 to 100 (default 0).

### `GET /v1/repositories/{id}/status`

Returns `repository` and `latest_job` objects.

### `GET /v1/jobs/{job_id}`

Job fields include `status`, progress total/done, indexed files/chunks, deleted/skipped/error counts, attempt/max_attempts, worker_id, lease_generation, priority, available/started/completed timestamps, heartbeat/lease timestamps, cancellation flag, and terminal error. Terminal states are `completed`, `completed_with_errors`, `failed`, and `cancelled`.

### `POST /v1/jobs/{job_id}/cancel`

Persists cancellation. Independent workers observe it through heartbeat and guarded storage checkpoints. Queued/retrying jobs cancel immediately. Repeated cancellation is safe.

### `GET /v1/workers`

Lists worker_id, hostname, pid, version, status (`idle`, `busy`, `stopped`, or derived `offline`), started_at, last_seen_at, current_job_id, completed_jobs and failed_jobs. Requires the configured API token.

### `POST /v1/search`

```json
{
  "repository_id":"uuid",
  "query":"Where is restart recovery implemented?",
  "mode":"hybrid",
  "top_k":5,
  "lexical_k":20,
  "vector_k":20
}
```

`mode` is `lexical`, `dense`, or `hybrid`. Every result contains `chunk_id`, repository/commit identity, `file_path`, language, symbol/kind, exact lines, content, chunking strategy, aggregate score, nullable lexical/vector/RRF scores, and retrieval method.

### `POST /v1/answer`

```json
{"repository_id":"uuid","query":"How are changed files detected?","top_k":6}
```

Returns `answer`, structured `citations`, the exact used/retrieved `chunks`, evidence sufficiency, and retrieval/generation/total latencies. Citation labels have the exact form `[path:start-end]`.

### `POST /v1/embed`

```json
{"texts":["first text","second text"]}
```

Returns model, actual dimensions, and vectors. Batch size is limited to 128.

### `GET /metrics`

Prometheus text exposition for HTTP request counts/latency, search latency by mode, Python process metrics, and indexed-file counts.

## Worker call sequence

1. `GET /v1/health` and `/v1/capabilities`.
2. Register or find a repository and retain its UUID.
3. Submit incremental indexing with a Mac-generated idempotency key.
4. Poll `/v1/jobs/{id}` until terminal; surface per-job counters.
5. Call search or answer. Preserve returned commit/path/lines in the UI.
6. Treat 503 as node degradation, not as repository corruption; lexical mode can remain usable.
