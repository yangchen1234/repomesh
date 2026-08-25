# Mac control-plane handoff

This document is the implementation handoff for a Codex session on the future Mac control plane.

## Start the Windows node

On Windows, from the RepoMesh project root:

```powershell
.\scripts\start-dependencies.ps1
.\scripts\models.ps1
.\scripts\start-api.ps1
```

For a real node, configure Ollama embedding/generation, Qdrant, an allowlisted repository root, and a long API token in the Windows `.env`. The process defaults to `127.0.0.1:8787`; change `REPOMESH_HOST` only when private-network access is ready.

## Mac configuration

Store these as control-plane secrets/configuration, never source constants:

```text
REPOMESH_WINDOWS_BASE_URL=http://<windows-lan-or-tailscale-address>:8787
REPOMESH_WINDOWS_API_TOKEN=<same token configured on Windows>
```

Every protected call sends `Authorization: Bearer <token>`. Do not log request headers or token-bearing URLs.

## Health check

```bash
curl --fail --silent --show-error "$REPOMESH_WINDOWS_BASE_URL/v1/health"
```

Read `degraded_mode`, `qdrant_status`, `ollama_status`, model names, and job counts. A reachable degraded node may still serve lexical search. Treat network failure separately from provider degradation.

## Stable API contract

The following endpoints are stable for the first Mac client:

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

The normative schemas, states, errors, examples, citation/line rules, and compatibility policy are in `docs/PROTOCOL.md` and the live `/openapi.json`.

## What the Mac control plane should implement

1. Node configuration and encrypted token storage.
2. Health/capability polling with clear offline vs degraded presentation.
3. Repository catalog using Windows-returned IDs and paths; do not assume the Mac can read Windows paths.
4. Index orchestration with a unique idempotency key per logical request, durable local tracking, job polling, cancellation, and retry presentation.
5. Lexical/dense/hybrid search controls that preserve every returned commit SHA, path, line range, score, and chunk.
6. Answer rendering that uses the structured citation array as the authority and links citations back to returned chunk content.
7. Evaluation/benchmark session history and comparison UI, without turning one machine's measurements into capacity promises.
8. Metrics collection/visualization and user-readable provider errors.

Do not duplicate Windows ingestion, chunking, embeddings, or repository filesystem access on the Mac. The compute-node API owns those concerns.

## LAN or Tailscale connection

Preferred order:

1. Install/verify Tailscale on both machines or use a trusted private LAN.
2. Set Windows `REPOMESH_HOST` to the intended private interface address or `0.0.0.0` only when the firewall is scoped correctly.
3. Set `REPOMESH_API_TOKEN`.
4. Allow inbound TCP 8787 only on the private/Tailscale profile and, where practical, only from the Mac.
5. Use the exact private address in the Mac base URL and run the health check.

TLS is not terminated by RepoMesh v0.1. Tailscale encryption is the recommended transport; otherwise put a private reverse proxy with TLS in front. Do not expose the node to the public Internet.

## Current guarantees

- Idempotency keys do not create a second job.
- File SHA manifests make recovery and repeated incremental indexing duplicate-safe.
- Paths are normalized repository-relative values; lines are one-based inclusive.
- Search always returns source chunks and score components.
- Answer citations are limited to retrieved source labels.
- Provider outages produce explicit 503 responses without taking down health, repository, job, metrics, or lexical endpoints.

## Remaining control-plane TODOs

These are intentionally outside the Windows vertical slice, not missing Windows buttons/endpoints:

- Mac-native application/UI and secure credential storage;
- multi-node registry, scheduling policy, and node selection;
- optional event streaming instead of polling;
- token rotation/roles and signed request protocol;
- centralized evaluation history, alerting, and audit export;
- any future HA design. Do not describe the current system as two-node HA or a Qdrant cluster.
