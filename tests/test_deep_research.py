"""Integration tests for Deep Research via native SSE.

Live tests are skipped by default (SKIP_LIVE=1 env var) because a real DR
run takes 30-120 seconds.  Set SKIP_LIVE=0 (or unset it) to run live.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest


_CODEX_AUTH = (
    Path(os.environ["CODEX_HOME"])
    if os.environ.get("CODEX_HOME")
    else Path.home() / ".codex"
) / "auth.json"
_AUTH_EXISTS = _CODEX_AUTH.exists()
_SKIP_LIVE = os.environ.get("SKIP_LIVE", "1") == "1"


@pytest.mark.skipif(not _AUTH_EXISTS, reason="requires the selected Codex auth.json")
@pytest.mark.skipif(_SKIP_LIVE, reason="SKIP_LIVE=1 — set SKIP_LIVE=0 to run live")
def test_deep_research_yields_done_event():
    """DR stream must emit at least one 'done' event with non-empty text."""
    from gpt2agent.backend import BackendClient
    from gpt2agent.sse import ConversationClient

    conv = ConversationClient(BackendClient())
    events: list[dict] = []

    async def collect():
        async for event in conv.deep_research("What is the capital of France? (brief)"):
            events.append(event)

    asyncio.run(collect())

    event_types = {e["type"] for e in events}
    assert "done" in event_types, f"no 'done' event; got types: {event_types}"

    done_events = [e for e in events if e["type"] == "done"]
    assert done_events[-1]["text"], "final 'done' event has empty text"


@pytest.mark.skipif(not _AUTH_EXISTS, reason="requires the selected Codex auth.json")
@pytest.mark.skipif(_SKIP_LIVE, reason="SKIP_LIVE=1 — set SKIP_LIVE=0 to run live")
def test_deep_research_emits_tool_events():
    """DR stream should emit at least one 'tool' event (search call)."""
    from gpt2agent.backend import BackendClient
    from gpt2agent.sse import ConversationClient

    conv = ConversationClient(BackendClient())
    events: list[dict] = []

    async def collect():
        async for event in conv.deep_research(
            "Search the web: what are the latest AI model releases in 2025?"
        ):
            events.append(event)

    asyncio.run(collect())

    tool_events = [e for e in events if e["type"] == "tool"]
    assert tool_events, (
        f"no 'tool' events; all event types: {[e['type'] for e in events]}"
    )
    assert "search" in tool_events[0]["call"].lower(), (
        f"first tool call doesn't look like a search: {tool_events[0]['call']!r}"
    )


@pytest.mark.skipif(not _AUTH_EXISTS, reason="requires the selected Codex auth.json")
@pytest.mark.skipif(_SKIP_LIVE, reason="SKIP_LIVE=1 — set SKIP_LIVE=0 to run live")
def test_deep_research_has_content_references():
    """Completed DR response should include web source references."""
    from gpt2agent.backend import BackendClient
    from gpt2agent.sse import ConversationClient

    conv = ConversationClient(BackendClient())
    events: list[dict] = []

    async def collect():
        async for event in conv.deep_research(
            "What is the most recent version of Python? Search the web."
        ):
            events.append(event)

    asyncio.run(collect())

    done_events = [e for e in events if e["type"] == "done"]
    assert done_events, "no 'done' event"
    refs = done_events[-1].get("content_references", [])
    groups = done_events[-1].get("search_result_groups", [])
    assert refs or groups, (
        "no content_references or search_result_groups in done event — "
        "web search may not have fired"
    )


def test_build_dr_payload_shape():
    """Unit test: _build_dr_payload produces correct model + system_hints."""
    from gpt2agent.sse import _build_dr_payload, DR_MODEL

    payload = _build_dr_payload("test query")
    assert payload["model"] == DR_MODEL == "research"
    assert payload["system_hints"] == ["research"]
    assert payload["conversation_mode"] == {"kind": "primary_assistant"}
    assert payload["force_use_sse"] is True
    # DR requires persistent conversation; "temporary chats" reject DR with
    # "Research is not currently supported in temporary chats".
    assert payload["history_and_training_disabled"] is False
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["content"]["parts"][0] == "test query"
