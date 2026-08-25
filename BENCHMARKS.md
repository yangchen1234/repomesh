# RepoMesh benchmarks

## Measurement policy

All numbers below are observed measurements from this Windows computer, not general capacity guarantees. Raw per-case retrieval results and performance data are stored in `benchmarks/results.json`. Rerun the scripts to update them; timestamps, commit SHA, system, providers, dataset size, and errors are recorded with each session.

## Retrieval evaluation

The committed ParcelFlow corpus supplies 25 queries covering single-file, cross-file, symbol, architecture, and implementation-location questions. Gold files and symbols are in `evaluation/cases.json`. The current real-provider run used `nomic-embed-text` through Ollama and Qdrant 1.15.4.

| Mode | Recall@3 | Recall@5 | MRR@10 | nDCG@10 | Hit rate | Mean latency | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| lexical | 0.380 | 0.680 | 0.223 | 0.333 | 0.720 | 1.45 ms | 1.33 ms | 2.15 ms |
| dense | 1.000 | 1.000 | 0.920 | 0.941 | 1.000 | 251.04 ms | 248.01 ms | 285.45 ms |
| hybrid | 0.780 | 0.920 | 0.696 | 0.754 | 0.960 | 253.01 ms | 249.57 ms | 294.96 ms |

## Local performance run

The initial performance harness verification used the deterministic embedding provider and persistent SQLite vector implementation on commit `5154f86f3b63ca52a307828c2feb60ef96dab135`. It indexed 60 files into 316 chunks on Windows 11 build 26200, Intel Core Ultra 9 275HX (24 logical processors), and 31.37 GiB RAM.

| Operation | Observed result |
|---|---:|
| Full index | 0.255 s; 235.11 files/s; 1238.23 chunks/s |
| Incremental add + modify + delete | 0.130 s; 2 indexed, 1 deleted, 58 skipped, 0 errors |
| Incremental one-file change | 0.132 s; 1 indexed, 59 skipped, 1.93× full-wall-time ratio |
| Hybrid query, concurrency 1 | 60.43 queries/s; p50 16.17 ms; p95 18.14 ms |
| Hybrid query, concurrency 5 | 63.38 queries/s; p50 77.00 ms; p95 104.10 ms |
| Hybrid query, concurrency 10 | 58.49 queries/s; p50 174.42 ms; p95 230.91 ms |

The real Ollama/Qdrant performance run should be treated separately from the deterministic baseline because it includes GPU/model and HTTP vector-store costs. The raw result identifies the provider, so reports must not merge the two as equivalent.

## Reproduce

Deterministic baseline:

```powershell
.\scripts\evaluate.ps1
.\scripts\benchmark.ps1
```

Real local providers:

```powershell
.\scripts\evaluate.ps1 --data-dir data/evaluation-real --embedding-provider ollama --vector-provider qdrant --embedding-dimensions 768
.\scripts\benchmark.ps1 --embedding-provider ollama --vector-provider qdrant --embedding-dimensions 768
```

The benchmark uses a temporary local Git clone, performs full indexing, modifies and adds files, deletes a file, runs incremental reconciliation, modifies one additional file, and executes query workloads at concurrency 1, 5, and 10. Temporary changes never touch the source worktree.
