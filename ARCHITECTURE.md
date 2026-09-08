# RepoMesh architecture

## Control, coordination, compute and storage

FastAPI is the control plane: registration, job submission/status/cancellation,
worker discovery, health, metrics, search, embedding and cited answers. It has no
indexing executor; `wait=true` polls durable state. Each `repomesh worker` process
owns one execution slot and a heartbeat thread.

PostgreSQL owns jobs, atomic `FOR UPDATE SKIP LOCKED` claims, worker registry,
leases, generation fencing, durable retries, idempotency, repository reservations
and attempt/file-error history. SQLite retains repository metadata, manifests,
chunks, FTS5, query logs and vector repair records. Qdrant retains vector search.

```text
Clients -> FastAPI -> PostgreSQL <- independent workers A / B / C
              |                            |
              |                      RepositoryIndexer
              +-----> SQLite FTS5 <--------+------> Qdrant
```

The supported initial deployment uses independent processes or containers with
shared local repository mounts and one local SQLite volume. PostgreSQL may be
remote. SQLite WAL is not supported on NFS/SMB; this does not distribute local
Windows paths or turn Qdrant into a cluster.

The previous `JobManager` used two threads inside FastAPI and SQLite job state.
That implementation was durable single-node asynchronous execution. See
[the pre-implementation design](docs/DISTRIBUTED_DESIGN.md) for the transition,
schema, state machine and detailed failure assumptions.

## Ingestion and identity

Registration resolves the supplied path, checks it against every configured allowlist root, verifies that it is the Git top-level directory, and stores a deterministic repository UUID. Discovery uses `git ls-files --cached --others --exclude-standard`, then applies defense-in-depth filters for dependency/build directories, secret filenames, binary extensions/content, generated suffixes, and file size.

File state is `(repository_id, relative_path, sha256, commit_sha, vector_synced)`. Chunk identity is UUIDv5 over repository ID, normalized path, exact line range, and content SHA-256. Qdrant is updated idempotently before the SQLite manifest transaction is committed; an unavailable store leaves `vector_synced=false`, forcing a later incremental retry instead of silently skipping the file. Replacing a changed file deletes its old SQLite chunks, inserts new chunks, and upserts its manifest in one transaction. Full reindex therefore replaces per-file data without duplicates; incremental indexing skips matching, vector-synced hashes and explicitly removes deleted paths.

## Chunking

Tree-sitter walks declaration nodes for:

- Python: functions and classes;
- Java: classes, interfaces, methods, constructors;
- JavaScript: functions, generators, classes, methods;
- TypeScript: JavaScript declarations plus interfaces, types, and enums;
- C++: functions, classes, structs, and namespaces.

Oversized symbols are divided into overlapping line windows and marked `tree-sitter-split`. Unsupported or failed parses use `line-fallback`. Every stored chunk includes repository/commit identity, path/language, symbol/kind, one-based inclusive lines, content and hash, reliable import/include lines, model/version, strategy, and vector.

## Retrieval and answers

Lexical retrieval uses a content-linked SQLite FTS5 table. Dense retrieval embeds the query and calls the selected vector-store interface. Hybrid retrieval independently requests the configured lexical and vector candidate counts, then calculates `sum(1 / (rrf_k + rank))`. Search responses retain component scores.

Answers receive hybrid chunks until the character budget is exhausted. The generator is told to use exact supplied source labels. The API removes any citation-looking label not in the retrieved set and appends verified labels if the model omitted them. The response includes both answer text and structured citation/chunk arrays.

## Job reliability

Delivery is **at-least-once job execution with idempotent indexing effects**.
There is no exactly-once execution claim. Submission uses a PostgreSQL unique
idempotency key and conflict-safe insertion. Queued/retrying jobs are claimable
only after `available_at`; a running job is reclaimable after expiry. Each claim
increments both attempt and generation. All worker writes require matching job,
worker, generation, running state and an unexpired lease. Rejected writes raise
`LeaseLost`; even an unreclaimed expired lease cannot be renewed.

Independent heartbeats renew leases during slow embedding calls. Retry scheduling
releases ownership immediately and persists capped exponential backoff with
jitter. Cancellation is a PostgreSQL flag, observed between stages and before
storage mutations. Terminal jobs cannot be reclaimed; exhausted crash/retry
attempts fail durably.

A repository execution row plus a partial unique running-job index prevents
concurrent jobs for one repository while allowing different repositories in
parallel. Per-file mutation guards lock that row and verify ownership, so an
expired worker cannot write after another worker takes over. Claimants skip a
repository while its guarded storage operations are in progress. Discovery,
chunking and embedding never hold these transactions. A frozen process inside
a mutation guard delays takeover until its transaction ends or connection closes.

Before replacing vectors, an existing manifest is marked unsynced. Deletion
stores a durable tombstone before removing lexical data; failed vector deletions
are retried by incremental passes. This preserves repair work across crashes.
FTS5 remains usable during Qdrant outages, and retrieval validates vector hits
against existing SQLite chunks.

## Failure domains

- Worker crash/kill: connection closes, lease expires, another process reclaims
  incrementally. Previously manifested files are skipped.
- API crash: already claimed jobs continue in independent processes; status
  survives in PostgreSQL.
- PostgreSQL outage: claims and status fail safely; APIs return 503 and health
  reports degradation. Workers stop authoritative writes at checkpoints.
- Qdrant outage: lexical data remains available; vector synchronization and
  deletion remain pending. Dense/hybrid/answer retain provider-specific errors.
- Ollama outage: preserve per-file isolation and provider-specific degraded APIs.
- Stale worker: ownership checks reject heartbeat, progress, terminal/retry writes
  and repository mutation entry.
- Independent storage partition: PostgreSQL cannot atomically revoke a Qdrant
  request already in flight after losing its session. The supported process-crash
  model is not a storage-side fencing protocol for arbitrary network partitions.
  Stronger partition guarantees require storage-side fencing before expanding
  beyond the shared-host model.

## Operations

`compose.yaml` separates PostgreSQL, Qdrant, API and a scalable worker service.
`GET /v1/workers` reports registry identity, process, heartbeat, job and counts;
old heartbeats derive `offline`. Shared-state metrics expose claims, completion,
failure, retries, reclaim, attempt-duration histogram, queue depth, active jobs,
workers and leases. Job IDs never appear in Prometheus labels; HTTP labels use
route templates. Per-file progress logs are debug-level; lifecycle/lease events
are structured logs. Attempt history has no automatic retention policy yet.

Run `repomesh import-legacy-jobs` once after stopping the old API. Existing SQLite
index data is reused, terminal job history is imported, and unfinished jobs resume
incrementally. This does not permit simultaneous old/new schedulers.
