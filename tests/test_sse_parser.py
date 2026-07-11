"""Unit tests for the SSE stream parser (no network).

Feeds synthetic SSE frames through ConversationClient.stream() via a mocked
curl_cffi AsyncSession, and asserts that multi-message streams do not
duplicate text across message-id boundaries (C2 regression).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest


# ---- fixture SSE frames --------------------------------------------------

# Three distinct assistant messages:
#   msg-1: streamed via v-patch string deltas "Hello " + "world"
#   msg-2: Format B full-message frame "<thinking>preamble</thinking>"
#          (starts a NEW message id -> must NOT dedupe against msg-1)
#   msg-3: Format B full-message frame "Final answer"
#          (NEW message id -> must NOT dedupe against msg-2)
#
# Combined expected output: "Hello " + "world" + "<thinking>preamble</thinking>"
# + "Final answer". If the old single-tracker logic were still in place, the
# msg-2 and msg-3 frames would fail the `startswith(last_text)` check and be
# yielded in full but without resetting `last_text`, potentially duplicating
# or desyncing on subsequent frames.

_FRAMES = [
    # Establish the visible assistant message before accepting string-only
    # deltas. An unscoped string patch must not be assumed to be user-visible;
    # it could belong to a tool-targeted dispatch.
    "data: "
    + json.dumps(
        {
            "v": {
                "message": {
                    "id": "msg-1",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": []},
                }
            }
        }
    ),
    # msg-1 deltas (string v-patch — continuation of the visible message)
    'data: {"v":"Hello "}',
    'data: {"v":"world"}',
    # msg-2: Format B full-message frame, new id
    "data: "
    + json.dumps(
        {
            "message": {
                "id": "msg-2",
                "author": {"role": "assistant"},
                "content": {
                    "content_type": "text",
                    "parts": ["<thinking>preamble</thinking>"],
                },
            }
        }
    ),
    # msg-3: Format B full-message frame, new id
    "data: "
    + json.dumps(
        {
            "message": {
                "id": "msg-3",
                "author": {"role": "assistant"},
                "content": {
                    "content_type": "text",
                    "parts": ["Final answer"],
                },
            }
        }
    ),
    "data: [DONE]",
]


class _FakeResp:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_content(self):
        for ln in self._lines:
            yield (ln + "\n").encode()


class _FakeSession:
    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, *_, **__) -> _FakeResp:
        return _FakeResp(_FRAMES)


class _FakeBackend:
    class _Sess:
        headers: dict[str, str] = {"User-Agent": "test-agent"}

    _session = _Sess()

    def _reload_token_if_stale(self) -> None:  # mirrors BackendClient
        pass

    def request_headers(self) -> dict[str, str]:
        return dict(self._session.headers)


class _FakeSentinel:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(
        self, _operation_headers: dict[str, str] | None = None
    ) -> dict[str, str]:
        return {"chat-requirements": "stub", "proof": "", "turnstile": ""}


def test_multi_message_stream_no_duplication(monkeypatch: pytest.MonkeyPatch) -> None:
    from gpt2agent import sse as sse_mod

    monkeypatch.setattr(sse_mod, "AsyncSession", _FakeSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _FakeSentinel)

    client = sse_mod.ConversationClient(_FakeBackend())  # type: ignore[arg-type]

    async def _run() -> list[str]:
        out: list[str] = []
        async for chunk in client.stream(
            "gpt-5-3", [{"role": "user", "content": "hi"}]
        ):
            out.append(chunk)
        return out

    chunks = asyncio.run(_run())
    joined = "".join(chunks)

    # Each fresh assistant message must appear exactly once.
    assert joined.count("Hello ") == 1
    assert joined.count("world") == 1
    assert joined.count("<thinking>preamble</thinking>") == 1
    assert joined.count("Final answer") == 1

    # And the full concatenated stream must match the expected sequence.
    expected = "Hello world<thinking>preamble</thinking>Final answer"
    assert joined == expected, f"joined={joined!r}"
