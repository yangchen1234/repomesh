#!/usr/bin/env python3
"""Authenticated RepoMesh control-plane smoke client.

Only environment-variable names and non-secret response summaries are printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 15


class SmokeFailure(RuntimeError):
    pass


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SmokeFailure(f"required environment variable is missing: {name}")
    return value


def validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SmokeFailure("REPOMESH_BASE_URL must be an http(s) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SmokeFailure("REPOMESH_BASE_URL must not contain credentials, query, or fragment")
    return value.rstrip("/")


def request_json(
    base_url: str,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "repomesh-mac-handoff-smoke/1",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.load(response)


def run() -> dict[str, Any]:
    base_url = validate_base_url(required_environment("REPOMESH_BASE_URL"))
    token = required_environment("REPOMESH_API_TOKEN")

    health = request_json(base_url, token, "GET", "/v1/health")
    capabilities = request_json(base_url, token, "GET", "/v1/capabilities")
    repositories = request_json(base_url, token, "GET", "/v1/repositories")
    if not isinstance(repositories, list) or not repositories:
        raise SmokeFailure("remote node has no registered repository for the search smoke test")

    repository_id = repositories[0].get("id")
    if not isinstance(repository_id, str) or not repository_id:
        raise SmokeFailure("repository response did not contain a valid id")
    mode = os.environ.get("REPOMESH_REMOTE_SEARCH_MODE", "hybrid").strip().lower()
    if mode not in {"lexical", "dense", "hybrid"}:
        raise SmokeFailure("REPOMESH_REMOTE_SEARCH_MODE must be lexical, dense, or hybrid")
    query = os.environ.get(
        "REPOMESH_REMOTE_QUERY", "Where is API authentication enforced?"
    ).strip()
    if not query:
        raise SmokeFailure("REPOMESH_REMOTE_QUERY must not be empty")
    search = request_json(
        base_url,
        token,
        "POST",
        "/v1/search",
        {
            "repository_id": repository_id,
            "query": query,
            "mode": mode,
            "top_k": 3,
            "lexical_k": 20,
            "vector_k": 20,
        },
    )
    results = search.get("results") if isinstance(search, dict) else None
    if not isinstance(results, list):
        raise SmokeFailure("search response did not contain a results list")

    return {
        "status": "passed",
        "node_id": health.get("node_id"),
        "api_version": capabilities.get("api_version"),
        "repository_count": len(repositories),
        "search_mode": mode,
        "search_result_count": len(results),
        "degraded_mode": health.get("degraded_mode"),
    }


def main() -> int:
    try:
        summary = run()
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            print(f"remote smoke failed: authentication rejected (HTTP {exc.code})", file=sys.stderr)
            return 3
        print(f"remote smoke failed: API returned HTTP {exc.code}", file=sys.stderr)
        return 4
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        print(f"remote smoke failed: network error: {reason}", file=sys.stderr)
        return 4
    except (SmokeFailure, json.JSONDecodeError, OSError) as exc:
        print(f"remote smoke failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
