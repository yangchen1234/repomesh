# RepoMesh benchmarks

## Measurement policy

Historical tables describe the published Windows session. The distributed section identifies its separate Windows and Linux CI environments; these are not general capacity guarantees. Raw per-case retrieval results and performance data are stored in `benchmarks/results.json`. Rerun the scripts to update them; timestamps, commit SHA, system, providers, dataset size, and errors are recorded with each session.

## Retrieval evaluation

The committed ParcelFlow corpus supplies 25 queries covering single-file, cross-file, symbol, architecture, and implementation-location questions. Gold files and symbols are in `evaluation/cases.json`. The harness constructs a clean temporary Git corpus on every run; this session indexed 15 files into 47 chunks at deterministic fixture commit `5277170f9013c543bf6ca32ad5abe31d5aff814a`. The historical real-provider run used `nomic-embed-text` through Ollama and Qdrant 1.15.4.

| Mode | Recall@3 | Recall@5 | MRR@10 | nDCG@10 | Hit rate | Mean latency | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| lexical | 0.880 | 0.960 | 0.776 | 0.827 | 0.960 | 0.84 ms | 0.43 ms | 2.66 ms |
| dense | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 245.41 ms | 245.71 ms | 284.51 ms |
| hybrid | 1.000 | 1.000 | 0.933 | 0.950 | 1.000 | 256.27 ms | 255.65 ms | 295.60 ms |

## Local performance run

The historical run used `nomic-embed-text` through Ollama and Qdrant 1.15.4 on commit `950d83df042dc06d2c65e6129eb061e45f291f9b`. It indexed 69 files into 442 chunks on Windows 11 build 26200, Intel Core Ultra 9 275HX (24 logical processors), and 31.37 GiB RAM.

| Operation | Observed result |
|---|---:|
| Full index | 28.854 s; 2.39 files/s; 15.32 chunks/s |
| Embedding batches | 2.39 batches/s; one file per embedding call |
| Incremental add + modify + delete | 0.951 s; 2 indexed, 1 deleted, 67 skipped, 0 errors |
| Incremental one-file change | 0.374 s; 1 indexed, 68 skipped, 77.25Ã— full-wall-time ratio |
| Hybrid query, concurrency 1 | 1.25 queries/s; p50 249.40 ms; p95 2319.92 ms |
| Hybrid query, concurrency 5 | 9.37 queries/s; p50 513.07 ms; p95 642.79 ms |
| Hybrid query, concurrency 10 | 10.65 queries/s; p50 935.61 ms; p95 1122.08 ms |

The concurrency-1 p95 contains first-query Ollama warm-up cost and is retained. CPU samples were 16.3% before and 75.9% after the workload; process RSS at the end was 122,957,824 bytes. GPU utilization was not sampled by this harness.

## Reproduce

Deterministic baseline:

```powershell
.\scripts\evaluate.ps1
.\scripts\benchmark.ps1
```

Real local providers:

```powershell
.\scripts\evaluate.ps1 --embedding-provider ollama --vector-provider qdrant --embedding-dimensions 768
.\scripts\benchmark.ps1 --embedding-provider ollama --vector-provider qdrant --embedding-dimensions 768
```

The benchmark uses a temporary local Git clone, performs full indexing, modifies and adds files, deletes a file, runs incremental reconciliation, modifies one additional file, and executes query workloads at concurrency 1, 5, and 10. Temporary changes never touch the source worktree.

## Distributed execution measurements — 2026-09-08

Each scaling level indexed 24 independent Git repositories, each with four copies of the existing 15-file/47-chunk fixture: **1,440 files and 4,512 chunks**. Environment: Windows 11, 24 logical CPUs, 31.37 GiB RAM, Python 3.12.14, PostgreSQL 17.11, deterministic-hash embeddings and the existing SQLite vector test provider (`memory`).

Wall time includes durable submission through completion; worker startup and corpus preparation are excluded. Poll interval is 20ms. There are no inserted processing delays or empty-job throughput claims. One final run per level is reported, without confidence intervals. Raw evidence includes attempt timestamps, worker distribution, system metadata and source SHA-256.

### Worker scaling

| Workers | Wall time | Repositories/s | Files/s | Chunks/s | Completed / failed |
|---:|---:|---:|---:|---:|---:|
| 1 | 10.123s | 2.371 | 142.26 | 445.74 | 24 / 0 |
| 2 | 5.886s | 4.077 | 244.63 | 766.50 | 24 / 0 |
| 4 | 4.358s | 5.507 | 330.45 | 1035.40 | 24 / 0 |
| 8 | 4.656s | 5.154 | 309.27 | 969.04 | 24 / 0 |

Four workers achieved **2.32Ã—** one-worker throughput in this run. Eight were slower than four; no linear-scaling claim is made. Every worker processed jobs. All measured runs had zero overlapping valid job/repository leases. This interval audit is not a universal storage-partition proof.

The preserved deterministic regression harness indexed 89 files/574 chunks in 1.421s, reconciled add/modify/delete in 1.827s and one changed file in 1.827s, with no file errors. Incremental was slower than full in this run with scheduler polling included. The 25-query Recall@5 results were 0.96 lexical, 0.80 dense and 1.00 hybrid. These are separate from the retained historical Ollama/Qdrant results.

### Crash recovery

| Trial | Recovery | Files sampled before kill | Skipped on recovery | Result |
|---:|---:|---:|---:|---|
| 1 | 2.499s | 1 | 2 | completed |
| 2 | 2.497s | 1 | 3 | completed |
| 3 | 2.499s | 1 | 2 | completed |

All three native trials abruptly killed the actual owning interpreter, reclaimed at the next generation and completed 120-file/376-chunk indexes, with unique chunk IDs, no failed jobs and no overlapping valid claims. Persistence can advance between sampling and kill, explaining skips greater than the sampled count. The lease was two seconds.

Linux Compose completed eight jobs with four workers (two each), then killed one worker container during a 300-file job. Generation increased from 1 to 2, 59 files were skipped, the job completed without file errors and dense retrieval worked afterward. Kill-to-recovered-query time was **8.527s** with a five-second lease. This is a deployment/correctness smoke, not a performance comparison with Windows.

Evidence files: `benchmarks/distributed_results.json`, `benchmarks/exclusive_claims.json`, `benchmarks/compose_results.json`, `benchmarks/distributed_regression.json`.


The full [engineering report](docs/DISTRIBUTED_REPORT.md) documents the implementation, tests, limitations and reproduction commands. [CI run 34291592744](https://github.com/yangchen1234/repomesh/actions/runs/34291592744) verified four real worker containers with PostgreSQL and Qdrant. The local benchmark source commit is `9a642c6c5d83caca63642391b48df2e29223dae4`. Raw measurements retain the corresponding source hash.

### Reproduce

With PostgreSQL configured through `REPOMESH_POSTGRES_DSN` and `.[dev]` installed:

```bash
python -m benchmarks.distributed_jobs --workers 1 2 4 8 --repositories 24 --copies 4 --crash-trials 3
python -m evaluation.run --output benchmarks/distributed_regression.json
python -m benchmarks.run --output benchmarks/distributed_regression.json
```

Set `REPOMESH_TEST_POSTGRES_DSN` to run PostgreSQL tests and `REPOMESH_CLAIM_AUDIT_PATH=benchmarks/exclusive_claims.json` to retain the 1,000-claim trace. The database role must be allowed to create/drop isolated test schemas. Real Ollama scaling, arbitrary multi-host storage and independent storage-partition experiments were not executed.
