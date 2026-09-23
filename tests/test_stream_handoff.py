"""Regression: a server ``stream_handoff`` must not come back as empty text.

2026-09-23 (account A, gpt2agent 0.0.22): ``complete("gpt-6-pro", ...)`` returned
``""`` with status ok while the persisted conversation held the answer. The raw
frames —
``artifacts/verify/live-matrix-2026-09-23/A-models/raw/gpt-6-pro-frames.json`` and
``.../gpt-6-pro-rawframes.sse`` — show why: the SSE carries a ``stream_handoff``
frame (options ``resume_sse_endpoint`` + ``subscribe_ws_topic`` on the turn
topic) followed by metadata frames and ``[DONE]``, and no assistant text ever
arrives on that stream.

The frames below are GENERATED from dicts via ``json.dumps`` (never hand-written
bytes); the polling backend returns the persisted node shape recorded in
``.../raw/posthoc_conversation_state.json``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from gpt2agent import sse as sse_mod

_CONV_ID = "00000000-0000-4000-8000-000000000001"
_PROMPT = "Reply with exactly: PONG-gpt-6-pro"


class _FrameResponse:
    status_code = 200
    text = ""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Backend:
    """Backend whose conversation GET returns the persisted assistant node."""

    class _Session:
        headers: dict[str, str] = {"User-Agent": "test-agent"}

    _session = _Session()

    def __init__(self, poll_text: str = "PONG-gpt-6-pro") -> None:
        self.poll_text = poll_text
        self.gets: list[str] = []

    def _reload_token_if_stale(self) -> None:
        pass

    def get(self, path: str, **_: Any) -> dict:
        self.gets.append(path)
        return {
            "mapping": {
                "node-final": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": [self.poll_text]},
                        "status": "finished_successfully",
                        "create_time": 1,
                    }
                }
            }
        }


class _SentinelStub:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(self) -> dict[str, str]:
        return {"chat-requirements": "stub", "proof": "", "turnstile": ""}


def _patch_sse_frames(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
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


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(sse_mod.asyncio, "sleep", _instant)


def _assistant_msg(text: str, status: str) -> dict:
    return {
        "id": "msg-assistant",
        "author": {"role": "assistant"},
        "recipient": "all",
        "content": {"content_type": "text", "parts": [text]},
        "status": status,
    }


def _system_msg(node_id: str, parent: str | None) -> dict:
    return {
        "id": node_id,
        "author": {"role": "system"},
        "content": {"content_type": "text", "parts": [""]},
        "status": "finished_successfully",
        "end_turn": True,
        "metadata": {
            "is_visually_hidden_from_conversation": True,
            "model_slug": "gpt-6-pro",
            "parent_id": parent,
        },
    }


def _handoff_frames(
    *, handoff: bool = True, partial_text: str | None = None
) -> list[str]:
    """SSE lines shaped like the 2026-09-23 gpt-6-pro capture.

    ``handoff=False`` is the control stream: byte-for-byte the same turn
    without the ``stream_handoff`` frame, which must NOT trigger a poll.
    """
    def _frame(obj: dict) -> str:
        return "data: " + json.dumps(obj)

    def _envelope(msg: dict) -> dict:
        return {"v": {"message": msg}, "c": 0, "conversation_id": _CONV_ID}

    lines = [
        "event: delta_encoding",
        'data: "v1"',
        _frame(
            {
                "p": "",
                "o": "add",
                "v": {
                    "message": {
                        "id": "msg-user",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": [_PROMPT]},
                        "status": "finished_successfully",
                    }
                },
                "conversation_id": _CONV_ID,
                "c": 0,
            }
        ),
        _frame(_envelope(_system_msg("sys-1", "msg-user"))),
        _frame(_envelope(_system_msg("sys-2", "sys-1"))),
    ]
    if partial_text is not None:
        lines.append(_frame(_envelope(_assistant_msg(partial_text, "in_progress"))))
    if handoff:
        lines.append(
            _frame(
                {
                    "type": "stream_handoff",
                    "conversation_id": _CONV_ID,
                    "turn_exchange_id": "turn-1",
                    "options": [
                        {
                            "type": "resume_sse_endpoint",
                            "topic_id": "conversation-turn-turn-1",
                        },
                        {
                            "type": "subscribe_ws_topic",
                            "topic_id": "conversation-turn-turn-1",
                        },
                    ],
                }
            )
        )
    lines += [
        _frame(
            {
                "type": "title_generation",
                "title": "Exact reply",
                "conversation_id": _CONV_ID,
            }
        ),
        _frame(
            {
                "type": "server_ste_metadata",
                "metadata": {"model_slug": "gpt-6-pro"},
                "conversation_id": _CONV_ID,
            }
        ),
        _frame(
            {
                "type": "conversation_detail_metadata",
                "banner_info": None,
                "conversation_id": _CONV_ID,
            }
        ),
        "data: [DONE]",
    ]
    return lines


def _complete(frames: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[str, _Backend]:
    _patch_sse_frames(monkeypatch, frames)
    _no_sleep(monkeypatch)
    backend = _Backend()
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]
    text = asyncio.run(
        client.complete("gpt-6-pro", [{"role": "user", "content": _PROMPT}])
    )
    return text, backend


def test_complete_polls_persisted_text_after_stream_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handoff stream carries no text — the answer must come from the poll."""
    text, backend = _complete(_handoff_frames(), monkeypatch)

    assert text == "PONG-gpt-6-pro"
    assert backend.gets == [f"/backend-api/conversation/{_CONV_ID}"]


def test_stream_sentinel_reports_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sse_frames(monkeypatch, _handoff_frames())
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[Any]:
        out: list[Any] = []
        async for event in client.stream(
            "gpt-6-pro", [{"role": "user", "content": _PROMPT}]
        ):
            out.append(event)
        return out

    events = asyncio.run(_collect())
    sentinel = [e for e in events if isinstance(e, dict)][-1]
    assert sentinel["_stream_handoff"] is True
    assert sentinel["_conversation_id"] == _CONV_ID
    assert not [e for e in events if isinstance(e, str)]


def test_textless_stream_without_handoff_does_not_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Power check: the same turn minus the handoff frame must not poll.

    Without the explicit server signal an empty stream keeps today's behaviour
    (no 300 s poll on an ordinary empty reply).
    """
    text, backend = _complete(_handoff_frames(handoff=False), monkeypatch)

    assert text == ""
    assert backend.gets == []


def test_handoff_after_partial_text_keeps_streamed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-stream handoff (not observed live): never splice two copies."""
    text, backend = _complete(
        _handoff_frames(partial_text="PONG-gpt"), monkeypatch
    )

    assert text == "PONG-gpt"
    assert backend.gets == []
