"""Tests for the codex token reload mechanism (P0 #3 fix).

Codex CLI auto-refreshes ``~/.codex/auth.json`` in the background. Without the
``_reload_token_if_stale()`` hook in BackendClient, long-running calls (heavy DR
poll phase) would 401 once the in-memory bearer aged past the file's refresh.
"""

from __future__ import annotations

import json
import os
import time

import pytest


def _write_auth(path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"access_token": token}}))


def test_reload_picks_up_codex_refresh(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    auth = tmp_path / ".codex" / "auth.json"
    _write_auth(auth, "TOK_OLD")

    from gpt2agent import backend as be

    client = be.BackendClient()
    assert "Authorization" not in client._session.headers
    assert client.request_headers()["Authorization"] == "Bearer TOK_OLD"
    assert client.auth_generation == 0

    # Simulate codex's background refresh — bump mtime + change token.
    time.sleep(0.01)
    _write_auth(auth, "TOK_NEW")

    client._reload_token_if_stale()
    assert "Authorization" not in client._session.headers
    assert client.request_headers()["Authorization"] == "Bearer TOK_NEW"
    assert client.auth_generation == 1


def test_reload_noop_when_mtime_unchanged(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    auth = tmp_path / ".codex" / "auth.json"
    _write_auth(auth, "TOK_STABLE")

    from gpt2agent import backend as be

    client = be.BackendClient()
    auth_before = client.request_headers()["Authorization"]
    generation_before = client.auth_generation

    client._reload_token_if_stale()
    assert client.request_headers()["Authorization"] == auth_before
    assert client.auth_generation == generation_before
    assert "Authorization" not in client._session.headers


def test_auth_generation_detects_rotation_before_cached_metadata_is_reused(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    auth = tmp_path / ".codex" / "auth.json"
    _write_auth(auth, "TOK_ACCOUNT_A")

    from gpt2agent import backend as be

    client = be.BackendClient()
    assert client.auth_generation == 0

    previous = auth.stat()
    _write_auth(auth, "TOK_ACCOUNT_B")
    os.utime(
        auth,
        ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000),
    )

    # ModelCatalog reads this property before consulting its 60-second cache.
    # The property therefore must refresh source metadata itself.
    assert client.auth_generation == 1
    assert client.request_headers()["Authorization"] == "Bearer TOK_ACCOUNT_B"


def test_auth_snapshot_pairs_generation_and_request_headers_atomically(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    auth = tmp_path / ".codex" / "auth.json"
    _write_auth(auth, "TOK_ACCOUNT_A")

    from gpt2agent import backend as be

    client = be.BackendClient()
    generation_a, headers_a = client.auth_snapshot()

    previous = auth.stat()
    _write_auth(auth, "TOK_ACCOUNT_B")
    os.utime(
        auth,
        ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000),
    )
    generation_b, headers_b = client.auth_snapshot()

    assert generation_a == 0
    assert headers_a["Authorization"] == "Bearer TOK_ACCOUNT_A"
    assert generation_b == 1
    assert headers_b["Authorization"] == "Bearer TOK_ACCOUNT_B"
    assert "Authorization" not in client._session.headers


def test_reload_tolerates_missing_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleted token file mid-session must not crash — keep stale bearer."""
    monkeypatch.setenv("HOME", str(tmp_path))
    auth = tmp_path / ".codex" / "auth.json"
    _write_auth(auth, "TOK_OLD")

    from gpt2agent import backend as be

    client = be.BackendClient()
    before = client.request_headers()["Authorization"]
    generation_before = client.auth_generation

    auth.unlink()  # file disappears

    # Must not raise — keeps the old bearer; next 401 will surface error.
    client._reload_token_if_stale()
    assert client.request_headers()["Authorization"] == before
    assert client.auth_generation == generation_before
    assert "Authorization" not in client._session.headers


def test_codex_home_routes_to_alt_account(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CODEX_HOME selects a second account's auth.json over ~/.codex."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_auth(tmp_path / ".codex" / "auth.json", "TOK_DEFAULT")
    alt_home = tmp_path / ".codex-cx2"
    _write_auth(alt_home / "auth.json", "TOK_CX2")
    monkeypatch.setenv("CODEX_HOME", str(alt_home))

    from gpt2agent import backend as be

    client = be.BackendClient()
    assert client.request_headers()["Authorization"] == "Bearer TOK_CX2"
    assert "Authorization" not in client._session.headers


def test_codex_home_unset_falls_back_to_home(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without CODEX_HOME, the default ~/.codex/auth.json is used."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_auth(tmp_path / ".codex" / "auth.json", "TOK_DEFAULT")

    from gpt2agent import backend as be

    client = be.BackendClient()
    assert client.request_headers()["Authorization"] == "Bearer TOK_DEFAULT"
    assert "Authorization" not in client._session.headers
