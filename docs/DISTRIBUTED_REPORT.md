# RepoMesh distributed worker engineering report

Implementation: [draft PR #2](https://github.com/yangchen1234/repomesh/pull/2), branch `codex/distributed-workers`, source commit `9a642c6c5d83caca63642391b48df2e29223dae4`. The design document was written before code changes, against original commit `cb5c2e7`.

## 1. Architecture before

FastAPI owned a `JobManager` with `ThreadPoolExecutor(max_workers=2)`. SQLite held jobs, startup resubmitted unfinished work to local threads, and retries slept inside those threads. This was durable single-node asynchronous execution, not distributed execution.

## 2. Architecture after

FastAPI is the control plane: submit, status, cancellation, worker discovery, health, metrics, retrieval and answers. It owns no indexing executor. Independent `repomesh worker` processes compete through PostgreSQL and run `RepositoryIndexer`. A heartbeat thread renews each running lease during slow embedding calls. SQLite retains repository metadata, manifests, chunks, FTS5 and query logs. Qdrant remains the vector store. Compose scales independent workers sharing repository mounts and a local SQLite volume.

## 3. Files changed

- Coordination: `src/repomesh/coordination/{__init__.py,repository.py,metrics.py,migrations/001_coordination.sql}`.
- Execution/control plane: `src/repomesh/{worker.py,jobs.py,services.py,api.py,cli.py,config.py,models.py}`.
- Storage/ingestion: `src/repomesh/{db.py,indexing.py,providers.py,repositories.py}`.
- Deployment: `compose.yaml`, `pyproject.toml`, `.env.example`, `.gitattributes`, `.gitignore`, `Makefile`, `scripts/start-dependencies.ps1`, `scripts/start-worker.ps1`, `.github/workflows/ci.yml`.
- Tests: `tests/{conftest.py,test_core.py,test_distributed.py,test_worker_integration.py,test_coordination_edges.py}`, `scripts/test-distributed-compose.py`.
- Measurement: `benchmarks/{harness.py,distributed_jobs.py,run.py}`, `evaluation/run.py`, and the four evidence JSON files listed below.
- Documentation: `README.md`, `ARCHITECTURE.md`, `BENCHMARKS.md`, `docs/{DISTRIBUTED_DESIGN.md,DISTRIBUTED_REPORT.md,PROTOCOL.md,WINDOWS_SETUP.md}`.

The Dockerfile, chunker, retrieval implementation, fixture/evaluation corpus, and historical `benchmarks/results.json` remain intact.

## 4. Schema and migrations

Ordered SQL migration `001_coordination.sql` creates `repository_execution`, `jobs`, `workers`, `job_attempts`, and `job_file_errors`. `schema_migrations` records application under a PostgreSQL advisory transaction lock. The built wheel includes the SQL migration.

Jobs include the requested scheduling, ownership, lease, progress, retry and timestamp fields. Partial indexes support runnable jobs, active leases, and one running job per repository. Idempotency keys are unique. Repository-job and worker-freshness indexes support lookup. SQLite gains durable `pending_vector_deletions`, and existing vector-sync markers are set before replacement.

`repomesh import-legacy-jobs` is explicit and repeatable: stop the old API, import terminal history and queue unfinished work for incremental recovery. Existing SQLite job tables remain historical, not a second live scheduler.

## 5. Distributed guarantees

Claims lock job and repository rows with `FOR UPDATE SKIP LOCKED`, then assign owner, generation, attempt and repository reservation atomically. All worker writes require the matching job, worker, generation, running state and unexpired lease; rejection raises `LeaseLost`. Expired ownership cannot be resurrected merely because nobody reclaimed it yet.

Repository reservations and a partial unique index prevent two running jobs for one repository. Storage mutations also lock the repository row and recheck ownership, so claimants skip a repository during guarded writes. Discovery, parsing and embedding run outside transactions. Different repositories can execute concurrently.

Unique keys and conflict-safe insertion protect concurrent submission. Retries persist capped exponential backoff with jitter and release ownership immediately. Cancellation is durable and terminal once acknowledged. Registry session tokens fence obsolete writers and reject active identity reuse; heartbeat age determines offline status.

These guarantees use the documented shared-host process-crash model. They do not constitute an atomic commit or storage-side fencing protocol across PostgreSQL, SQLite and Qdrant under arbitrary network partitions.

## 6. Delivery semantics

**At-least-once job execution with idempotent indexing effects**, not exactly-once. SHA-256 manifests, deterministic chunk IDs, duplicate-safe replacement, vector upsert and incremental reconciliation make repeated work convergent. Reclaimed jobs resume incrementally. Progress counters describe the current attempt; manifests preserve previous attempts' successful files.

Per-file isolation remains: file failures may end as `completed_with_errors`; unhandled job failures use durable retries. Vector replacement first marks an existing manifest unsynced, and deletion records a durable tombstone, preserving repair work across crashes and provider outages.

## 7. Tests executed

Local Windows: full pytest against real PostgreSQL 17.11; Ruff; mypy; dashboard Vitest, TypeScript and Vite build; wheel build and migration-content check; eight-process claim stress; preserved evaluation/performance scripts; 1/2/4/8-worker indexing; three abrupt-kill trials.

Linux CI: all quality gates, Docker image build, PostgreSQL/Qdrant/API/four workers, participation of every worker, lexical/dense/hybrid retrieval and worker-container kill/recovery.

## 8. Test results

**54 backend tests passed locally (38.68s) and in Linux CI (23.58s).** Ruff and mypy passed; mypy checked 17 source files. Both dashboard tests, type check and production build passed. The wheel contains the migration.

The separate correctness replay recorded all 1,000 claims from eight worker identities: 1,000 completions and zero duplicate active claims. Tests cover stale authoritative writes, lease expiry/reclaim, blocked-mutation takeover, repository exclusion, concurrent idempotency, retries/exhaustion, queued/running cancellation, heartbeat during blocked embedding, registry fencing, migration/import, vector repair, metrics and existing RAG/security behavior.

[CI run 34291592744](https://github.com/yangchen1234/repomesh/actions/runs/34291592744) passed both `quality` and `compose-smoke` on the implementation commit. Two upstream test deprecation warnings were emitted. The unchanged frontend lockfile's install reported two moderate audit advisories; no forced dependency upgrade was introduced.

## 9. Benchmark setup and results

Each scaling level indexed 24 independent Git repositories, each with four copies of the existing 15-file/47-chunk fixture: **1,440 files and 4,512 chunks**. Environment: Windows 11, 24 logical CPUs, 31.37 GiB RAM, Python 3.12.14, PostgreSQL 17.11, deterministic-hash embeddings and the existing SQLite vector test provider (`memory`).

Wall time includes durable submission through completion; worker startup and corpus preparation are excluded. Poll interval is 20ms. There are no inserted processing delays or empty-job throughput claims. One final run per level is reported, without confidence intervals. Raw evidence includes attempt timestamps, worker distribution, system metadata and source SHA-256.

## 10. Worker scaling

| Workers | Wall time | Repositories/s | Files/s | Chunks/s | Completed / failed |
|---:|---:|---:|---:|---:|---:|
| 1 | 10.123s | 2.371 | 142.26 | 445.74 | 24 / 0 |
| 2 | 5.886s | 4.077 | 244.63 | 766.50 | 24 / 0 |
| 4 | 4.358s | 5.507 | 330.45 | 1035.40 | 24 / 0 |
| 8 | 4.656s | 5.154 | 309.27 | 969.04 | 24 / 0 |

Four workers achieved **2.32×** one-worker throughput in this run. Eight were slower than four; no linear-scaling claim is made. Every worker processed jobs. All measured runs had zero overlapping valid job/repository leases. This interval audit is not a universal storage-partition proof.

The preserved deterministic regression harness indexed 89 files/574 chunks in 1.421s, reconciled add/modify/delete in 1.827s and one changed file in 1.827s, with no file errors. Incremental was slower than full in this run with scheduler polling included. The 25-query Recall@5 results were 0.96 lexical, 0.80 dense and 1.00 hybrid. These are separate from the retained historical Ollama/Qdrant results.

## 11. Crash recovery

| Trial | Recovery | Files sampled before kill | Skipped on recovery | Result |
|---:|---:|---:|---:|---|
| 1 | 2.499s | 1 | 2 | completed |
| 2 | 2.497s | 1 | 3 | completed |
| 3 | 2.499s | 1 | 2 | completed |

All three native trials abruptly killed the actual owning interpreter, reclaimed at the next generation and completed 120-file/376-chunk indexes, with unique chunk IDs, no failed jobs and no overlapping valid claims. Persistence can advance between sampling and kill, explaining skips greater than the sampled count. The lease was two seconds.

Linux Compose completed eight jobs with four workers (two each), then killed one worker container during a 300-file job. Generation increased from 1 to 2, 59 files were skipped, the job completed without file errors and dense retrieval worked afterward. Kill-to-recovered-query time was **8.527s** with a five-second lease. This is a deployment/correctness smoke, not a performance comparison with Windows.

Evidence files: `benchmarks/distributed_results.json`, `benchmarks/exclusive_claims.json`, `benchmarks/compose_results.json`, `benchmarks/distributed_regression.json`.

## 12. Limitations and experiments not executed

- Workers share local repository paths and one SQLite WAL volume. Arbitrary cloud-machine repository distribution and SQLite WAL over NFS/SMB are unsupported; Qdrant is not clustered.
- A process frozen inside a guarded mutation delays takeover until the PostgreSQL transaction ends or connection closes.
- PostgreSQL cannot revoke an external Qdrant request already in flight after independent session loss. Stronger network-partition guarantees need storage-side fencing.
- Attempts are finite; repeated crashes eventually fail a job. There is no automatic attempt-history retention or starvation guarantee for lower-priority work.
- Real Ollama scaling, arbitrary multi-host storage, independent storage-partition injection and multi-region recovery were not executed. API crash injection was not a separate fault benchmark; startup/control-plane separation is tested.
- Docker is unavailable on this Windows environment. The container build, four-worker launch, real Qdrant retrieval and container kill were executed successfully in Linux CI instead.

## 13. Run the system

```bash
docker compose up --build --scale worker=4
```

Register `/repositories/repomesh` with the default mount. For native use, give every process the same PostgreSQL DSN, data directory and allowlisted repository roots, then run in separate terminals:

```bash
repomesh migrate
repomesh serve
repomesh worker --worker-id worker-a
repomesh worker --worker-id worker-b
```

Default identity is `hostname:pid:uuid`; defaults are 30-second leases, 10-second heartbeats and one-second polling. `GET /v1/workers` exposes the registry and `/metrics` exposes shared counters/gauges and attempt durations. `wait=true` polls PostgreSQL for up to 300 seconds and returns the latest job; it never executes indexing. Stop the old API before running `repomesh import-legacy-jobs`.

## 14. Reproduce tests and benchmarks

Install `.[dev]`, start PostgreSQL, and supply a role allowed to create/drop isolated test/benchmark schemas.

```powershell
$env:REPOMESH_POSTGRES_DSN = "postgresql://repomesh:repomesh@127.0.0.1:5432/repomesh"
$env:REPOMESH_TEST_POSTGRES_DSN = $env:REPOMESH_POSTGRES_DSN
$env:REPOMESH_CLAIM_AUDIT_PATH = "benchmarks/exclusive_claims.json"
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m benchmarks.distributed_jobs --workers 1 2 4 8 --repositories 24 --copies 4 --crash-trials 3
.venv/Scripts/python.exe -m evaluation.run --output benchmarks/distributed_regression.json
.venv/Scripts/python.exe -m benchmarks.run --output benchmarks/distributed_regression.json
```

Run `python scripts/test-distributed-compose.py` after the four-worker container launch in a disposable deployment: it creates ignored fixture copies and deliberately kills one worker. Historical measurements and the fixture corpus remain intact. The implementation and measured evidence are committed on the PR branch; main remains unchanged pending review.
