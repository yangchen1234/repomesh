# Contributing

RepoMesh welcomes focused bug fixes, tests, documentation improvements, and compatibility work that preserves its local-first security model.

## Development setup

```powershell
Copy-Item .env.example .env
.\scripts\setup.ps1
.\scripts\test.ps1
```

Linux/WSL equivalents are in `scripts/setup.sh` and `scripts/test.sh`.

## Pull requests

1. Keep changes small and explain the user-visible behavior.
2. Add or update tests for functional changes.
3. Run pytest, Ruff, mypy, Vitest, TypeScript checking, and the production build.
4. Do not commit `.env`, credentials, private paths/addresses, runtime data, model artifacts, generated benchmark sessions, or copied proprietary source code.
5. Do not change committed benchmark values without rerunning the documented harness and retaining its raw output.

By contributing, you agree that your contribution is licensed under the MIT License.
