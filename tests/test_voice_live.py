"""Offline tests for GPT-Live Mode B MCP control tools (no audio / no network)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import urllib.error
import urllib.request

from gpt2agent.tools import voice_live


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args, **kwargs):
        name = kwargs.get("name")

        def deco(fn):
            key = name or fn.__name__
            self.tools[key] = fn
            return fn

        return deco


def _register() -> dict[str, Any]:
    mcp = _FakeMCP()
    voice_live.register(mcp, client=MagicMock())
    return mcp.tools


@pytest.mark.asyncio
async def test_voice_live_export_help_documents_boundary():
    tools = _register()
    text = await tools["voice_live_export_help"]()
    assert "Mode B" in text
    assert "Turnstile" in text
    assert "OUT OF SCOPE" in text or "out of scope" in text.lower()
    assert "voice_live_send_text" in text
    assert "audio" in text.lower()


@pytest.mark.asyncio
async def test_voice_live_status_uses_control_plane_and_strips_secrets():
    tools = _register()
    payload = {
        "state": "live",
        "token": "should-not-leak",
        "boundary": {"audioCrossesBoundary": False},
    }

    with patch.object(voice_live, "_request", return_value=payload) as req:
        out = await tools["voice_live_status"]()
        req.assert_called_once()
        assert out["state"] == "live"
        assert out["token"] == "[redacted]"
        assert out["boundary"]["audioCrossesBoundary"] is False


@pytest.mark.asyncio
async def test_voice_live_send_text_validates_and_posts():
    tools = _register()

    empty = await tools["voice_live_send_text"](text="  ")
    assert empty["ok"] is False

    with patch.object(
        voice_live, "_request", return_value={"ok": True, "delivered": True, "wire": "nope"}
    ) as req:
        out = await tools["voice_live_send_text"](text="agent reply")
        assert out["ok"] is True
        assert out["wire"] == "[redacted]"
        assert req.call_args[0][0] == "POST"
        assert req.call_args[0][1] == "/send_text"
        assert req.call_args[0][2] == {"text": "agent reply"}


@pytest.mark.asyncio
async def test_voice_live_get_transcript_and_end():
    tools = _register()
    with patch.object(
        voice_live,
        "_request",
        return_value={"transcripts": [{"role": "human", "text": "hi"}]},
    ) as req:
        out = await tools["voice_live_get_transcript"](clear=True)
        assert out["transcripts"][0]["text"] == "hi"
        assert req.call_args[0][1].endswith("clear=1")

    with patch.object(voice_live, "_request", return_value={"ok": True, "state": "closed"}):
        ended = await tools["voice_live_end"]()
        assert ended["ok"] is True


def test_request_unreachable_returns_hint(monkeypatch):
    def boom(*_a, **_k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    out = voice_live._request("GET", "/status")
    assert out["ok"] is False
    assert "unreachable" in out["error"]
    assert "sidecar" in out["hint"]
