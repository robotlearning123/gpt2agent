"""Light Deep Research on the modern /f/conversation v1 delta stream.

The legacy lane (model slug "research" + system_hints ["research"]) was
retired upstream in the 2026-09-22 GPT-6 rollout: the turn is accepted, the
system preamble streams, then the server aborts in-band with
"Error in message stream" (both Pro accounts; taskruns/20260923-dr-2acct/).
The working recipe — captured live 2026-09-23 (E10a3-nohint-fixed.jsonl) —
is the default chat model WITHOUT research hints: the model auto-searches
the web and streams citeturn markers + content_references. These tests pin
that recipe and the v1-delta parsing the old loop never had.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest


REFS = [
    {
        "matched_text": "Python 3.14",
        "safe_urls": ["https://python.org/"],
        "items": [
            {"title": "Python 3.14 What's New", "url": "https://python.org/3.14/"}
        ],
        "type": "webpage",
    }
]


def _msg_line(
    mid: str,
    role: str,
    parts: list[str],
    status: str,
    metadata: dict | None = None,
    recipient: str | None = None,
    content_type: str = "text",
) -> str:
    msg: dict[str, Any] = {
        "id": mid,
        "author": {"role": role},
        "content": {"content_type": content_type, "parts": parts},
        "status": status,
        "metadata": metadata or {},
    }
    if recipient is not None:
        msg["recipient"] = recipient
    return "data: " + json.dumps({"v": {"message": msg}})


def _patch_line(p: str, o: str, v: Any) -> str:
    return "data: " + json.dumps({"p": p, "o": o, "v": v})


# Frame shape transcribed from the live capture E10a3-nohint-fixed.jsonl:
# assistant envelope anchor with empty parts, text arrives ONLY via
# /message/content/parts/0 append patches, refs ride a system-message
# envelope's metadata, completion is a /message/status replace patch.
_MODERN_FRAMES = [
    "data: " + json.dumps({"type": "resume_conversation_token", "token": "t"}),
    _msg_line("u1", "user", ["q"], "finished_successfully"),
    _msg_line("s1", "system", [""], "finished_successfully"),
    _msg_line("a1", "assistant", [""], "in_progress"),
    _patch_line("/message/content/parts/0", "append", "Python 3.14 "),
    _patch_line("/message/content/parts/0", "append", "adds t-strings"),
    _msg_line(
        "s2", "system", [""], "finished_successfully",
        metadata={"content_references": REFS},
    ),
    _patch_line("/message/status", "replace", "finished_successfully"),
    "data: [DONE]",
]

# The same turn where the tail rides a batch patch frame (live capture
# frame 40 shape): {"p": "", "o": "patch", "v": [append, status-replace]}.
_BATCH_FRAMES = [
    _msg_line("a1", "assistant", [""], "in_progress"),
    "data: "
    + json.dumps(
        {
            "p": "",
            "o": "patch",
            "v": [
                {
                    "p": "/message/content/parts/0",
                    "o": "append",
                    "v": "batched answer",
                },
                {
                    "p": "/message/status",
                    "o": "replace",
                    "v": "finished_successfully",
                },
            ],
        }
    ),
    "data: [DONE]",
]


class _FakeResp:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeBackend:
    class _Sess:
        headers: dict[str, str] = {"User-Agent": "test-agent"}

    _session = _Sess()

    def _reload_token_if_stale(self) -> None:
        pass

    def post(self, *args: Any, **kwargs: Any) -> dict:
        return {
            "limits_progress": [
                {"feature_name": "deep_research", "remaining": 100}
            ]
        }


class _FakeSentinel:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(self) -> dict[str, str]:
        return {"chat-requirements": "stub", "proof": "", "turnstile": ""}


def _run_light_dr(
    monkeypatch: pytest.MonkeyPatch, frames: list[str]
) -> list[dict]:
    from gpt2agent import sse as sse_mod

    class _FrameSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_FrameSession":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _FakeResp:
            return _FakeResp(frames)

    monkeypatch.setattr(sse_mod, "AsyncSession", _FrameSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _FakeSentinel)

    client = sse_mod.ConversationClient(  # type: ignore[arg-type]
        _FakeBackend()
    )

    async def _go() -> list[dict]:
        out: list[dict] = []
        async for ev in client.deep_research("test query"):
            out.append(ev)
        return out

    return asyncio.run(_go())


def test_modern_stream_done_event_has_text_and_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _run_light_dr(monkeypatch, _MODERN_FRAMES)
    dones = [e for e in events if e.get("type") == "done"]
    assert len(dones) == 1, f"expected 1 done, got {len(dones)}: {events}"
    # The refs' matched_text is rewritten into an inline [N](url) citation.
    assert dones[0]["text"] == "[1](https://python.org/) adds t-strings"
    assert dones[0].get("content_references") == REFS
    assert not dones[0].get("terminated_abnormally")


def test_modern_stream_progress_events_stream_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _run_light_dr(monkeypatch, _MODERN_FRAMES)
    progress = "".join(
        e["text"] for e in events if e.get("type") == "progress"
    )
    assert progress == "Python 3.14 adds t-strings"


def test_batch_patch_frame_completes_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _run_light_dr(monkeypatch, _BATCH_FRAMES)
    dones = [e for e in events if e.get("type") == "done"]
    assert len(dones) == 1, f"expected 1 done, got {len(dones)}: {events}"
    assert dones[0]["text"] == "batched answer"
    assert not dones[0].get("terminated_abnormally")


def test_light_dr_payload_drops_research_hint() -> None:
    from gpt2agent.sse import LIGHT_DR_MODEL, _build_dr_payload

    payload = _build_dr_payload("test query")
    assert payload["model"] == LIGHT_DR_MODEL == "gpt-5-6"
    assert "research" not in (payload.get("system_hints") or []), (
        "the retired research hint must not be sent: "
        f"{payload.get('system_hints')}"
    )
    # DR still needs a persistent conversation.
    assert payload["history_and_training_disabled"] is False
    assert payload["messages"][0]["content"]["parts"][0] == "test query"


def test_light_dr_payload_keeps_connector_hints() -> None:
    from gpt2agent.sse import _build_dr_payload

    payload = _build_dr_payload(
        "test query", connectors=["connector_openai_pubmed"]
    )
    assert payload["system_hints"] == ["connector:connector_openai_pubmed"]


def test_light_dr_model_param_overrides_default() -> None:
    from gpt2agent.sse import _build_dr_payload

    payload = _build_dr_payload("q", model="gpt-6-pro")
    assert payload["model"] == "gpt-6-pro"
