"""RED regressions for widget recency and malformed poll authors."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest

from gpt2agent import sse as sse_mod
from gpt2agent.errors import BackendContractError


_MALFORMED_AUTHOR_CASES = (
    "author-missing",
    "author-null",
    "author-string",
    "author-list",
    "role-missing",
    "role-null",
    "role-empty",
    "role-whitespace",
    "role-control",
    "role-oversized",
    "role-unknown",
    "role-int",
    "role-list",
)


def _assistant_terminal(text: str, *, message_id: str, create_time: int) -> dict[str, Any]:
    return {
        "id": message_id,
        "author": {"role": "assistant"},
        "recipient": "all",
        "content": {"content_type": "text", "parts": [text]},
        "status": "finished_successfully",
        "create_time": create_time,
        "metadata": {},
    }


def _widget_report(
    text: str,
    *,
    message_id: str,
    create_time: int,
) -> dict[str, Any]:
    state = {
        "status": "completed",
        "report_message": {
            "content": {"content_type": "text", "parts": [text]},
            "status": "finished_successfully",
            "metadata": {
                "content_references": [
                    {"type": "webpage", "url": f"https://example.com/{message_id}"}
                ]
            },
        },
    }
    return {
        "id": message_id,
        "author": {"role": "tool", "name": "api_tool.widget_state"},
        "recipient": "all",
        "content": {
            "content_type": "text",
            "parts": [
                "The latest state of the widget is: " + json.dumps(state, separators=(",", ":"))
            ],
        },
        "status": "finished_successfully",
        "create_time": create_time,
        "metadata": {
            "exclusive_key": "widget_state:Deep Research App_start",
            "is_visually_hidden_from_conversation": True,
        },
    }


def _private_tool_lifecycle(*, message_id: str, create_time: int) -> dict[str, Any]:
    return {
        "id": message_id,
        "author": {"role": "tool", "name": "api_tool.call_tool"},
        "recipient": "all",
        "content": {"content_type": "text", "parts": ["private lifecycle"]},
        "status": "in_progress",
        "create_time": create_time,
        "metadata": {"is_visually_hidden_from_conversation": True},
    }


def _malformed_message(case: str) -> dict[str, Any]:
    message = _assistant_terminal("must never be exposed", message_id="bad", create_time=2)
    if case == "author-missing":
        del message["author"]
    elif case == "author-null":
        message["author"] = None
    elif case == "author-string":
        message["author"] = "assistant"
    elif case == "author-list":
        message["author"] = [{"role": "assistant"}]
    elif case == "role-missing":
        message["author"] = {}
    elif case == "role-null":
        message["author"] = {"role": None}
    elif case == "role-empty":
        message["author"] = {"role": ""}
    elif case == "role-whitespace":
        message["author"] = {"role": " "}
    elif case == "role-control":
        message["author"] = {"role": "assistant\n"}
    elif case == "role-oversized":
        message["author"] = {"role": "a" * 129}
    elif case == "role-unknown":
        message["author"] = {"role": "future_backend_role"}
    elif case == "role-int":
        message["author"] = {"role": 7}
    elif case == "role-list":
        message["author"] = {"role": ["assistant"]}
    else:  # pragma: no cover - parameter list is closed above
        raise AssertionError(f"unknown malformed-author case: {case}")
    return message


class _SequenceBackend:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls = 0

    def get(self, *_: Any, **__: Any) -> dict[str, Any]:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return deepcopy(response)


def test_heavy_poll_waits_past_stale_widget_and_selects_newest_carrier() -> None:
    old_text = "# OBSOLETE REPORT\n\n" + ("old private result " * 20)
    safe_text = "# Current report\n\nSafe current result."
    old_widget = _widget_report(old_text, message_id="widget-old", create_time=1)
    invalidator = _private_tool_lifecycle(message_id="tool-newer", create_time=2)
    current_widget = _widget_report(
        safe_text,
        message_id="widget-current",
        create_time=3,
    )
    backend = _SequenceBackend(
        [
            {
                "mapping": {
                    "old": {"message": old_widget},
                    "newer": {"message": invalidator},
                }
            },
            {
                "mapping": {
                    # Deliberately not timestamp order: lifecycle ordering, not
                    # mapping order or longest text, selects the current carrier.
                    "current": {"message": current_widget},
                    "newer": {"message": invalidator},
                    "old": {"message": old_widget},
                }
            },
        ]
    )
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in client._poll_dr_completion(
                "conversation-safe", interval=0, max_wait=1
            )
        ]

    events = asyncio.run(_collect())
    done = [event for event in events if event.get("type") == "done"]

    assert backend.calls == 2
    assert len(done) == 1
    assert done[0]["text"] == safe_text
    assert old_text not in repr(events)


@pytest.mark.parametrize("case", _MALFORMED_AUTHOR_CASES)
def test_ordered_poll_lifecycles_rejects_malformed_author_contract(case: str) -> None:
    mapping = {"bad": {"message": _malformed_message(case)}}

    with pytest.raises(BackendContractError):
        sse_mod._ordered_poll_lifecycles(mapping, adapter="test_poll")


@pytest.mark.parametrize("case", _MALFORMED_AUTHOR_CASES)
@pytest.mark.parametrize("poller", ("async", "heavy"))
def test_pollers_reject_newer_malformed_author_before_stale_terminal(
    case: str,
    poller: str,
) -> None:
    backend = _SequenceBackend(
        [
            {
                "mapping": {
                    "stale": {
                        "message": _assistant_terminal(
                            "stale terminal",
                            message_id="stale",
                            create_time=1,
                        )
                    },
                    "malformed": {"message": _malformed_message(case)},
                }
            }
        ]
    )
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _poll_heavy() -> list[dict[str, Any]]:
        return [
            event
            async for event in client._poll_dr_completion(
                "conversation-safe", interval=0, max_wait=1
            )
        ]

    with pytest.raises(BackendContractError):
        if poller == "async":
            asyncio.run(
                client._poll_async_response("conversation-safe", poll_interval=0, max_wait=1)
            )
        else:
            asyncio.run(_poll_heavy())


@pytest.mark.parametrize("poller", ("async", "heavy"))
def test_pollers_reject_mixed_timestamp_modes_before_stale_terminal(
    poller: str,
) -> None:
    stale = _assistant_terminal("stale terminal", message_id="stale", create_time=1)
    del stale["create_time"]
    backend = _SequenceBackend(
        [
            {
                "mapping": {
                    "newer": {
                        "message": _private_tool_lifecycle(
                            message_id="newer",
                            create_time=2,
                        )
                    },
                    "stale": {"message": stale},
                }
            }
        ]
    )
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _poll_heavy() -> list[dict[str, Any]]:
        return [
            event
            async for event in client._poll_dr_completion(
                "conversation-safe", interval=0, max_wait=1
            )
        ]

    with pytest.raises(BackendContractError):
        if poller == "async":
            asyncio.run(
                client._poll_async_response("conversation-safe", poll_interval=0, max_wait=1)
            )
        else:
            asyncio.run(_poll_heavy())


@pytest.mark.parametrize("poller", ("async", "heavy"))
def test_pollers_reject_oversized_full_message_metadata(
    monkeypatch: pytest.MonkeyPatch,
    poller: str,
) -> None:
    monkeypatch.setattr(sse_mod, "_MAX_METADATA_NODES", 3)
    terminal = _assistant_terminal("terminal", message_id="terminal", create_time=1)
    terminal["metadata"] = {"one": 1, "two": 2, "three": 3}
    backend = _SequenceBackend([{"mapping": {"terminal": {"message": terminal}}}])
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _poll_heavy() -> list[dict[str, Any]]:
        return [
            event
            async for event in client._poll_dr_completion(
                "conversation-safe", interval=0, max_wait=1
            )
        ]

    with pytest.raises(BackendContractError, match="metadata"):
        if poller == "async":
            asyncio.run(
                client._poll_async_response("conversation-safe", poll_interval=0, max_wait=1)
            )
        else:
            asyncio.run(_poll_heavy())


def test_poll_lifecycle_order_preserves_adjacent_large_integer_timestamps() -> None:
    stale = _assistant_terminal(
        "stale terminal",
        message_id="stale",
        create_time=2**53,
    )
    newer = _private_tool_lifecycle(
        message_id="newer",
        create_time=2**53 + 1,
    )
    mapping = {
        # Deliberately reverse chronological insertion order. Float conversion
        # collapses these adjacent integers and would incorrectly select stale.
        "newer": {"message": newer},
        "stale": {"message": stale},
    }

    ordered = sse_mod._ordered_poll_lifecycles(mapping, adapter="test_poll")

    assert ordered[-1][1]["id"] == "newer"


def test_poll_lifecycle_rejects_unbounded_integer_timestamp() -> None:
    message = _assistant_terminal(
        "terminal",
        message_id="terminal",
        create_time=1,
    )
    message["create_time"] = 10**400

    with pytest.raises(BackendContractError, match="create_time"):
        sse_mod._ordered_poll_lifecycles(
            {"terminal": {"message": message}},
            adapter="test_poll",
        )


def test_heavy_poll_withholds_widget_superseded_by_newer_user_turn() -> None:
    old_text = "OBSOLETE WIDGET REPORT"
    old_widget = _widget_report(old_text, message_id="widget-old", create_time=1)
    current_user = {
        "id": "user-current",
        "author": {"role": "user"},
        "recipient": "all",
        "content": {"content_type": "text", "parts": ["new question"]},
        "status": "finished_successfully",
        "create_time": 2,
        "metadata": {},
    }
    backend = _SequenceBackend(
        [
            {
                "mapping": {
                    "old": {"message": old_widget},
                    "current": {"message": current_user},
                }
            }
        ]
    )
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in client._poll_dr_completion(
                "conversation-safe", interval=0, max_wait=0.01
            )
        ]

    events = asyncio.run(_collect())

    assert old_text not in repr(events)
    assert events[-1]["terminated_abnormally"] is True
    assert events[-1]["timeout"] is True


def test_heavy_poll_does_not_reuse_citations_across_user_turn_boundary() -> None:
    stale_url = "https://stale.example/old-turn"
    stale = _assistant_terminal(
        "old report",
        message_id="assistant-old",
        create_time=1,
    )
    stale["metadata"] = {
        "content_references": [
            {"items": [{"url": stale_url, "title": "Old-turn source"}]}
        ],
        "search_result_groups": [{"type": "old-turn"}],
    }
    current_user = {
        "id": "user-current",
        "author": {"role": "user"},
        "recipient": "all",
        "content": {"content_type": "text", "parts": ["new question"]},
        "status": "finished_successfully",
        "create_time": 2,
        "metadata": {},
    }
    current = _assistant_terminal(
        "current report without citations",
        message_id="assistant-current",
        create_time=3,
    )
    backend = _SequenceBackend(
        [
            {
                "mapping": {
                    "old": {"message": stale},
                    "user": {"message": current_user},
                    "current": {"message": current},
                }
            }
        ]
    )
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _collect() -> list[dict[str, Any]]:
        return [
            event
            async for event in client._poll_dr_completion(
                "conversation-safe", interval=0, max_wait=1
            )
        ]

    events = asyncio.run(_collect())
    done = [event for event in events if event.get("type") == "done"][-1]

    assert done["text"] == "current report without citations"
    assert done["content_references"] == []
    assert done["search_result_groups"] == []
    assert stale_url not in repr(done)


def test_widget_report_rejects_oversized_text_and_references() -> None:
    oversized_text = _widget_report(
        "x" * (sse_mod._MAX_TOOL_TEXT_CHARS + 1),
        message_id="widget-large-text",
        create_time=1,
    )
    oversized_refs = _widget_report(
        "bounded text",
        message_id="widget-large-refs",
        create_time=1,
    )
    state = json.loads(
        oversized_refs["content"]["parts"][0].removeprefix("The latest state of the widget is: ")
    )
    state["report_message"]["metadata"]["content_references"] = [
        {"type": "webpage", "url": f"https://example.com/{index}"}
        for index in range(sse_mod._MAX_METADATA_LIST_ITEMS + 1)
    ]
    oversized_refs["content"]["parts"][0] = "The latest state of the widget is: " + json.dumps(
        state, separators=(",", ":")
    )

    for message in (oversized_text, oversized_refs):
        with pytest.raises(BackendContractError):
            sse_mod._dr_report_from_widget_state({"mapping": {"candidate": {"message": message}}})


@pytest.mark.parametrize(
    "parts",
    (
        "not-an-array",
        {"0": "not-an-array"},
        7,
        ["x"] * (sse_mod._MAX_TOOL_PARTS + 1),
        [7],
    ),
)
def test_widget_report_rejects_malformed_report_parts(parts: object) -> None:
    message = _widget_report("valid report", message_id="widget-parts", create_time=1)
    state = json.loads(
        message["content"]["parts"][0].removeprefix("The latest state of the widget is: ")
    )
    state["report_message"]["content"]["parts"] = parts
    message["content"]["parts"][0] = "The latest state of the widget is: " + json.dumps(
        state, separators=(",", ":")
    )

    with pytest.raises(BackendContractError, match="parts|array|string"):
        sse_mod._dr_report_from_widget_state({"mapping": {"candidate": {"message": message}}})


@pytest.mark.parametrize(
    "parts",
    (
        "not-an-array",
        {"0": "not-an-array"},
        7,
        ["x"] * (sse_mod._MAX_TOOL_PARTS + 1),
    ),
)
def test_widget_report_rejects_malformed_carrier_parts(parts: object) -> None:
    message = _widget_report("valid report", message_id="widget-carrier", create_time=1)
    message["content"]["parts"] = parts

    with pytest.raises(BackendContractError, match="parts|array"):
        sse_mod._dr_report_from_widget_state({"mapping": {"candidate": {"message": message}}})
