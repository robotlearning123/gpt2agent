"""Live SSE roundtrip: ask model to reply with PONG, verify stream parses.

Live tests are skipped by default to keep `pytest tests/` offline-safe.
Opt in with ``SKIP_LIVE=0`` (and, for the heavy DR variant, ``SKIP_HEAVY_DR=0``).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

_SKIP_LIVE = os.environ.get("SKIP_LIVE", "1") == "1"
_SKIP_HEAVY_DR = os.environ.get("SKIP_HEAVY_DR", "1") == "1"
_CODEX_AUTH = (
    Path(os.environ["CODEX_HOME"])
    if os.environ.get("CODEX_HOME")
    else Path.home() / ".codex"
) / "auth.json"

_NEEDS_AUTH = pytest.mark.skipif(
    not _CODEX_AUTH.exists(),
    reason="requires the selected Codex auth.json",
)


@_NEEDS_AUTH
@pytest.mark.skipif(_SKIP_LIVE, reason="SKIP_LIVE=1 (default); set SKIP_LIVE=0 to run")
def test_sse_pong():
    from gpt2agent.backend import BackendClient
    from gpt2agent.sse import ConversationClient

    conv = ConversationClient(BackendClient())
    out = asyncio.run(
        conv.complete(
            "gpt-5-3",
            [{"role": "user", "content": "Reply with exactly: PONG"}],
        )
    )
    assert "pong" in out.lower(), f"no PONG in response: {out!r}"


@_NEEDS_AUTH
@pytest.mark.skipif(
    _SKIP_LIVE or _SKIP_HEAVY_DR,
    reason="heavy DR skipped by default; set SKIP_LIVE=0 SKIP_HEAVY_DR=0 to run",
)
def test_sse_deep_research_heavy():
    """Live deep-research probe — skipped by default (slow + costly)."""
    from gpt2agent.backend import BackendClient
    from gpt2agent.sse import ConversationClient

    conv = ConversationClient(BackendClient())
    out = asyncio.run(
        conv.complete(
            "research",
            [{"role": "user", "content": "Summarize: what is 2+2? One sentence."}],
        )
    )
    assert out and out.strip(), "empty DR response"


def test_heavy_dr_payload_structure():
    """Verify _build_heavy_dr_payload produces the correct ground-truth payload shape.

    Pure unit test — no network call.
    """
    from gpt2agent.sse import (
        HEAVY_DR_HINT,
        _F_CONV_URL,
        _build_heavy_dr_payload,
    )

    payload = _build_heavy_dr_payload("What is the tallest mountain?")

    # Core fields
    assert payload["model"] == "gpt-5-5-pro", f"model mismatch: {payload['model']}"
    assert payload["system_hints"] == ["connector:connector_openai_deep_research"]
    assert payload["thinking_effort"] == "extended"
    assert payload["conversation_mode"] == {"kind": "primary_assistant"}
    assert payload["supported_encodings"] == ["v1"]
    assert payload["supports_buffering"] is True

    assert len(payload["messages"]) == 1
    msg = payload["messages"][0]
    assert msg["author"] == {"role": "user"}
    assert msg["content"]["parts"] == ["What is the tallest mountain?"]

    meta = msg["metadata"]
    assert meta["system_hints"] == [HEAVY_DR_HINT]
    assert meta["deep_research_version"] == "standard"
    assert meta["venus_model_variant"] == "standard"
    assert meta["caterpillar_selected_sources"] == []

    # Endpoint constant must point to /f/conversation
    assert _F_CONV_URL.endswith("/backend-api/f/conversation")


@_NEEDS_AUTH
@pytest.mark.skipif(
    _SKIP_LIVE or _SKIP_HEAVY_DR,
    reason="heavy DR skipped by default; set SKIP_LIVE=0 SKIP_HEAVY_DR=0 to run",
)
def test_heavy_dr_live_metadata():
    """Fire one heavy DR request and verify the connector is invoked.

    Checks server_ste_metadata event with tool_name=ApiToolWrapper and
    tool_invoked=True, confirming connector_openai_deep_research fired.
    """
    from gpt2agent.backend import BackendClient
    from gpt2agent.sse import ConversationClient

    async def run():
        conv = ConversationClient(BackendClient())
        meta_events = []
        async for event in conv.deep_research_heavy("What is 2+2?"):
            if event["type"] == "meta":
                meta_events.append(event)
            if event["type"] == "done":
                break
        return meta_events

    metas = asyncio.run(run())
    assert metas, "no server_ste_metadata events received"
    first_meta = metas[0]
    assert first_meta.get("tool_invoked") is True, (
        f"tool_invoked not True in metadata: {first_meta}"
    )
    assert first_meta.get("tool_category") == "connector", (
        f"unexpected tool category: {first_meta.get('tool_category')}"
    )
