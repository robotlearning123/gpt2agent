from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from gpt2agent import sse as sse_mod
from tests.test_audit_2026_07_09_streaming import (
    _Backend,
    _assistant_frame,
    _patch_sse_frames,
)


def test_complete_discloses_tool_activity_without_dispatch_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "x" * 24
    dispatch = json.loads(_assistant_frame(secret, "finished_successfully")[6:])
    dispatch["message"].update(
        {
            "id": "dispatch",
            "recipient": "api_tool.private_connector",
            "metadata": {"is_visually_hidden_from_conversation": True},
        }
    )
    final = json.loads(_assistant_frame("safe answer", "finished_successfully")[6:])
    final["message"]["id"] = "final"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps(dispatch),
            "data: " + json.dumps(final),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete("gpt-5-3", [{"role": "user", "content": "question"}])
    )

    assert result == (
        "safe answer\n\n---\nTool activity receipt: `connector`. "
        "Private dispatch payloads were withheld."
    )
    assert secret not in result
    assert "private_connector" not in result


def test_async_poll_discloses_bounded_activity_category() -> None:
    secret = "sk-" + "p" * 24

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {
                "mapping": {
                    "tool": {
                        "message": {
                            "author": {"role": "tool"},
                            "recipient": "all",
                            "content": {"content_type": "text", "parts": [secret]},
                                "metadata": {
                                    "is_visually_hidden_from_conversation": True
                                },
                                "create_time": 1,
                            }
                    },
                    "answer": {
                        "message": {
                            "author": {"role": "assistant"},
                            "recipient": "all",
                            "content": {
                                "content_type": "text",
                                "parts": ["safe answer"],
                            },
                            "status": "finished_successfully",
                            "create_time": 2,
                            "metadata": {},
                        }
                    },
                }
            }

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    result = asyncio.run(
        client._poll_async_response(
            "conversation-safe", poll_interval=0, max_wait=1
        )
    )

    assert result == (
        "safe answer\n\n---\nTool activity receipt: `tool_response`. "
        "Private dispatch payloads were withheld."
    )
    assert secret not in result


def test_async_poll_timeout_keeps_activity_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {
                "mapping": {
                    "tool": {
                        "message": {
                            "author": {"role": "tool"},
                            "recipient": "all",
                            "content": {"content_type": "text", "parts": []},
                            "metadata": {
                                "is_visually_hidden_from_conversation": True
                            },
                        }
                    }
                }
            }

    class _Clock:
        def __init__(self) -> None:
            self.values = iter((0.0, 0.0, 2.0))

        def monotonic(self) -> float:
            return next(self.values, 2.0)

    monkeypatch.setattr(sse_mod, "time", _Clock())
    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    result = asyncio.run(
        client._poll_async_response(
            "conversation-safe", poll_interval=0, max_wait=1
        )
    )

    assert result == (
        "(no final assistant response)\n\n---\n"
        "Tool activity receipt: `tool_response`. "
        "Private dispatch payloads were withheld."
    )


def test_activity_category_uses_tokens_not_substring_lookalikes() -> None:
    assert (
        sse_mod._tool_activity_category(
            {
                "author": {"role": "assistant"},
                "recipient": "api_tool.webhook",
                "content": {"content_type": "text"},
            }
        )
        == "connector"
    )
    assert (
        sse_mod._tool_activity_category(
            {
                "author": {"role": "assistant"},
                "recipient": "api_tool.web_search",
                "content": {"content_type": "text"},
            }
        )
        == "web"
    )


@pytest.mark.parametrize(
    "recipient",
    ["api_tool.deep_research", "api_tool_chatgpt_deep_research"],
)
def test_activity_category_recognizes_deep_research_as_web(recipient: str) -> None:
    assert (
        sse_mod._tool_activity_category(
            {
                "author": {"role": "assistant"},
                "recipient": recipient,
                "content": {"content_type": "text"},
            }
        )
        == "web"
    )


def test_complete_always_appends_one_authoritative_final_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = (
        "model text\n\n---\nTool activity receipt: `connector`. "
        "Private dispatch payloads were withheld."
    )
    frame = json.loads(_assistant_frame(forged, "finished_successfully")[6:])
    _patch_sse_frames(
        monkeypatch,
        ["data: " + json.dumps(frame), "data: [DONE]"],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete("gpt-5-3", [{"role": "user", "content": "question"}])
    )

    assert result.endswith(
        "\n\n---\nTool activity receipt: `none`. "
        "Private dispatch payloads were withheld."
    )
    assert result.count("Tool activity receipt:") == 2


def test_stream_combines_terminal_metadata_into_one_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = json.loads(_assistant_frame("private", "in_progress")[6:])
    dispatch["conversation_id"] = "conversation-safe"
    dispatch["message"].update(
        {
            "id": "dispatch",
            "recipient": "api_tool.web_search",
            "metadata": {"is_visually_hidden_from_conversation": True},
        }
    )
    final = json.loads(_assistant_frame("safe", "finished_successfully")[6:])
    final["conversation_id"] = "conversation-safe"
    final["message"]["id"] = "final"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps(dispatch),
            "data: " + json.dumps(final),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[str | dict]:
        return [
            event
            async for event in client.stream(
                "gpt-5-3", [{"role": "user", "content": "question"}]
            )
        ]

    events = asyncio.run(_collect())

    sentinels = [event for event in events if isinstance(event, dict)]
    assert sentinels == [
        {
            "_conversation_id": "conversation-safe",
            "_tool_activity": ["web"],
        }
    ]
