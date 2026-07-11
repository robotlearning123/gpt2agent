"""Adversarial regressions for parser findings raised during v0.0.12 review."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest

from gpt2agent import sse as sse_mod
from gpt2agent.errors import BackendContractError
from tests.test_audit_2026_07_09_streaming import (
    _Backend,
    _assistant_frame,
    _patch_sse_frames,
)
from tests.test_heavy_dr_parser import _run_heavy_dr_with_frames


_SAFE_RECEIPT_SUFFIX = (
    "\n\n---\nTool activity receipt: `{category}`. "
    "Private dispatch payloads were withheld."
)


def _message(
    text: str,
    *,
    message_id: str,
    role: str = "assistant",
    recipient: str = "all",
    status: str = "finished_successfully",
    create_time: int | float | str = 1,
    metadata: object | None = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "author": {"role": role},
        "recipient": recipient,
        "content": {"content_type": "text", "parts": [text]},
        "status": status,
        "create_time": create_time,
        "metadata": {} if metadata is None else metadata,
    }


def _dispatch_text(secret: str) -> str:
    return json.dumps(
        {
            "path": (
                "/Deep Research App/implicit_link::"
                "connector_openai_deep_research/start"
            ),
            "args": {"query": secret},
        },
        separators=(",", ":"),
    )


def test_regular_complete_withholds_recipient_all_connector_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "r" * 24
    dispatch = json.loads(
        _assistant_frame(_dispatch_text(secret), "finished_successfully")[6:]
    )
    dispatch["message"]["id"] = "recipient-all-dispatch"
    final = json.loads(_assistant_frame("safe answer", "finished_successfully")[6:])
    final["message"]["id"] = "safe-final"
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

    assert result == "safe answer" + _SAFE_RECEIPT_SUFFIX.format(category="web")
    assert secret not in result
    assert "connector_openai_deep_research" not in result


def test_heavy_dr_withholds_recipient_all_connector_dispatch_and_emits_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "h" * 24
    dispatch = _message(
        _dispatch_text(secret),
        message_id="recipient-all-dispatch",
    )
    final = _message("safe report", message_id="safe-final", create_time=2)
    frames = [
        "data: " + json.dumps({"v": {"message": dispatch}}),
        "data: " + json.dumps({"v": {"message": final}}),
        "data: [DONE]",
    ]

    events = _run_heavy_dr_with_frames(monkeypatch, frames)

    assert {"type": "tool", "call": "web_search", "category": "web"} in events
    assert [event for event in events if event.get("type") == "done"] == [
        {
            "type": "done",
            "text": "safe report",
            "content_references": [],
            "search_result_groups": [],
        }
    ]
    assert secret not in repr(events)
    assert "connector_openai_deep_research" not in repr(events)


@pytest.mark.parametrize(
    ("private_lifecycle", "category"),
    [
        (
            _message(
                "private dispatch",
                message_id="private-dispatch",
                recipient="api_tool.private_connector",
                status="in_progress",
                create_time=2,
                metadata={"is_visually_hidden_from_conversation": True},
            ),
            "connector",
        ),
        (
            _message(
                "private tool state",
                message_id="private-tool",
                role="tool",
                status="in_progress",
                create_time=2,
                metadata={"is_visually_hidden_from_conversation": True},
            ),
            "tool_response",
        ),
    ],
    ids=("hidden-assistant", "hidden-tool"),
)
def test_async_poll_waits_past_stale_terminal_after_newer_private_lifecycle(
    private_lifecycle: dict[str, Any],
    category: str,
) -> None:
    stale = _message("stale answer", message_id="stale", create_time=1)
    safe = _message("safe answer", message_id="safe", create_time=3)

    class _PollingBackend(_Backend):
        def __init__(self) -> None:
            self.responses = [
                {
                    "mapping": {
                        "stale": {"message": stale},
                        "private": {"message": private_lifecycle},
                    }
                },
                {
                    "mapping": {
                        "stale": {"message": stale},
                        "private": {"message": private_lifecycle},
                        "safe": {"message": safe},
                    }
                },
            ]

        def get(self, *_: Any, **__: Any) -> dict[str, Any]:
            return self.responses.pop(0)

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    result = asyncio.run(
        client._poll_async_response(
            "conversation-safe", poll_interval=0, max_wait=1
        )
    )

    assert result == "safe answer" + _SAFE_RECEIPT_SUFFIX.format(category=category)
    assert "stale answer" not in result
    assert "private" not in result


def test_heavy_poll_buffers_stale_terminal_revoked_by_newer_tool_lifecycle() -> None:
    stale = _message("stale report", message_id="stale", create_time=1)
    private_tool = _message(
        "private tool state",
        message_id="private-tool",
        role="tool",
        status="in_progress",
        create_time=2,
        metadata={"is_visually_hidden_from_conversation": True},
    )
    revoked_stale = deepcopy(stale)
    revoked_stale["metadata"] = {"is_visually_hidden_from_conversation": True}
    safe = _message("safe report", message_id="safe", create_time=3)

    class _PollingBackend(_Backend):
        def __init__(self) -> None:
            self.responses = [
                {
                    "mapping": {
                        "stale": {"message": stale},
                        "private": {"message": private_tool},
                    }
                },
                {
                    "mapping": {
                        "stale": {"message": revoked_stale},
                        "private": {"message": private_tool},
                        "safe": {"message": safe},
                    }
                },
            ]

        def get(self, *_: Any, **__: Any) -> dict[str, Any]:
            return self.responses.pop(0)

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    async def _collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in client._poll_dr_completion(
                "conversation-safe", interval=0, max_wait=1
            )
        ]

    events = asyncio.run(_collect())

    assert "stale report" not in repr(events)
    assert "private tool state" not in repr(events)
    assert events[-1] == {
        "type": "done",
        "text": "safe report",
        "content_references": [],
        "search_result_groups": [],
        "connector_failed": False,
    }


@pytest.mark.parametrize(
    "detail",
    [
        ["not-an-object"],
        {"mapping": ["not-an-object"]},
        {"mapping": {"node": ["not-an-object"]}},
    ],
    ids=("response", "mapping", "node"),
)
def test_heavy_poll_malformed_containers_raise_contract_error(
    detail: object,
) -> None:
    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> object:
            return detail

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    async def _collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in client._poll_dr_completion(
                "conversation-safe", interval=0, max_wait=1
            )
        ]

    with pytest.raises(BackendContractError, match="heavy_deep_research"):
        asyncio.run(_collect())


def test_heavy_poll_mixed_timestamp_types_raise_contract_error() -> None:
    detail = {
        "mapping": {
            "numeric": {
                "message": _message(
                    "numeric timestamp", message_id="numeric", create_time=1
                )
            },
            "string": {
                "message": _message(
                    "string timestamp", message_id="string", create_time="2"
                )
            },
        }
    }

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict[str, Any]:
            return detail

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    async def _collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in client._poll_dr_completion(
                "conversation-safe", interval=0, max_wait=1
            )
        ]

    with pytest.raises(BackendContractError, match="create_time"):
        asyncio.run(_collect())


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("author", "assistant"),
        ("content", ["not-an-object"]),
        ("parts", {"not": "a list"}),
    ],
    ids=("author", "content", "parts"),
)
def test_heavy_stream_malformed_message_shapes_raise_contract_error(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    malformed: object,
) -> None:
    message = _message("unsafe", message_id="malformed")
    if field == "parts":
        message["content"]["parts"] = malformed
    else:
        message[field] = malformed
    frames = [
        "data: " + json.dumps({"v": {"message": message}}),
        "data: [DONE]",
    ]

    with pytest.raises(BackendContractError, match="heavy_deep_research"):
        _run_heavy_dr_with_frames(monkeypatch, frames)
