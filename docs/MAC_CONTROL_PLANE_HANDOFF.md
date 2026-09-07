# Mac control-plane handoff

This is the secure integration contract for a Mac control surface calling one RepoMesh Windows compute node. RepoMesh v1.0 is a single-node vertical slice, not a highly available cluster.

## Security boundary and current readiness

- The supported native launcher defaults to `127.0.0.1:8787`.
- A native wildcard bind (`0.0.0.0` or `::`) is rejected. Remote access must bind to the exact Tailscale or private-LAN IP assigned to Windows.
- A non-loopback bind is rejected unless `REPOMESH_API_TOKEN` is non-empty.
- The Docker image uses an explicit internal wildcard flag because Compose publishes the host port to `127.0.0.1` only. That flag is not for native remote access.
- `/v1/health` and `/metrics` are intentionally probeable without authentication. Every other `/v1` endpoint requires the configured token.
- RepoMesh v1.0 does not terminate TLS. Prefer Tailscale encryption. Do not forward port 8787 on a router, expose it with a public tunnel, or bind it to all interfaces.
- Never put a populated `.env`, token, authorization header, or token-bearing command transcript in Git, logs, screenshots, issue reports, or benchmark output.

## Start Windows locally

From the repository root in PowerShell:

```powershell
.\scripts\start-dependencies.ps1
.\scripts\models.ps1
.\scripts\start-api.ps1
```

The local URLs are `http://127.0.0.1:8787`, `/docs`, and `/metrics`. Local mode can omit a token, but use a token before switching to any non-loopback bind.

## Preferred Tailscale setup

1. Sign in to the same tailnet on Windows and Mac. Apply normal tailnet ACLs so only the intended Mac identity/device can reach this Windows node.
2. On Windows, verify the service and obtain the actual tailnet address:

   ```powershell
   & 'C:\Program Files\Tailscale\tailscale.exe' status
   & 'C:\Program Files\Tailscale\tailscale.exe' ip -4
   ```

3. On the Mac, obtain its Tailscale IP with `tailscale ip -4`.
4. Set Windows process configuration without committing the values. `REPOMESH_HOST` must be the exact Windows Tailscale IP, not `0.0.0.0`:

   ```powershell
   $env:REPOMESH_HOST = '<WINDOWS_TAILSCALE_IP>'
   $env:REPOMESH_PORT = '8787'
   $env:REPOMESH_API_TOKEN = Read-Host -MaskInput 'RepoMesh API token'
   .\scripts\start-api.ps1
   ```

5. In an elevated PowerShell, add one narrowly scoped inbound rule. The remote address is the Mac Tailscale IP:

   ```powershell
   .\scripts\configure-remote-firewall.ps1 `
     -BindIP '<WINDOWS_TAILSCALE_IP>' `
     -RemoteAddress '<MAC_TAILSCALE_IP>' `
     -Port 8787
   ```

   Inspect the created rule with `Get-NetFirewallRule -DisplayName 'RepoMesh private API*' | Get-NetFirewallAddressFilter`. The script refuses wildcard local addresses and unrestricted remote-address spellings.

6. On the Mac, set the two required values in a secure shell/session or Keychain-backed launcher:

   ```bash
   export REPOMESH_BASE_URL='http://<WINDOWS_TAILSCALE_IP>:8787'
   read -s REPOMESH_API_TOKEN && export REPOMESH_API_TOKEN
   python3 scripts/test-remote-client.py
   ```

The smoke client calls health, capabilities, repositories, and hybrid search. It never prints the token and exits nonzero for configuration, network, authentication, HTTP, or response-shape failures.

## Private LAN fallback (no Tailscale)

Use this only on a trusted, private, non-guest LAN. Native HTTP exposes the bearer token to anyone able to observe that LAN traffic, so a private TLS reverse proxy is recommended if the LAN is not fully trusted.

1. Give Windows a stable DHCP reservation; do not configure router port forwarding, UPnP exposure, a public tunnel, or DMZ mode.
2. Set `REPOMESH_HOST` to the exact Windows private-LAN IP and set a strong `REPOMESH_API_TOKEN` outside Git.
3. Run `configure-remote-firewall.ps1` with the exact Windows LAN IP and exact Mac LAN IP. A narrowly scoped private CIDR may be used only if the Mac address cannot be stable.
4. Start with `scripts/start-api.ps1`. The launcher rejects a wildcard and rejects a non-loopback bind without a token.
5. Set the Mac base URL to the exact private LAN IP reported by Windows and run the remote smoke client.
6. Confirm from a third, unapproved LAN device that port 8787 is not reachable. Keep the router free of any inbound mapping for that port.

## Transfer the repository with Git history

Clone the public release repository. Never include a populated `.env` or secret in a fork, issue, or support bundle:

```bash
git clone https://github.com/yangchen1234/repomesh.git
cd repomesh
git show v1.0.0 --no-patch
```

For an offline handoff, a verified `git bundle` preserves commits, branches, tags, and other included refs; a plain source-folder copy does not:

```bash
git bundle verify /path/to/repomesh-mac-handoff.bundle
git clone /path/to/repomesh-mac-handoff.bundle repomesh
cd repomesh
git log --oneline --decorate -5
```

Do not put credentials in a remote URL.

## Mac environment contract

The Mac client uses these names exactly:

```text
REPOMESH_BASE_URL=http://<WINDOWS_TAILSCALE_IP>:8787
REPOMESH_API_TOKEN=<REPOMESH_API_TOKEN>
```

The provided smoke client also accepts optional non-secret controls:

```text
REPOMESH_REMOTE_SEARCH_MODE=hybrid
REPOMESH_REMOTE_QUERY=Where is API authentication enforced?
```

Send `Authorization: Bearer <REPOMESH_API_TOKEN>` on every protected request. `X-API-Token` is supported for compatibility, but bearer authentication is the stable Mac convention. Never put a token in a query string.

## Transport and compatibility

- Media type: `application/json`, except Prometheus text at `/metrics`.
- API version: `/v1`. Compatible fields may be added; removals or semantic breaks require `/v2`.
- OpenAPI: `GET /openapi.json`; interactive docs: `GET /docs`.
- Repository paths returned by Windows use `/` separators and remain Windows-owned; the Mac must not attempt local filesystem access.
- Line numbers are one-based and inclusive.
- Structured citation objects, not rendered prose, are the citation authority.
- Example IDs, hashes, timestamps, scores, and vector values below are illustrative rather than current machine state.

In the examples, `$REPOMESH_BASE_URL` and `$REPOMESH_API_TOKEN` are already configured.

## Complete request and response examples

### Health

```bash
curl --fail --silent --show-error \
  "$REPOMESH_BASE_URL/v1/health"
```

`200 OK`:

```json
{
  "node_id": "WINDOWS-NODE",
  "version": "1.0.0",
  "uptime_seconds": 913.42,
  "qdrant_status": "healthy",
  "ollama_status": "healthy",
  "embedding_model": "nomic-embed-text",
  "generation_model": "qwen2.5-coder:14b",
  "active_jobs": 0,
  "queued_jobs": 0,
  "degraded_mode": false
}
```

A reachable node may report `degraded_mode: true`; inspect both provider status fields. This is different from a network failure.

### Capabilities

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $REPOMESH_API_TOKEN" \
  "$REPOMESH_BASE_URL/v1/capabilities"
```

`200 OK`:

```json
{
  "api_version": "v1",
  "modes": ["standalone", "worker", "lexical", "dense", "hybrid"],
  "languages": ["python", "java", "javascript", "typescript", "cpp", "html", "css"],
  "providers": {
    "embedding": "ollama",
    "generation": "ollama",
    "vector_store": "qdrant"
  },
  "limits": {"max_file_bytes": 1000000, "max_search_top_k": 50}
}
```

### List repositories

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $REPOMESH_API_TOKEN" \
  "$REPOMESH_BASE_URL/v1/repositories"
```

`200 OK`:

```json
[
  {
    "id": "9f4e6f2e-9f0c-54d8-9df5-2da10a5cb641",
    "name": "repomesh",
    "root": "<REPOSITORY_PATH>",
    "commit_sha": "0123456789abcdef0123456789abcdef01234567",
    "status": "indexed",
    "indexed_files": 69,
    "indexed_chunks": 436,
    "created_at": "2026-08-25T20:10:11.000000+00:00",
    "updated_at": "2026-08-25T20:14:32.000000+00:00"
  }
]
```

An empty catalog is `200 OK` with `[]`.

### Register a repository

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $REPOMESH_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"path":"<REPOSITORY_PATH>","name":"Project"}' \
  "$REPOMESH_BASE_URL/v1/repositories"
```

`201 Created`:

```json
{
  "id": "d1436c61-67de-5847-b90e-c87b12d99e49",
  "name": "Project",
  "root": "<REPOSITORY_PATH>",
  "commit_sha": "fedcba9876543210fedcba9876543210fedcba98",
  "status": "registered",
  "indexed_files": 0,
  "indexed_chunks": 0,
  "created_at": "2026-08-25T20:20:00.000000+00:00",
  "updated_at": "2026-08-25T20:20:00.000000+00:00"
}
```

The root must be an allowlisted local Git repository. Re-registering the same normalized root returns its existing repository object without duplicating it.

### Submit indexing

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $REPOMESH_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"incremental","idempotency_key":"mac-run-20260825-001","wait":false}' \
  "$REPOMESH_BASE_URL/v1/repositories/d1436c61-67de-5847-b90e-c87b12d99e49/index"
```

`202 Accepted`:

```json
{
  "id": "3e521693-3013-4cc3-82bb-99a19d14399f",
  "repository_id": "d1436c61-67de-5847-b90e-c87b12d99e49",
  "kind": "index",
  "mode": "incremental",
  "status": "queued",
  "progress_done": 0,
  "progress_total": 0,
  "indexed_files": 0,
  "indexed_chunks": 0,
  "deleted_files": 0,
  "skipped_files": 0,
  "error_count": 0,
  "attempt": 0,
  "idempotency_key": "mac-run-20260825-001",
  "heartbeat_at": null,
  "lease_expires_at": null,
  "cancel_requested": false,
  "error": null,
  "created_at": "2026-08-25T20:21:00.000000+00:00",
  "updated_at": "2026-08-25T20:21:00.000000+00:00"
}
```

Use a unique idempotency key per logical operation and reuse it only when retrying that operation. `wait=true` blocks until terminal and is intended for scripts, not the Mac UI.

### Repository status

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $REPOMESH_API_TOKEN" \
  "$REPOMESH_BASE_URL/v1/repositories/d1436c61-67de-5847-b90e-c87b12d99e49/status"
```

`200 OK`:

```json
{
  "repository": {
    "id": "d1436c61-67de-5847-b90e-c87b12d99e49",
    "name": "Project",
    "root": "<REPOSITORY_PATH>",
    "commit_sha": "fedcba9876543210fedcba9876543210fedcba98",
    "status": "indexed",
    "indexed_files": 42,
    "indexed_chunks": 183,
    "created_at": "2026-08-25T20:20:00.000000+00:00",
    "updated_at": "2026-08-25T20:22:08.000000+00:00"
  },
  "latest_job": {
    "id": "3e521693-3013-4cc3-82bb-99a19d14399f",
    "repository_id": "d1436c61-67de-5847-b90e-c87b12d99e49",
    "kind": "index",
    "mode": "incremental",
    "status": "completed",
    "progress_done": 42,
    "progress_total": 42,
    "indexed_files": 42,
    "indexed_chunks": 183,
    "deleted_files": 0,
    "skipped_files": 0,
    "error_count": 0,
    "attempt": 1,
    "idempotency_key": "mac-run-20260825-001",
    "heartbeat_at": "2026-08-25T20:22:08.000000+00:00",
    "lease_expires_at": null,
    "cancel_requested": false,
    "error": null,
    "created_at": "2026-08-25T20:21:00.000000+00:00",
    "updated_at": "2026-08-25T20:22:08.000000+00:00"
  }
}
```

`latest_job` is `null` before the repository has any job.

### Poll a job

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $REPOMESH_API_TOKEN" \
  "$REPOMESH_BASE_URL/v1/jobs/3e521693-3013-4cc3-82bb-99a19d14399f"
```

`200 OK` returns the complete job object shown above. Terminal states are `completed`, `completed_with_errors`, `failed`, and `cancelled`. Nonterminal jobs expose heartbeat and lease timestamps.

### Cancel a job

```bash
curl --fail --silent --show-error -X POST \
  -H "Authorization: Bearer $REPOMESH_API_TOKEN" \
  "$REPOMESH_BASE_URL/v1/jobs/3e521693-3013-4cc3-82bb-99a19d14399f/cancel"
```

`200 OK` returns the full job object with `cancel_requested: true`; the status becomes `cancelled` after the worker observes the request between files. Repeated cancellation is safe.

### Search

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $REPOMESH_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"repository_id":"d1436c61-67de-5847-b90e-c87b12d99e49","query":"Where is restart recovery implemented?","mode":"hybrid","top_k":3,"lexical_k":20,"vector_k":20}' \
  "$REPOMESH_BASE_URL/v1/search"
```

`200 OK`:

```json
{
  "query": "Where is restart recovery implemented?",
  "mode": "hybrid",
  "repository_id": "d1436c61-67de-5847-b90e-c87b12d99e49",
  "results": [
    {
      "chunk_id": "58e530ffea02800fcbcedcd897d4818a0d2df8b243690cce6aa6e7ef68f30532",
      "repository_id": "d1436c61-67de-5847-b90e-c87b12d99e49",
      "commit_sha": "fedcba9876543210fedcba9876543210fedcba98",
      "file_path": "src/repomesh/jobs.py",
      "language": "python",
      "symbol_name": "recover",
      "symbol_kind": "method",
      "start_line": 41,
      "end_line": 63,
      "content": "def recover(self) -> int:\n    ...",
      "chunking_strategy": "tree_sitter",
      "score": 0.03252247488101534,
      "lexical_score": 5.1407,
      "vector_score": 0.7142,
      "rrf_score": 0.03252247488101534,
      "retrieval_method": "hybrid"
    }
  ],
  "latency_ms": 48.27
}
```

Modes are `lexical`, `dense`, and `hybrid`. Preserve the commit SHA, path, line range, score components, method, and content for every result. A successful no-match search returns an empty `results` array.

### Answer

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $REPOMESH_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"repository_id":"d1436c61-67de-5847-b90e-c87b12d99e49","query":"How does job recovery work?","top_k":6}' \
  "$REPOMESH_BASE_URL/v1/answer"
```

`200 OK` (the `chunks` entries use the full search-result schema above):

```json
{
  "answer": "Expired running jobs are returned to the queue during startup [src/repomesh/jobs.py:41-63].",
  "citations": [
    {
      "chunk_id": "58e530ffea02800fcbcedcd897d4818a0d2df8b243690cce6aa6e7ef68f30532",
      "file_path": "src/repomesh/jobs.py",
      "start_line": 41,
      "end_line": 63
    }
  ],
  "chunks": [
    {
      "chunk_id": "58e530ffea02800fcbcedcd897d4818a0d2df8b243690cce6aa6e7ef68f30532",
      "repository_id": "d1436c61-67de-5847-b90e-c87b12d99e49",
      "commit_sha": "fedcba9876543210fedcba9876543210fedcba98",
      "file_path": "src/repomesh/jobs.py",
      "language": "python",
      "symbol_name": "recover",
      "symbol_kind": "method",
      "start_line": 41,
      "end_line": 63,
      "content": "def recover(self) -> int:\n    ...",
      "chunking_strategy": "tree_sitter",
      "score": 0.03252247488101534,
      "lexical_score": 5.1407,
      "vector_score": 0.7142,
      "rrf_score": 0.03252247488101534,
      "retrieval_method": "hybrid"
    }
  ],
  "evidence_sufficient": true,
  "retrieval_latency_ms": 49.11,
  "generation_latency_ms": 735.44,
  "total_latency_ms": 784.66
}
```

If no source matched, the response says `Evidence insufficient`, has no citations, and sets `evidence_sufficient` to `false`. The Mac should render links from the structured `citations` array and must not invent citations from prose.

### Embed

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $REPOMESH_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"texts":["restart recovery","incremental index"]}' \
  "$REPOMESH_BASE_URL/v1/embed"
```

`200 OK` (vectors are shortened here only to keep the protocol example readable):

```json
{
  "model": "nomic-embed-text",
  "dimensions": 768,
  "vectors": [
    [0.012, -0.031, 0.008],
    [-0.004, 0.027, 0.019]
  ]
}
```

The actual response contains exactly `dimensions` numbers per vector. A request accepts 1 through 128 texts.

### Metrics

```bash
curl --fail --silent --show-error "$REPOMESH_BASE_URL/metrics"
```

`200 OK`, content type `text/plain; version=0.0.4`:

```text
# HELP repomesh_http_requests_total HTTP requests
# TYPE repomesh_http_requests_total counter
repomesh_http_requests_total{method="GET",path="/v1/health",status="200"} 1.0
```

## HTTP status and error contract

Errors normally use FastAPI's JSON envelope:

```json
{"detail":"human-readable error"}
```

Provider failures add the degraded flag:

```json
{"detail":"qdrant is unavailable","degraded":true}
```

| Status | Meaning | Expected Mac action |
|---|---|---|
| `200` | Successful read/search/answer/embed/cancel | Parse the documented response. |
| `201` | Repository registration returned a new or already-registered repository | Store the returned repository ID. |
| `202` | Index job accepted or idempotently returned | Store and poll the returned job ID. |
| `400` | Repository path is outside the allowlist, not a Git root, or otherwise invalid | Show the server detail; do not retry unchanged input. |
| `401` | Missing or invalid token | Stop retries, request credential repair, and never log the supplied token. |
| `404` | Repository or job ID does not exist | Refresh the catalog/job state; do not guess another ID. |
| `409` | Idempotency key conflicts with a different logical request/state | Reconcile the original job or generate a new key for a genuinely new operation. |
| `422` | JSON/schema validation failed, including invalid mode, empty query, bounds, or batch size | Fix the request using the returned validation locations. |
| `503` | Required Ollama or Qdrant provider is unavailable | Mark the node degraded; lexical search may still work, but do not treat this as index corruption. |
| `500` | Unexpected server fault | Preserve request correlation context without secrets, retry only safe/idempotent operations, and inspect Windows logs. |

Example `401 Unauthorized`:

```json
{"detail":"invalid or missing API token"}
```

Example `404 Not Found`:

```json
{"detail":"repository not found"}
```

Example `422 Unprocessable Entity`:

```json
{
  "detail": [
    {
      "type": "greater_than_equal",
      "loc": ["body", "top_k"],
      "msg": "Input should be greater than or equal to 1",
      "input": 0,
      "ctx": {"ge": 1}
    }
  ]
}
```

DNS failure, connection refusal, timeout, and TLS failure have no HTTP status. Report them as node connectivity failures, separately from a reachable node whose health is degraded.

## Mac orchestration sequence

1. Store the base URL as configuration and the token in Keychain or another encrypted secret store.
2. Probe health, then fetch capabilities. Treat connectivity, authentication, and provider degradation as distinct states.
3. List/register a repository and retain the Windows-issued UUID. Never assume the Mac can open the returned Windows root.
4. Submit incremental indexing with a durable Mac-generated idempotency key.
5. Poll the job until terminal, showing progress/error counts and offering cancellation.
6. Run lexical, dense, or hybrid search. Preserve every source and score field.
7. Render answer citations from structured data and link them to the returned chunks.
8. Collect metrics/evaluation history without presenting one Windows machine's benchmark as a universal capacity guarantee.

## Stable Windows API surface

Stable for the first Mac client:

- `GET /v1/health`
- `GET /v1/capabilities`
- `GET /v1/repositories`
- `POST /v1/repositories`
- `POST /v1/repositories/{id}/index`
- `GET /v1/repositories/{id}/status`
- `GET /v1/jobs/{job_id}`
- `POST /v1/jobs/{job_id}/cancel`
- `POST /v1/search`
- `POST /v1/answer`
- `POST /v1/embed`
- `GET /metrics`

The normative machine-readable schemas are always the checked-out node's `/openapi.json`. `docs/PROTOCOL.md` provides the concise protocol summary.

## Windows guarantees the Mac may rely on

- A repeated idempotency key does not create a second index job.
- File hashes and deterministic chunk IDs make repeated incremental indexing duplicate-safe.
- Restart recovery returns unfinished leased work to a resumable state while completed files remain recorded.
- Search always returns source chunks, exact one-based line ranges, commit identity, and score components.
- Answer citations are constrained to retrieved source labels.
- Ollama/Qdrant outages return explicit degradation without taking down health, repository/job, metrics, or lexical operations.

## Intentionally deferred Mac/control-plane work

- Mac-native UI and Keychain-backed credential management;
- node registry, scheduling, node selection, and optional event streaming;
- token rotation/roles or signed requests beyond the current bearer token;
- centralized evaluation history, alerting, and audit export;
- multi-node Qdrant, Kubernetes, public exposure, and HA claims.

Do not duplicate repository filesystem ingestion, chunking, embeddings, or Windows job recovery on the Mac. Those remain compute-plane responsibilities behind this API.
