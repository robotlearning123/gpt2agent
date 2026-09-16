"""Integration test: BackendClient.account_status() against live chatgpt.com.

Skipped by default (SKIP_LIVE=1) or when ~/.codex/auth.json is absent.
Run live: SKIP_LIVE=0 pytest tests/test_backend_tools.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_SKIP_LIVE = os.environ.get("SKIP_LIVE", "1") == "1"


@pytest.mark.skipif(_SKIP_LIVE, reason="SKIP_LIVE=1 — set SKIP_LIVE=0 to run live")
@pytest.mark.skipif(
    not (Path.home() / ".codex" / "auth.json").exists(),
    reason="~/.codex/auth.json not present",
)
def test_account_status_has_subscription() -> None:
    from gpt2agent.backend import BackendClient

    client = BackendClient()

    # call the raw backend methods directly — no MCP runtime needed
    me = client.get("/backend-api/me", target_path="/backend-api/me")
    assert isinstance(me, dict) and me, "/backend-api/me returned no account info"
    check = client.get(
        "/backend-api/accounts/check/v4-2023-04-27",
        target_path="/backend-api/accounts/check/v4-2023-04-27",
    )

    acc_keys = list((check.get("accounts") or {}).keys())
    assert acc_keys, "accounts dict is empty"
    first = (check.get("accounts") or {}).get(acc_keys[0], {})
    ent = first.get("entitlement") or {}

    assert "subscription_plan" in ent, f"subscription field missing; entitlement={ent}"
    assert ent.get("subscription_plan"), "subscription_plan is empty"
