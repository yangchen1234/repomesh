#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
(cd dashboard && npm install)

