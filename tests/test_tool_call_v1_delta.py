"""Regression: tool content on the ``/f/conversation`` v1 delta wire.

2026-09-23 live matrix (gpt2agent 0.0.22, two real ChatGPT accounts): a
``code_interpreter`` run returned ``text=""`` with ``tool_calls`` and
``tool_responses`` whose ``parts`` were empty — account A
(``artifacts/verify/live-matrix-2026-09-23/A-tools/02_code_interpreter.json``,
canvas twin in ``04_raw_frames_canvas.jsonl``) and account B
(``B-bounded/03_code_interpreter.json``, same shape). The raw capture shows
why: the tool call's code and its execution output never appear inside a
message envelope — they stream as ``/message/content/text`` patch ops
(frames 30-38 carry the code, frame 43 the stdout) plus bare ``{"v": str}``
continuations, which only ``stream()`` knew how to apply.

Frames here are GENERATED from dicts through ``json.dumps`` (never hand-copied
bytes), and the expected stdout is computed by running the fixture's own code,
so the assertion is an oracle rather than a constant.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from gpt2agent import sse as sse_mod

_CONV_ID = "00000000-0000-4000-8000-000000000002"
_MODEL = "gpt-5-6"
_PROMPT = "Run the code and report the printed output verbatim."

_PROSE = "There are 30 numbers in that range, 15 Fizz, 10 Buzz and 3 FizzBuzz."


def _fizzbuzz_source() -> str:
    return (
        "def fizzbuzz(n):\n"
        "    out = []\n"
        "    for i in range(1, n + 1):\n"
        "        word = ('Fizz' if i % 3 == 0 else '')\n"
        "        word += 'Buzz' if i % 5 == 0 else ''\n"
        "        out.append(word or str(i))\n"
        "    return out\n"
        "\n"
        "print(len(fizzbuzz(30)))"
    )


def _stdout() -> str:
    """What the fixture's code prints — computed, not copied from the capture."""
    out = []
    for i in range(1, 31):
        word = ("Fizz" if i % 3 == 0 else "") + ("Buzz" if i % 5 == 0 else "")
        out.append(word or str(i))
    return f"{len(out)}\n"


def _chunks(text: str, width: int) -> list[str]:
    return [text[i : i + width] for i in range(0, len(text), width)]


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
    gets: list[str] = []

    def _reload_token_if_stale(self) -> None:
        pass


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


def _frames(*, with_patches: bool = True) -> list[str]:
    """SSE lines shaped like the recorded code-interpreter turn.

    ``with_patches=False`` keeps every envelope and drops the v1 patch frames:
    that is what the empty-parts defect was reading, so it is the control case
    proving these frames can produce the defect as well as the fix.
    """
    code_chunks = _chunks(_fizzbuzz_source(), 24)
    prose_chunks = _chunks(_PROSE, 11)
    assert len(code_chunks) >= 3 and len(prose_chunks) >= 3

    def line(obj: dict) -> str:
        return "data: " + json.dumps(obj)

    def envelope(msg: dict) -> str:
        return line(
            {
                "p": "",
                "o": "add",
                "v": {"message": msg},
                "c": 1,
                "conversation_id": _CONV_ID,
            }
        )

    frames = [
        "event: delta_encoding",
        'data: "v1"',  # non-dict encoding marker — must be skipped
        line(
            {
                "type": "input_message",
                "conversation_id": _CONV_ID,
                "input_message": {"id": "msg-user"},
            }
        ),
        envelope(
            {
                "id": "msg-user",
                "author": {"role": "user"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": [_PROMPT]},
                "status": "finished_successfully",
            }
        ),
        # The tool call: recipient python, content_type code, and empty content —
        # the code itself arrives only through the patch frames below.
        envelope(
            {
                "id": "msg-call",
                "author": {"role": "assistant"},
                "recipient": "python",
                "content": {
                    "content_type": "code",
                    "language": "unknown",
                    "text": "",
                },
                "status": "in_progress",
            }
        ),
    ]
    if with_patches:
        frames += [
            line(
                {
                    "p": "/message/content/text",
                    "o": "append",
                    "v": code_chunks[0],
                }
            ),
            *[line({"v": chunk}) for chunk in code_chunks[1:-1]],
            line(
                {
                    "p": "",
                    "o": "patch",
                    "v": [
                        {
                            "p": "/message/content/text",
                            "o": "append",
                            "v": code_chunks[-1],
                        },
                        {
                            "p": "/message/status",
                            "o": "replace",
                            "v": "finished_successfully",
                        },
                        {"p": "/message/end_turn", "o": "replace", "v": False},
                        {
                            "p": "/message/metadata",
                            "o": "append",
                            "v": {
                                "is_complete": True,
                                "finish_details": {"type": "stop"},
                            },
                        },
                    ],
                }
            ),
        ]
    frames.append(
        envelope(
            {
                "id": "msg-tool",
                "author": {"role": "tool", "name": "python"},
                "recipient": "all",
                "content": {"content_type": "execution_output", "text": ""},
                "status": "in_progress",
            }
        )
    )
    if with_patches:
        frames.append(
            line(
                {
                    "p": "",
                    "o": "patch",
                    "v": [
                        {
                            "p": "/message/content/text",
                            "o": "append",
                            "v": _stdout(),
                        },
                        {
                            "p": "/message/status",
                            "o": "replace",
                            "v": "finished_successfully",
                        },
                        {
                            "p": "/message/metadata/aggregate_result/messages",
                            "o": "append",
                            "v": [
                                {
                                    "message_type": "stream",
                                    "stream_name": "stdout",
                                    "text": _stdout(),
                                }
                            ],
                        },
                        {
                            "p": "/message/metadata/aggregate_result/status",
                            "o": "replace",
                            "v": "success",
                        },
                    ],
                }
            )
        )
    frames.append(
        envelope(
            {
                "id": "msg-prose",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": [""]},
                "status": "in_progress",
            }
        )
    )
    if with_patches:
        frames += [
            line(
                {
                    "p": "/message/content/parts/0",
                    "o": "append",
                    "v": prose_chunks[0],
                }
            ),
            *[line({"v": chunk}) for chunk in prose_chunks[1:]],
            line(
                {
                    "p": "",
                    "o": "patch",
                    "v": [
                        {
                            "p": "/message/status",
                            "o": "replace",
                            "v": "finished_successfully",
                        },
                        {"p": "/message/end_turn", "o": "replace", "v": True},
                        {
                            "p": "/message/metadata",
                            "o": "append",
                            "v": {"is_complete": True},
                        },
                    ],
                }
            ),
        ]
    frames += [
        line(
            {
                "type": "message_marker",
                "conversation_id": _CONV_ID,
                "message_id": "msg-prose",
                "marker": "last_token",
                "event": "last",
            }
        ),
        line(
            {
                "type": "server_ste_metadata",
                "conversation_id": _CONV_ID,
                "metadata": {"model_slug": _MODEL},
            }
        ),
        line({"type": "message_stream_complete", "conversation_id": _CONV_ID}),
        "data: [DONE]",
    ]
    return frames


def test_tool_call_applies_v1_delta_patch_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text, the tool call's code and the stdout all come back populated."""
    _patch_sse_frames(monkeypatch, _frames())

    result = asyncio.run(sse_mod.ConversationClient(_Backend()).tool_call(_PROMPT))

    assert result["conversation_id"] == _CONV_ID
    assert result["text"] == _PROSE
    assert [c["recipient"] for c in result["tool_calls"]] == ["python"]
    assert result["tool_calls"][0]["content_type"] == "code"
    assert result["tool_calls"][0]["parts"] == [_fizzbuzz_source()]
    assert [r["content_type"] for r in result["tool_responses"]] == [
        "execution_output"
    ]
    assert result["tool_responses"][0]["parts"] == [_stdout()]
    assert result["multimodal_assets"] == []


def test_envelope_only_frames_reproduce_the_empty_parts_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Power check: without the patch frames the same turn is empty.

    This is the recorded defect shape (``A-tools/02_code_interpreter.json``:
    ``text=""``, parts ``[]``), so the assertion above cannot pass by accident.
    """
    _patch_sse_frames(monkeypatch, _frames(with_patches=False))

    result = asyncio.run(sse_mod.ConversationClient(_Backend()).tool_call(_PROMPT))

    assert result["text"] == ""
    assert result["tool_calls"] == [
        {"recipient": "python", "content_type": "code", "parts": []}
    ]
    assert result["tool_responses"] == [
        {"content_type": "execution_output", "parts": []}
    ]


def test_stream_emits_prose_only_on_the_same_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared applier must not leak tool payloads into chat text."""
    _patch_sse_frames(monkeypatch, _frames())
    client = sse_mod.ConversationClient(_Backend())

    async def _collect() -> list[Any]:
        out: list[Any] = []
        async for event in client.stream(
            _MODEL, [{"role": "user", "content": _PROMPT}]
        ):
            out.append(event)
        return out

    events = asyncio.run(_collect())
    text = "".join(e for e in events if isinstance(e, str))

    assert text == _PROSE
    assert _fizzbuzz_source() not in text
    # ``_stdout()`` is "30\n" — its newline makes it unambiguous next to the
    # prose, and neither payload carries one into the chat stream.
    assert _stdout() not in text
    assert "\n" not in text
    assert [e for e in events if isinstance(e, dict)][-1]["_conversation_id"] == _CONV_ID
