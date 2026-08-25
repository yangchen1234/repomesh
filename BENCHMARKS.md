# RepoMesh benchmarks

## Measurement policy

All numbers below are observed measurements from this Windows computer, not general capacity guarantees. Raw per-case retrieval results and performance data are stored in `benchmarks/results.json`. Rerun the scripts to update them; timestamps, commit SHA, system, providers, dataset size, and errors are recorded with each session.

## Retrieval evaluation

The committed ParcelFlow corpus supplies 25 queries covering single-file, cross-file, symbol, architecture, and implementation-location questions. Gold files and symbols are in `evaluation/cases.json`. The harness constructs a clean temporary Git corpus on every run; this session indexed 15 files into 47 chunks at deterministic fixture commit `5277170f9013c543bf6ca32ad5abe31d5aff814a`. The current real-provider run used `nomic-embed-text` through Ollama and Qdrant 1.15.4.

| Mode | Recall@3 | Recall@5 | MRR@10 | nDCG@10 | Hit rate | Mean latency | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| lexical | 0.880 | 0.960 | 0.776 | 0.827 | 0.960 | 0.84 ms | 0.43 ms | 2.66 ms |
| dense | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 245.41 ms | 245.71 ms | 284.51 ms |
| hybrid | 1.000 | 1.000 | 0.933 | 0.950 | 1.000 | 256.27 ms | 255.65 ms | 295.60 ms |

## Local performance run

The current run used `nomic-embed-text` through Ollama and Qdrant 1.15.4 on commit `950d83df042dc06d2c65e6129eb061e45f291f9b`. It indexed 69 files into 442 chunks on Windows 11 build 26200, Intel Core Ultra 9 275HX (24 logical processors), and 31.37 GiB RAM.

| Operation | Observed result |
|---|---:|
| Full index | 28.854 s; 2.39 files/s; 15.32 chunks/s |
| Embedding batches | 2.39 batches/s; one file per embedding call |
| Incremental add + modify + delete | 0.951 s; 2 indexed, 1 deleted, 67 skipped, 0 errors |
| Incremental one-file change | 0.374 s; 1 indexed, 68 skipped, 77.25× full-wall-time ratio |
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
