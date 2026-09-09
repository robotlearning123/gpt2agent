"""Regression: SSE message parsing must tolerate present-but-null fields.

The backend can send a frame whose ``author`` / ``content`` / ``metadata`` key is
present with an explicit JSON ``null`` (system/tether/echo frames, moderation
placeholders). ``dict.get(key, {})`` returns ``None`` (not ``{}``) for such a
frame, so a following ``.get(...)`` raised ``AttributeError`` and aborted the
whole stream. For ``deep_research`` this killed a multi-minute, quota-consuming
run on a single unrelated frame, because ``content`` is dereferenced for every
frame before any role filter.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from gpt2agent import sse as sse_mod


class _FrameResponse:
    status_code = 200
    text = ""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Backend:
    class _Session:
        headers: dict[str, str] = {"User-Agent": "test-agent"}

    _session = _Session()

    def _reload_token_if_stale(self) -> None:
        pass

    def post(self, *_: Any, **__: Any) -> dict:
        return {"limits_progress": [{"feature_name": "deep_research", "remaining": 100}]}


class _SentinelStub:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(self) -> dict[str, str]:
        return {"chat-requirements": "stub", "proof": "", "turnstile": ""}


def _patch_frames(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    class _FrameSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_FrameSession":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _FrameResponse:
            return _FrameResponse(lines)

    monkeypatch.setattr(sse_mod, "AsyncSession", _FrameSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _SentinelStub)


def _frame(message: dict) -> str:
    return "data: " + json.dumps({"message": message})


_REAL_REPORT = _frame(
    {
        "id": "m-report",
        "author": {"role": "assistant"},
        "recipient": "all",
        "content": {"content_type": "text", "parts": ["THE REAL REPORT"]},
        "status": "finished_successfully",
        "metadata": {},
    }
)


def _run_deep_research(monkeypatch, poison_frames: list[str]) -> list[dict]:
    _patch_frames(monkeypatch, [*poison_frames, _REAL_REPORT, "data: [DONE]"])
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [ev async for ev in client.deep_research("q")]

    return asyncio.run(_collect())


def test_deep_research_survives_null_content_frame(monkeypatch) -> None:
    poison = _frame({"id": "m0", "author": {"role": "system"}, "content": None,
                     "status": "finished"})
    events = _run_deep_research(monkeypatch, [poison])
    dones = [e for e in events if e.get("type") == "done"]
    assert dones and dones[-1]["text"] == "THE REAL REPORT"


def test_deep_research_survives_null_author_and_metadata(monkeypatch) -> None:
    null_author = _frame({"id": "m0", "author": None,
                          "content": {"content_type": "text", "parts": ["x"]},
                          "status": "in_progress"})
    null_metadata = _frame({"id": "m1", "author": {"role": "assistant"},
                            "recipient": "all",
                            "content": {"content_type": "text", "parts": ["y"]},
                            "status": "in_progress", "metadata": None})
    events = _run_deep_research(monkeypatch, [null_author, null_metadata])
    dones = [e for e in events if e.get("type") == "done"]
    assert dones and dones[-1]["text"] == "THE REAL REPORT"
