from __future__ import annotations

import pytest

from repomesh.cli import validate_serve_bind
from repomesh.config import Settings


def test_loopback_bind_remains_available_without_token() -> None:
    validate_serve_bind(Settings(host="127.0.0.1", api_token=None), False)


def test_exact_private_bind_requires_token() -> None:
    with pytest.raises(SystemExit, match="REPOMESH_API_TOKEN is required"):
        validate_serve_bind(Settings(host="192.168.10.20", api_token=None), False)


def test_exact_private_bind_accepts_token() -> None:
    validate_serve_bind(Settings(host="100.64.10.20", api_token="configured-outside-git"), False)


def test_wildcard_bind_is_rejected_for_native_serve() -> None:
    with pytest.raises(SystemExit, match="refusing wildcard"):
        validate_serve_bind(Settings(host="0.0.0.0", api_token="configured"), False)


def test_container_internal_wildcard_requires_explicit_flag() -> None:
    validate_serve_bind(Settings(host="0.0.0.0", api_token=None), True)
