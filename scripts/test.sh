#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
.venv/bin/python -m pytest
.venv/bin/ruff check src tests evaluation benchmarks scripts/test-remote-client.py
.venv/bin/mypy src/repomesh
(cd dashboard && npm test && npm run build)
