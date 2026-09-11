# Windows compute-node setup

## Prerequisites

- Windows 11 with PowerShell 7 recommended;
- Git;
- Python 3.12+ (native Windows);
- Docker Desktop using the Linux/WSL2 engine;
- Node.js LTS for Dashboard development only;
- Ollama for real local embeddings/generation.

The application itself runs natively; PostgreSQL and Qdrant run in Docker Desktop (native PostgreSQL is also supported). WSL is optional.

## Install and configure

```powershell
git clone https://github.com/yangchen1234/repomesh.git
Set-Location repomesh
Copy-Item .env.example .env
.\scripts\setup.ps1
.\scripts\start-dependencies.ps1
.\scripts\models.ps1
```

Edit `.env` so `REPOMESH_REPOSITORY_ROOTS` contains only roots the node may read. Do not put API tokens in Git. A semicolon-separated Windows value or JSON array is accepted:

```dotenv
REPOMESH_REPOSITORY_ROOTS=["<REPOSITORY_PATH>"]
REPOMESH_API_TOKEN=<REPOMESH_API_TOKEN>
```

Real providers:

```dotenv
REPOMESH_EMBEDDING_PROVIDER=ollama
REPOMESH_EMBEDDING_MODEL=nomic-embed-text
REPOMESH_EMBEDDING_DIMENSIONS=768
REPOMESH_GENERATION_PROVIDER=ollama
REPOMESH_GENERATION_MODEL=qwen2.5-coder:14b
REPOMESH_VECTOR_PROVIDER=qdrant
```

If you switch embedding dimensions for an existing Qdrant collection, choose a new `REPOMESH_QDRANT_COLLECTION` or recreate only that collection after deliberately preserving any needed index.

## Run

Foreground API and built Dashboard:

```powershell
.\scripts\start-api.ps1
```

Independent worker (required, second terminal):

```powershell
.\scripts\start-worker.ps1
```

Dashboard development server (optional, another terminal):

```powershell
.\scripts\start-dashboard.ps1
```

Dockerized API, PostgreSQL, Qdrant and four workers:

```powershell
docker compose up -d --build --scale worker=4 --wait
```

All published ports remain bound to loopback. The container mounts this project read-only as `/repositories/repomesh`; change volumes and allowlist together for other container-visible repositories.

## Verify

```powershell
Invoke-RestMethod http://127.0.0.1:8787/v1/health
.\scripts\demo.ps1
.\scripts\test.ps1
.\scripts\evaluate.ps1 --embedding-provider ollama --vector-provider qdrant --embedding-dimensions 768
.\scripts\benchmark.ps1 --embedding-provider ollama --vector-provider qdrant --embedding-dimensions 768
docker compose ps
```

## WSL/Linux equivalent

```bash
bash scripts/setup.sh
docker compose up -d --wait postgres qdrant
ollama pull nomic-embed-text
ollama pull qwen2.5-coder:1.5b
REPOMESH_REPOSITORY_ROOTS='["/home/me/source"]' .venv/bin/repomesh serve
# In another terminal: .venv/bin/repomesh worker
REPOMESH_TEST_POSTGRES_DSN="$REPOMESH_POSTGRES_DSN" bash scripts/test.sh
```

## Private network binding

Leave `REPOMESH_HOST=127.0.0.1` for standalone use. For Mac access, set `REPOMESH_HOST=<WINDOWS_TAILSCALE_IP>`, configure `REPOMESH_API_TOKEN=<REPOMESH_API_TOKEN>`, and add a Windows Firewall inbound rule whose remote address is `<MAC_TAILSCALE_IP>`. Never forward port 8787 from a public router. Run `scripts/test-remote-client.py` from the Mac before registering additional repositories.

## Troubleshooting

- `degraded_mode=true`: inspect `postgres_status`, `ollama_status` and `qdrant_status`; lexical search should still work for an existing index.
- Ollama model not found: rerun `scripts/models.ps1` and verify `ollama list`.
- Qdrant unhealthy: run `docker compose logs qdrant` and confirm ports 6333/6334 are unused.
- Repository rejected: register the exact Git top level and include its resolved parent/root in the allowlist.
- Stale running job after a crash: keep at least one independent worker running; it reclaims after lease expiry and resumes incrementally.
- Dashboard unavailable at `/`: run the production build in `dashboard` or rebuild the Docker image.

Set `REPOMESH_POSTGRES_DSN` identically for API/workers. For tests set `REPOMESH_TEST_POSTGRES_DSN`; CI uses real PostgreSQL and temporary schemas. Before upgrading old jobs, stop the old API and run `repomesh import-legacy-jobs`.
