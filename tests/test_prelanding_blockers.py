from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest

from gpt2agent import server as server_mod
from gpt2agent import sse as sse_mod
from gpt2agent.errors import BackendContractError
from gpt2agent.tools.conversations import normalize_conversation_detail
from tests.test_audit_2026_07_09_streaming import (
    _Backend,
    _assistant_frame,
    _patch_sse_frames,
)


def _hidden_assistant_frame(
    text: str,
    *,
    message_id: str = "hidden-message",
    recipient: str = "all",
    create_time: int = 2,
) -> dict[str, Any]:
    frame = json.loads(_assistant_frame(text, "finished_successfully")[6:])
    frame["message"].update(
        {
            "id": message_id,
            "recipient": recipient,
            "create_time": create_time,
            "metadata": {"is_visually_hidden_from_conversation": True},
        }
    )
    return frame


def test_complete_drops_visually_hidden_assistant_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "h" * 24
    hidden = _hidden_assistant_frame(secret)
    visible = json.loads(_assistant_frame("safe final", "finished_successfully")[6:])
    visible["message"]["id"] = "visible-message"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps(hidden),
            "data: " + json.dumps(visible),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete("gpt-5-3", [{"role": "user", "content": "question"}])
    )

    assert result == (
        "safe final\n\n---\nTool activity receipt: `none`. "
        "Private dispatch payloads were withheld."
    )
    assert secret not in result


def test_complete_treats_explicit_null_metadata_as_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "n" * 24
    hidden = _hidden_assistant_frame(secret)
    hidden["message"]["metadata"] = None
    visible = json.loads(_assistant_frame("safe final", "finished_successfully")[6:])
    visible["message"]["id"] = "visible-message"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps(hidden),
            "data: " + json.dumps(visible),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete("gpt-5-3", [{"role": "user", "content": "question"}])
    )

    assert result == (
        "safe final\n\n---\nTool activity receipt: `none`. "
        "Private dispatch payloads were withheld."
    )
    assert secret not in result


def test_tool_call_drops_visually_hidden_dispatch_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "t" * 24
    hidden = _hidden_assistant_frame(
        secret,
        recipient="api_tool.private_connector",
    )
    visible = json.loads(_assistant_frame("safe final", "finished_successfully")[6:])
    visible["message"]["id"] = "visible-message"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps(hidden),
            "data: " + json.dumps(visible),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(client.tool_call("run safely"))

    assert result["text"] == "safe final"
    assert result["tool_calls"] == []
    assert secret not in repr(result)


def test_conversation_detail_drops_visually_hidden_messages() -> None:
    secret = "sk-" + "c" * 24
    detail = {
        "id": "conversation-safe",
        "mapping": {
            "hidden": {"message": _hidden_assistant_frame(secret)["message"]},
            "visible": {
                "message": {
                    "id": "visible-message",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["safe final"]},
                    "status": "finished_successfully",
                    "metadata": {},
                }
            },
        },
    }

    result = normalize_conversation_detail(
        detail,
        expected_id="conversation-safe",
        max_messages=100,
    )

    assert result["message_count"] == 1
    assert [message["text"] for message in result["messages"]] == ["safe final"]
    assert secret not in repr(result)


def test_async_poll_drops_hidden_assistant_before_newer_visible_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "a" * 24

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {
                "mapping": {
                    "hidden": {"message": _hidden_assistant_frame(secret)["message"]},
                    "visible": {
                        "message": {
                            "author": {"role": "assistant"},
                            "recipient": "all",
                            "content": {
                                "content_type": "text",
                                "parts": ["safe final"],
                            },
                            "status": "finished_successfully",
                            "create_time": 3,
                            "metadata": {},
                        }
                    },
                }
            }

    async def _no_sleep(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(sse_mod.asyncio, "sleep", _no_sleep)
    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    result = asyncio.run(client._poll_async_response("conversation-safe"))

    assert result == (
        "safe final\n\n---\nTool activity receipt: `none`. "
        "Private dispatch payloads were withheld."
    )
    assert secret not in result


def test_dr_poll_drops_hidden_assistant_before_newer_visible_terminal() -> None:
    secret = "sk-" + "r" * 24

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {
                "mapping": {
                    "hidden": {"message": _hidden_assistant_frame(secret)["message"]},
                    "visible": {
                        "message": {
                            "author": {"role": "assistant"},
                            "recipient": "all",
                            "content": {
                                "content_type": "text",
                                "parts": ["safe report"],
                            },
                            "status": "finished_successfully",
                            "create_time": 3,
                            "metadata": {},
                        }
                    },
                }
            }

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [
            event
            async for event in client._poll_dr_completion(
                "conversation-safe", interval=0, max_wait=1
            )
        ]

    events = asyncio.run(_collect())

    done = [event for event in events if event.get("type") == "done"]
    assert done[-1]["text"] == "safe report"
    assert secret not in repr(events)


def test_light_dr_stream_drops_visually_hidden_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "l" * 24
    hidden = _hidden_assistant_frame(secret)
    hidden["conversation_id"] = "conversation-safe"
    visible = json.loads(_assistant_frame("safe report", "finished_successfully")[6:])
    visible["conversation_id"] = "conversation-safe"
    visible["message"]["id"] = "visible-message"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps(hidden),
            "data: " + json.dumps(visible),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [
            event
            async for event in client.deep_research(
                "question", max_clarification_rounds=0
            )
        ]

    events = asyncio.run(_collect())

    assert [event["text"] for event in events if event["type"] == "done"] == [
        "safe report"
    ]
    assert secret not in repr(events)


def test_heavy_dr_stream_drops_visually_hidden_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "v" * 24
    hidden = _hidden_assistant_frame(secret)
    hidden["conversation_id"] = "conversation-safe"
    visible = json.loads(_assistant_frame("safe report", "finished_successfully")[6:])
    visible["conversation_id"] = "conversation-safe"
    visible["message"]["id"] = "visible-message"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps({"v": {"message": hidden["message"]}}),
            "data: " + json.dumps({"v": {"message": visible["message"]}}),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [
            event async for event in client.deep_research_heavy("question")
        ]

    events = asyncio.run(_collect())

    assert [event["text"] for event in events if event["type"] == "done"] == [
        "safe report"
    ]
    assert secret not in repr(events)


def test_heavy_dr_hidden_envelope_suppresses_following_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "d" * 24
    hidden = _hidden_assistant_frame("", message_id="hidden-message")
    hidden["message"]["status"] = "in_progress"
    visible = json.loads(_assistant_frame("safe report", "finished_successfully")[6:])
    visible["message"]["id"] = "visible-message"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps({"v": {"message": hidden["message"]}}),
            "data: "
            + json.dumps(
                {
                    "p": "/message/content/parts/0",
                    "o": "append",
                    "v": secret,
                }
            ),
            "data: " + json.dumps({"v": {"message": visible["message"]}}),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [event async for event in client.deep_research_heavy("question")]

    events = asyncio.run(_collect())

    assert [event["text"] for event in events if event["type"] == "done"] == [
        "safe report"
    ]
    assert secret not in repr(events)


def test_heavy_dr_hidden_tool_envelope_resets_visible_patch_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "w" * 24
    visible_partial = json.loads(_assistant_frame("prefix", "in_progress")[6:])
    hidden_tool = {
        "id": "hidden-tool",
        "author": {"role": "tool"},
        "recipient": "all",
        "content": {"content_type": "text", "parts": []},
        "status": "in_progress",
        "metadata": {"is_visually_hidden_from_conversation": True},
    }
    visible_final = json.loads(
        _assistant_frame("safe report", "finished_successfully")[6:]
    )
    visible_final["message"]["id"] = "visible-final"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps({"v": {"message": visible_partial["message"]}}),
            "data: " + json.dumps({"v": {"message": hidden_tool}}),
            "data: "
            + json.dumps(
                {
                    "p": "/message/content/parts/0",
                    "o": "append",
                    "v": secret,
                }
            ),
            "data: " + json.dumps({"v": {"message": visible_final["message"]}}),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [event async for event in client.deep_research_heavy("question")]

    events = asyncio.run(_collect())

    assert events[-1]["text"] == "safe report"
    assert secret not in repr(events)


def test_heavy_dr_buffers_text_until_late_visibility_patch_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "b" * 24
    provisional = json.loads(_assistant_frame(secret, "in_progress")[6:])
    provisional["message"]["id"] = "provisional-message"
    visible_final = json.loads(
        _assistant_frame("safe report", "finished_successfully")[6:]
    )
    visible_final["message"]["id"] = "visible-final"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps({"v": {"message": provisional["message"]}}),
            "data: "
            + json.dumps(
                {
                    "p": (
                        "/message/metadata/"
                        "is_visually_hidden_from_conversation"
                    ),
                    "o": "replace",
                    "v": True,
                }
            ),
            "data: " + json.dumps({"v": {"message": visible_final["message"]}}),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [event async for event in client.deep_research_heavy("question")]

    events = asyncio.run(_collect())

    assert events[-1]["text"] == "safe report"
    assert secret not in repr(events)


def test_regular_stream_buffers_text_until_late_visibility_patch_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "q" * 24
    provisional = json.loads(_assistant_frame("", "in_progress")[6:])
    provisional["message"].update({"id": "provisional", "metadata": {}})
    visible_final = json.loads(
        _assistant_frame("safe final", "finished_successfully")[6:]
    )
    visible_final["message"]["id"] = "visible-final"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps({"v": {"message": provisional["message"]}}),
            "data: "
            + json.dumps(
                {
                    "p": "/message/content/parts/0",
                    "o": "append",
                    "v": secret,
                }
            ),
            "data: "
            + json.dumps(
                {
                    "p": (
                        "/message/metadata/"
                        "is_visually_hidden_from_conversation"
                    ),
                    "o": "replace",
                    "v": True,
                }
            ),
            "data: " + json.dumps(visible_final),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete("gpt-5-3", [{"role": "user", "content": "question"}])
    )

    assert result.startswith("safe final\n\n---\nTool activity receipt: `none`.")
    assert secret not in result


def test_regular_stream_never_restores_visibility_for_same_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "y" * 24
    provisional = json.loads(_assistant_frame("", "in_progress")[6:])
    provisional["message"].update({"id": "same-message", "metadata": {}})
    restored = deepcopy(provisional)
    restored["message"]["content"]["parts"] = [secret]
    restored["message"]["status"] = "finished_successfully"
    visible_final = json.loads(
        _assistant_frame("safe final", "finished_successfully")[6:]
    )
    visible_final["message"]["id"] = "visible-final"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps({"v": {"message": provisional["message"]}}),
            "data: "
            + json.dumps(
                {
                    "p": (
                        "/message/metadata/"
                        "is_visually_hidden_from_conversation"
                    ),
                    "o": "add",
                    "v": True,
                }
            ),
            "data: " + json.dumps({"v": {"message": restored["message"]}}),
            "data: " + json.dumps(visible_final),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete("gpt-5-3", [{"role": "user", "content": "question"}])
    )

    assert result.startswith("safe final\n\n---\nTool activity receipt: `none`.")
    assert secret not in result


def test_regular_stream_revokes_earlier_message_when_hidden_snapshot_reappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "z" * 24
    earlier = json.loads(
        _assistant_frame(secret, "finished_successfully")[6:]
    )
    earlier["message"]["id"] = "earlier-message"
    safe = json.loads(
        _assistant_frame("safe final", "finished_successfully")[6:]
    )
    safe["message"]["id"] = "safe-message"
    revoked = deepcopy(earlier)
    revoked["message"]["metadata"] = {
        "is_visually_hidden_from_conversation": True,
    }
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps(earlier),
            "data: " + json.dumps(safe),
            "data: " + json.dumps(revoked),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete("gpt-5-3", [{"role": "user", "content": "question"}])
    )

    assert result.startswith("safe final\n\n---\nTool activity receipt: `none`.")
    assert secret not in result


def test_heavy_dr_newer_hidden_tool_invalidates_stale_finished_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = json.loads(
        _assistant_frame("stale report", "finished_successfully")[6:]
    )
    stale["message"]["id"] = "stale-report"
    hidden_tool = {
        "id": "tool-lifecycle",
        "author": {"role": "tool"},
        "recipient": "all",
        "content": {"content_type": "text", "parts": ["private tool state"]},
        "status": "in_progress",
        "metadata": {"is_visually_hidden_from_conversation": True},
    }
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps({"v": {"message": stale["message"]}}),
            "data: " + json.dumps({"v": {"message": hidden_tool}}),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [event async for event in client.deep_research_heavy("question")]

    events = asyncio.run(_collect())

    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == ""
    assert events[-1]["terminated_abnormally"] is True
    assert "stale report" not in repr(events)


def test_heavy_dr_never_exposes_raw_tool_dispatch_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "u" * 24
    dispatch = {
        "id": "dispatch",
        "author": {"role": "assistant"},
        "recipient": "api_tool_chatgpt_deep_research",
        "content": {"content_type": "text", "parts": [secret]},
        "status": "in_progress",
        "metadata": {},
    }
    visible_final = json.loads(
        _assistant_frame("safe report", "finished_successfully")[6:]
    )
    visible_final["message"]["id"] = "visible-final"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps({"v": {"message": dispatch}}),
            "data: "
            + json.dumps(
                {
                    "p": (
                        "/message/metadata/"
                        "is_visually_hidden_from_conversation"
                    ),
                    "o": "replace",
                    "v": True,
                }
            ),
            "data: " + json.dumps({"v": {"message": visible_final["message"]}}),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [event async for event in client.deep_research_heavy("question")]

    events = asyncio.run(_collect())

    assert {"type": "tool", "call": "web_search", "category": "web"} in events
    assert secret not in repr(events)


def test_light_dr_hidden_newer_lifecycle_invalidates_earlier_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible = json.loads(_assistant_frame("stale report", "finished_successfully")[6:])
    visible["conversation_id"] = "conversation-safe"
    hidden = _hidden_assistant_frame(
        "private dispatch",
        message_id="hidden-dispatch",
        recipient="api_tool.deep_research",
    )
    hidden["message"]["status"] = "in_progress"
    hidden["conversation_id"] = "conversation-safe"
    _patch_sse_frames(
        monkeypatch,
        ["data: " + json.dumps(visible), "data: " + json.dumps(hidden)],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [
            event
            async for event in client.deep_research(
                "question", max_clarification_rounds=0
            )
        ]

    events = asyncio.run(_collect())

    assert events[-1]["type"] == "done"
    assert events[-1]["terminated_abnormally"] is True
    assert events[-1]["text"] == ""


def test_tool_call_hidden_newer_lifecycle_invalidates_earlier_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible = json.loads(_assistant_frame("stale answer", "finished_successfully")[6:])
    hidden = _hidden_assistant_frame(
        "private dispatch",
        message_id="hidden-dispatch",
        recipient="api_tool.private_connector",
    )
    hidden["message"]["status"] = "in_progress"
    _patch_sse_frames(
        monkeypatch,
        ["data: " + json.dumps(visible), "data: " + json.dumps(hidden)],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="ended before completion"):
        asyncio.run(client.tool_call("question"))


def test_heavy_dr_projects_server_metadata_to_static_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "m" * 24
    visible = json.loads(_assistant_frame("safe report", "finished_successfully")[6:])
    _patch_sse_frames(
        monkeypatch,
        [
            "data: "
            + json.dumps(
                {
                    "type": "server_ste_metadata",
                    "metadata": {
                        "tool_invoked": True,
                        "request_id": secret,
                        "opaque": {"private": secret},
                    },
                }
            ),
            "data: " + json.dumps({"v": {"message": visible["message"]}}),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [event async for event in client.deep_research_heavy("question")]

    events = asyncio.run(_collect())

    assert [event for event in events if event["type"] == "meta"] == [
        {
            "type": "meta",
            "tool_invoked": True,
            "tool_category": "connector",
        }
    ]
    assert secret not in repr(events)


@pytest.mark.parametrize("hidden_flag", [True, None, "false", 0])
def test_explicit_non_boolean_false_visibility_is_fail_closed(
    hidden_flag: object,
) -> None:
    detail = {
        "id": "conversation-safe",
        "mapping": {
            "message": {
                "message": {
                    **_hidden_assistant_frame("private")["message"],
                    "metadata": {
                        "is_visually_hidden_from_conversation": hidden_flag
                    },
                }
            }
        },
    }

    result = normalize_conversation_detail(
        detail,
        expected_id="conversation-safe",
        max_messages=100,
    )

    assert result["messages"] == []


def test_image_poll_rejects_backend_conversation_path_injection() -> None:
    class _PollingBackend(_Backend):
        def __init__(self) -> None:
            self.paths: list[str] = []

        def get(self, path: str, **__: Any) -> dict:
            self.paths.append(path)
            return {
                "mapping": {
                    "image": {
                        "message": {
                            "author": {"role": "tool"},
                            "content": {
                                "content_type": "multimodal_text",
                                "parts": [
                                    {
                                        "content_type": "image_asset_pointer",
                                        "asset_pointer": "sediment://file-safe",
                                    }
                                ],
                            },
                            "create_time": 1,
                        }
                    }
                }
            }

    backend = _PollingBackend()
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    with pytest.raises(BackendContractError, match="conversation_id"):
        asyncio.run(
            client._poll_image_result("../me?x=1", poll_interval=0, max_wait=1)
        )

    assert backend.paths == []


def test_heavy_dr_poll_rejects_backend_conversation_path_injection() -> None:
    class _PollingBackend(_Backend):
        def __init__(self) -> None:
            self.paths: list[str] = []

        def get(self, path: str, **__: Any) -> dict:
            self.paths.append(path)
            return {}

    backend = _PollingBackend()
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        return [
            event
            async for event in client._poll_dr_completion(
                "../me?x=1", interval=0, max_wait=1
            )
        ]

    with pytest.raises(BackendContractError, match="conversation_id"):
        asyncio.run(_collect())

    assert backend.paths == []


def test_build_server_propagates_tool_registration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gpt2agent.backend as backend_mod
    import gpt2agent.model_catalog as model_catalog_mod
    import gpt2agent.tools as tools_mod

    class _BackendStub:
        pass

    class _CatalogStub:
        def __init__(self, _backend: object) -> None:
            pass

    class _ConversationStub:
        def __init__(self, _backend: object) -> None:
            pass

    def _fail_registration(*_: Any, **__: Any) -> None:
        raise RuntimeError("synthetic registration failure")

    monkeypatch.setattr(backend_mod, "BackendClient", _BackendStub)
    monkeypatch.setattr(model_catalog_mod, "ModelCatalog", _CatalogStub)
    monkeypatch.setattr(sse_mod, "ConversationClient", _ConversationStub)
    monkeypatch.setattr(tools_mod, "register_all", _fail_registration)

    with pytest.raises(RuntimeError, match="synthetic registration failure"):
        server_mod.build_server(
            {
                "server": {"host": "127.0.0.1", "port": 9000},
                "models": {"chat": "gpt-5-3"},
            }
        )
