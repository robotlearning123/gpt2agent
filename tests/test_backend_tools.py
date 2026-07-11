"""GET-only live contracts against chatgpt.com.

Skipped by default. Opt in with ``SKIP_LIVE=0`` and a Codex login.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest


_SKIP_LIVE = os.environ.get("SKIP_LIVE", "1") == "1"
_NEEDS_AUTH = pytest.mark.skipif(
    not (Path.home() / ".codex" / "auth.json").exists(),
    reason="requires ~/.codex/auth.json",
)
_LIVE_ONLY = pytest.mark.skipif(
    _SKIP_LIVE,
    reason="SKIP_LIVE=1 (default); set SKIP_LIVE=0 to run",
)


@_NEEDS_AUTH
@_LIVE_ONLY
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


# None = account default; the rest are values accepted by the live account
# contract on 2026-07-11. GPT-Live audio is a separate session contract; the
# catalog endpoint rejects `voice_mode=live` with a typed HTTP 422 response.
@_NEEDS_AUTH
@_LIVE_ONLY
@pytest.mark.parametrize("voice_mode", [None, "standard", "advanced", "wingman"])
def test_voice_catalog_live_contract(voice_mode: str | None) -> None:
    """Exercise one registered GET without starting or fetching Voice media."""
    from gpt2agent.backend import BackendClient
    from gpt2agent.tools import voice
    from tests.test_tools import FakeMCP

    mcp = FakeMCP()
    voice.register(mcp, BackendClient())
    kwargs = {} if voice_mode is None else {"voice_mode": voice_mode}
    result = asyncio.run(mcp.tools["list_voices"](**kwargs))

    # A mode the account cannot serve may legitimately return an empty catalog;
    # what must always hold is the normalized schema and identity invariants.
    assert isinstance(result, list)
    assert all(
        set(item) == {"id", "name", "description", "selected", "has_preview"}
        for item in result
    )
    assert all(isinstance(item["id"], str) and item["id"] for item in result)
    assert len({item["id"] for item in result}) == len(result)
    assert sum(item["selected"] is True for item in result) <= 1
    assert all(item["selected"] in (True, False, None) for item in result)


@_NEEDS_AUTH
@_LIVE_ONLY
def test_voice_catalog_removed_live_alias_is_rejected() -> None:
    """Keep the GPT-Live product name separate from the catalog enum."""
    from gpt2agent.backend import BackendClient
    from gpt2agent.tools import voice
    from tests.test_tools import FakeMCP

    mcp = FakeMCP()
    voice.register(mcp, BackendClient())

    with pytest.raises(
        RuntimeError,
        match=r"^HTTP 422 for /backend-api/settings/voices\?voice_mode=live$",
    ):
        asyncio.run(mcp.tools["list_voices"](voice_mode="live"))
