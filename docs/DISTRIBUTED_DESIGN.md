# Distributed execution design

Design recorded before implementation, against `cb5c2e7`.

## Audit and scope

The old `Services.create()` constructs a `JobManager` containing a
`ThreadPoolExecutor(max_workers=2)`. FastAPI startup calls `recover()`, submission
schedules a future, and `wait()` waits on that future. SQLite stores jobs and
file errors; retry delays sleep inside executor threads. This is durable
single-node asynchronous execution, not distributed execution.

The existing Git discovery/filtering, Tree-sitter/line chunking, SHA-256 manifests,
deterministic chunk IDs, FTS5, Qdrant, RRF, citations, authentication, dashboard,
provider degradation, evaluation corpus, and published historical measurements
remain. Audited README, ARCHITECTURE, BENCHMARKS, Compose, Dockerfile, packaging,
all service modules, core/security tests, and benchmark/evaluation harnesses.

## Planes and deployment boundary

```text
Clients -> FastAPI control plane -> PostgreSQL coordination
                                      ^ claim/renew/status
                                independent worker processes
                                      | RepositoryIndexer
                               SQLite FTS5 + Qdrant
```

FastAPI registers repositories, submits/cancels/reads jobs, lists workers, serves
health/metrics, retrieves, and answers. It never executes indexing. `wait=true`
polls persisted status with a bounded timeout and returns the latest job.
`repomesh worker` is a separate process, one active job per process, default
identity `hostname:pid:uuid`. Scale with multiple CLI processes or Compose replicas.

PostgreSQL owns jobs, ownership, worker registry, retry timing, idempotency,
file errors, attempt history, and repository execution rows. SQLite retains
repository metadata, file manifests, chunks, FTS5 and query logs. PostgreSQL
stores only repository IDs for coordination, not another copy of repository paths.
Qdrant remains the vector store; the existing SQLite vector test provider remains.

The initial supported deployment is independent processes/containers on a single
host sharing the SAME local repository mounts and SQLite data volume. SQLite WAL
requires local shared memory/locking semantics: do not put the database on NFS/SMB
or claim arbitrary machines can read another machine's local paths. PostgreSQL
may be remote. This distributes execution/coordination, not SQLite or Qdrant.

## Durable state machine

* `queued -> running` when claimed; `queued -> cancelled` on cancellation.
* `retrying -> running` only after `available_at`; `retrying -> cancelled`.
* `running -> completed | completed_with_errors` after successful indexing.
* `running -> retrying` on failure with attempts remaining; else `failed`.
* `running -> cancelled` after cooperative cancellation or expired cancellation.
* Expired `running -> running` is a new claim/generation/attempt, incrementally
  recovering prior work. Exhausted expired work becomes `failed`.
* Terminal states never become runnable and cannot be changed by a stale worker.

`attempt` counts acquired executions, including crashes. `max_attempts` is a
durable per-job budget. Timestamps and lease comparisons use PostgreSQL's clock.

## Ownership, claims, leases and fencing

Versioned SQL migrations run deterministically under a PostgreSQL advisory
transaction lock. Claims use `FOR UPDATE SKIP LOCKED` on the candidate job AND
its repository execution row. Ownership and the repository's active job are
changed in the same short transaction. A partial unique index on running jobs
provides a second check against two active jobs for one repository.

Every claim increments `lease_generation`, assigns `worker_id`, increments
attempt, and sets heartbeat/expiry (30 seconds by default). A separate heartbeat
thread renews every 10 seconds even during a slow embedding call; idle workers
also heartbeat. Poll interval defaults to one second. Worker IDs cannot be reused
while their previous registry session is live; registry session tokens fence old
processes after ID reuse.

Heartbeat, progress, completion, cancellation acknowledgement, failure and retry
updates require job ID, worker ID, generation, running status AND an unexpired
lease. Zero matched rows raises `LeaseLost`. A lease is never resurrected merely
because no replacement worker has claimed it yet. On coordination failure workers
stop at the next safe boundary and leave recovery to lease expiry.

## Repository exclusion and indexing side effects

A repository execution row reserves the repository for its running job. Another
job for that repository cannot start until release/recovery. Different repositories
can execute concurrently. There are no process-local scheduler locks.

Fencing status alone does not fence external storage. Every indexing mutation
therefore enters a PostgreSQL transaction that locks the repository row and
checks live ownership before side effects. A claimant skips a locked repository,
so takeover cannot race a guarded mutation. Ownership is checked again between
provider and SQLite calls. Embedding/discovery/parsing occur outside these guards;
transactions never span a full job. Guards cover only per-file storage operations
and repository status/provenance updates; provider requests have bounded timeouts.
Cancellation and lease loss must escape per-file error isolation immediately.

A paused process outside a guard can be reclaimed, but cannot enter a guard when
it resumes. A process paused inside a guard delays takeover until its transaction
finishes or the connection closes: exclusion takes priority over immediate
recovery. Kill/crash closes its connections and allows expiry-based takeover.

SQLite and Qdrant do not participate in a PostgreSQL transaction. This is not a
cross-store atomic commit or storage-side fencing protocol under arbitrary
network partitions (e.g. PostgreSQL loses a session while Qdrant retains a request
already in flight). The supported shared-host crash model, rechecks and durable
repair markers cover process crashes/retries; distributed storage with independent
partitions would require a storage-side fencing protocol before stronger claims.

Mark an existing file vector-unsynced before replacing vectors. Retain durable
pending vector deletion records when deleting files. This closes crash windows
where a later incremental pass could otherwise skip an interrupted vector write
or forget an offline deletion. Retrieval still validates vector hits against SQLite.

## Delivery, retries, idempotency and cancellation

Delivery is **at-least-once job execution with idempotent indexing effects**,
never exactly-once. SHA manifests, deterministic chunk IDs, replacement, vector
upsert and incremental reconciliation make retries convergent. Reclaimed jobs
resume incrementally even if the original request was full.

Retries release ownership and persist `status=retrying`, future `available_at`,
and error. Backoff is exponential with bounded jitter and a configured cap.
Workers claim other work instead of sleeping through a retry delay.

A global unique idempotency key plus `INSERT ... ON CONFLICT DO NOTHING` makes
concurrent identical submissions return one logical job. Reuse for different
repository/mode/priority is rejected. Empty keys are rejected at validation.

API cancellation is a durable flag. Waiting/retrying jobs cancel immediately;
running jobs observe it through heartbeat/checkpoints and terminate cooperatively.
Expired cancelled work becomes terminal without running indexing again.

## Failure behavior and observability

* Worker crash: lease expires; another worker reclaims and increments generation.
* API crash: workers continue independently; PostgreSQL status survives restart.
* PostgreSQL outage: no invented ownership; coordination APIs return 503, health
  reports degradation, workers stop authoritative work and retry polling later.
* Qdrant outage: lexical state remains available; deferred synchronization/deletion
  is durable. Dense/hybrid/answer retain existing provider-specific 503 behavior.
* Ollama outage: preserve per-file isolation and provider-specific degraded APIs.
* Stale worker resumes: fenced writes fail; no completion or retry from old owner.

Attempt history records worker, generation, claim/end/lease times, outcome and
duration for auditing. Metrics are read from shared PostgreSQL state so an API
scrape includes every worker. Labels are bounded and never contain job/repository
IDs. Logs contain event, job, repository, worker, attempt and generation when
relevant. Registry freshness determines offline status, including crashed workers.

## Verification and migration

Real PostgreSQL tests will exercise competing processes, idempotency races,
lease expiry, all fenced updates, repository exclusion, retries, cancellation,
registry identity and stale mutation rejection. A real indexing benchmark uses
independent fixture copies at 1/2/4/8 workers. Fault injection kills a worker
abruptly and records recovery; raw JSON and honest limitations accompany results.
Existing RAG tests/evaluations and formatting/type/frontend gates remain.

Existing SQLite job tables are historical data, not a second live scheduler.
An explicit one-time import command will copy old jobs into PostgreSQL using
conflict-safe insertion, preserving terminal history and queueing unfinished
work for incremental recovery. Stop the old API before importing; do not run
old and new schedulers against the same SQLite/Qdrant state.

Implementation references: [PostgreSQL locking](https://www.postgresql.org/docs/17/explicit-locking.html),
[SKIP LOCKED](https://www.postgresql.org/docs/17/sql-select.html), and
[Psycopg connection pools](https://www.psycopg.org/psycopg3/docs/advanced/pool.html).
