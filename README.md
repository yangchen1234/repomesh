# RepoMesh — Distributed Codebase RAG Platform

RepoMesh is a local-first compute node for source-code retrieval-augmented generation. The current release is a complete Windows single-node vertical slice and a versioned worker API for a future Mac control plane.

It provides:

- Git repository registration behind a configurable root allowlist;
- `.gitignore`, secret, binary, generated-file, and size filtering;
- Tree-sitter symbol chunks for Python, Java, JavaScript, TypeScript, and C++, with line fallback for HTML, CSS, text, and parse failures;
- SHA-256 manifests and idempotent full/incremental add-modify-delete indexing;
- SQLite FTS5 lexical search, pluggable dense vector search, and reciprocal-rank fusion;
- deterministic offline providers plus real Ollama embeddings/generation and Qdrant storage;
- exact `file:line` search results and citation-constrained answers;
- durable jobs with heartbeat, lease, retry, cancellation, idempotency, recovery, and per-file isolation;
- FastAPI/OpenAPI, optional bearer-token authentication, JSON logs, Prometheus metrics, and a responsive dashboard;
- a 25-case retrieval evaluation and repeatable local performance benchmark.

## Quick start on Windows

From PowerShell in the project root:

```powershell
Copy-Item .env.example .env
.\scripts\setup.ps1
.\scripts\start-dependencies.ps1
.\scripts\models.ps1
.\scripts\start-api.ps1
```

The default `.env.example` uses deterministic providers. For real local inference, set:

```dotenv
REPOMESH_EMBEDDING_PROVIDER=ollama
REPOMESH_EMBEDDING_DIMENSIONS=768
REPOMESH_GENERATION_PROVIDER=ollama
REPOMESH_GENERATION_MODEL=qwen2.5-coder:14b
REPOMESH_VECTOR_PROVIDER=qdrant
```

Then open:

- Dashboard: <http://127.0.0.1:8787/>
- API documentation: <http://127.0.0.1:8787/docs>
- Metrics: <http://127.0.0.1:8787/metrics>

Run the complete demo against a running node:

```powershell
.\scripts\demo.ps1
```

## WSL/Linux quick start

```bash
bash scripts/setup.sh
docker compose up -d --wait qdrant
ollama pull nomic-embed-text
ollama pull qwen2.5-coder:1.5b
.venv/bin/repomesh serve
```

The Makefile exposes `setup`, `deps`, `models`, `api`, `dashboard`, `test`, `evaluate`, `benchmark`, `demo`, and `down` targets.

## Search contract

All three retrieval modes return source chunks, never just prose:

```powershell
$body = @{
  repository_id = '<repository-id>'
  query = 'Where is reciprocal rank fusion implemented?'
  mode = 'hybrid'
  top_k = 5
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8787/v1/search -Method Post -ContentType application/json -Body $body
```

Each result includes the commit SHA, normalized repository-relative path, symbol, exact one-based start/end lines, chunk content, individual lexical/vector/RRF scores, and retrieval method. `/v1/answer` uses hybrid retrieval, enforces a context budget, removes unverified citation labels, and returns citations as structured data as well as `[path:start-end]` text.

## Tests, evaluation, and benchmark

```powershell
.\scripts\test.ps1
.\scripts\evaluate.ps1 --embedding-provider ollama --vector-provider qdrant --embedding-dimensions 768
.\scripts\benchmark.ps1 --embedding-provider ollama --vector-provider qdrant --embedding-dimensions 768
```

Raw measurements are regenerated in `benchmarks/results.json`. The current-machine report is in [BENCHMARKS.md](BENCHMARKS.md); README deliberately does not freeze performance claims.

## Security model

- The API and Docker ports bind to `127.0.0.1` by default.
- Repositories must resolve inside `REPOMESH_REPOSITORY_ROOTS`, must be Git roots, and cannot escape through traversal or symlinks.
- Git discovery respects `.gitignore`; common secret names, binaries, generated assets, dependency trees, and large files are excluded.
- Set `REPOMESH_API_TOKEN` before listening beyond loopback. Tokens are accepted as `Authorization: Bearer ...` or `X-API-Token` and are never logged.
- RepoMesh is a trusted single-user node, not a multi-tenant sandbox. Indexed code is sent to the configured local providers.
- LAN/Tailscale exposure should be firewalled to the intended private interface. Public exposure is unsupported.

See [ARCHITECTURE.md](ARCHITECTURE.md), [docs/PROTOCOL.md](docs/PROTOCOL.md), [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md), and [docs/MAC_CONTROL_PLANE_HANDOFF.md](docs/MAC_CONTROL_PLANE_HANDOFF.md).

## Known limitations

- One process owns job execution; SQLite and the default Qdrant collection are node-local. This release does not claim HA or a distributed Qdrant cluster.
- Recovery replays an incremental job. SHA manifests prevent completed files from being embedded again, but there is no cross-node lease coordinator yet.
- Tree-sitter extraction targets common declaration node types; unusual syntax and HTML/CSS use line fallback.
- FTS5 tokenization is identifier-friendly but does not perform language-specific stemming.
- Ollama generation quality and latency depend on the selected local model. Retrieval and evaluation continue without a generator.
- The API token is a single node secret. Rotation, roles, audit export, and multi-tenancy belong to the future control plane.

## Originality and references

RepoMesh's application source, schema, protocol, chunk identifiers, job recovery flow, fusion logic, evaluation corpus, Dashboard, scripts, and documentation were written independently for this project. No source files were copied from the three architecture references named in the project brief. Dependencies remain under their upstream licenses; details are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

RepoMesh is licensed under the MIT License. Dependency licenses are separate.
