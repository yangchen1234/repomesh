#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base_url="${REPOMESH_BASE_URL:-http://127.0.0.1:8787}"
auth=()
if [[ -n "${REPOMESH_API_TOKEN:-}" ]]; then auth=(-H "Authorization: Bearer ${REPOMESH_API_TOKEN}"); fi
curl -fsS "${auth[@]}" "$base_url/v1/health"
repo_json="$(curl -fsS "${auth[@]}" -H 'Content-Type: application/json' -d "{\"path\":\"$project_root\"}" "$base_url/v1/repositories")"
repo_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$repo_json")"
curl -fsS "${auth[@]}" -H 'Content-Type: application/json' -d '{"mode":"incremental","wait":true}' "$base_url/v1/repositories/$repo_id/index"
curl -fsS "${auth[@]}" -H 'Content-Type: application/json' -d "{\"repository_id\":\"$repo_id\",\"query\":\"reciprocal rank fusion\",\"mode\":\"hybrid\"}" "$base_url/v1/search"

