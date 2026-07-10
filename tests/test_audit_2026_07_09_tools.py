"""Release-v0.0.10 regressions for REST tool privacy and responsiveness.

All backend I/O is provided by local fakes; these tests never use a live account.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from time import sleep
from typing import Any

import pytest

from gpt2agent.tools import (
    account,
    apps,
    codex,
    conversations,
    gpts,
    images,
    instructions,
    memory,
    voice,
    writes,
)
from gpt2agent.tools._redact import redact


class _MCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorate


class _Client:
    def __init__(self, *, gets: dict[str, Any] | None = None) -> None:
        self.get_results = gets or {}
        self.requests: list[tuple[str, str, Any]] = []

    def get(self, path: str, **kwargs: Any) -> Any:
        self.requests.append(("GET", path, kwargs))
        return self.get_results.get(path, {})

    def post(self, path: str, **kwargs: Any) -> Any:
        self.requests.append(("POST", path, kwargs))
        return kwargs.get("json", {})


def _register(module, client: _Client) -> _MCP:
    mcp = _MCP()
    if module is images:
        module.register(mcp, client, conv=object())
    else:
        module.register(mcp, client)
    return mcp


async def _invoke(fn, *args: Any, **kwargs: Any) -> Any:
    """Invoke either the pre-hardening sync handler or its async replacement."""
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["../me", "%2e%2e/me", "a/b", "a%2fb", "a?x", "a#x", "", ".", ".."],
)
async def test_file_tools_reject_non_segment_ids(bad: str) -> None:
    client = _Client()
    tools = _register(images, client).tools

    for name in ("get_file_info", "get_file_download_url"):
        client.requests.clear()
        with pytest.raises(ValueError, match="invalid file ID"):
            await _invoke(tools[name], bad)
        assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["a\x00b", "a" * 257])
async def test_file_tools_reject_control_and_overlong_ids(bad: str) -> None:
    client = _Client()
    tool = _register(images, client).tools["get_file_info"]

    with pytest.raises(ValueError, match="invalid file ID"):
        await _invoke(tool, bad)
    assert client.requests == []


@pytest.mark.asyncio
async def test_generated_asset_rejects_invalid_backend_file_id_before_request() -> None:
    class _ImageConversation:
        async def image_gen(self, prompt: str, model: str) -> dict:
            return {"assets": [{"file_id": "../me"}]}

    client = _Client()
    mcp = _MCP()
    images.register(mcp, client, conv=_ImageConversation())

    with pytest.raises(ValueError, match="invalid file ID"):
        await mcp.tools["generate_image"]("draw a test", "gpt-5-3")
    assert client.requests == []


@pytest.mark.asyncio
async def test_conversation_tool_rejects_non_segment_id_before_request() -> None:
    client = _Client()
    tool = _register(conversations, client).tools["get_conversation"]

    with pytest.raises(ValueError, match="invalid conversation ID"):
        await _invoke(tool, "../me")
    assert client.requests == []


@pytest.mark.asyncio
async def test_memory_search_cannot_distinguish_redacted_secret_prefixes() -> None:
    secret = "sk-ABCDEFGHIJKLMNOPQRST"
    client = _Client(
        gets={
            "/backend-api/memories": {
                "memories": [
                    {"id": "m1", "content": f"credential {secret}", "created_timestamp": 1}
                ]
            }
        }
    )
    tool = _register(memory, client).tools["memory_search"]

    assert await _invoke(tool, "sk-ABC") == []
    assert await _invoke(tool, "sk-ABD") == []


@pytest.mark.asyncio
async def test_custom_instruction_setter_never_echoes_preserved_secrets() -> None:
    email = "private@example.com"
    api_key = "sk-ABCDEFGHIJKLMNOPQRST"
    path = "/backend-api/user_system_messages"
    client = _Client(
        gets={
            path: {
                "enabled": True,
                "about_user_message": f"contact {email}; credential {api_key}",
                "about_model_message": "old",
            }
        }
    )
    tool = _register(writes, client).tools["custom_instructions_set"]

    result = await _invoke(tool, about_model="new")
    assert result == {"updated": True, "fields": ["about_model"]}
    assert email not in repr(result)
    assert api_key not in repr(result)

    no_op_client = _Client(gets={path: {"about_user_message": email}})
    no_op_tool = _register(writes, no_op_client).tools["custom_instructions_set"]
    with pytest.raises(ValueError, match="at least one"):
        await _invoke(no_op_tool)
    assert no_op_client.requests == []


@pytest.mark.asyncio
async def test_custom_instruction_setter_serializes_read_modify_write() -> None:
    path = "/backend-api/user_system_messages"

    class _ConcurrentClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.state = {
                "about_user_message": "old user",
                "about_model_message": "old model",
            }
            self._state_lock = threading.Lock()
            self._second_get_started = threading.Event()
            self._get_count = 0

        def get(self, request_path: str, **kwargs: Any) -> dict:
            assert request_path == path
            with self._state_lock:
                self._get_count += 1
                call_number = self._get_count
                snapshot = dict(self.state)
            if call_number == 1:
                # Without a handler-level RMW lock, the second task reaches GET
                # and both callers retain the same stale snapshot. With the lock,
                # this bounded wait expires and the second GET observes the POST.
                self._second_get_started.wait(timeout=0.2)
            else:
                self._second_get_started.set()
            return snapshot

        def post(self, request_path: str, **kwargs: Any) -> dict:
            assert request_path == path
            with self._state_lock:
                self.state = dict(kwargs["json"])
            return dict(self.state)

    client = _ConcurrentClient()
    tool = _register(writes, client).tools["custom_instructions_set"]

    await asyncio.gather(
        tool(about_user="new user"),
        tool(about_model="new model"),
    )

    assert client.state["about_user_message"] == "new user"
    assert client.state["about_model_message"] == "new model"


@pytest.mark.asyncio
async def test_slow_rest_backend_does_not_block_event_loop() -> None:
    class _SlowClient(_Client):
        def get(self, path: str, **kwargs: Any) -> Any:
            sleep(0.30)
            return {"models": []}

    tool = _register(account, _SlowClient()).tools["list_models"]
    tool_task = asyncio.create_task(_invoke(tool))
    heartbeat = asyncio.create_task(asyncio.sleep(0.05))
    try:
        done, _ = await asyncio.wait(
            {tool_task, heartbeat},
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert heartbeat in done
        assert tool_task not in done
    finally:
        await tool_task
        await heartbeat


def test_redact_handles_thousands_of_dates_without_recursion() -> None:
    dates = " ".join(["2026-05-26"] * 1_200)
    result = redact(f"{dates} 617-555-0123")

    assert result.count("2026-05-26") == 1_200
    assert result.endswith("<PHONE>")


@pytest.mark.asyncio
async def test_get_conversation_caps_visible_messages_after_filtering() -> None:
    def node(node_id: str, parent: str | None, role: str, text: str) -> dict:
        return {
            "id": node_id,
            "parent": parent,
            "message": {
                "id": node_id,
                "author": {"role": role},
                "content": {"content_type": "text", "parts": [text]},
            },
        }

    detail = {
        "id": "c1",
        "current_node": "s2",
        "mapping": {
            "u": node("u", None, "user", "question"),
            "a": node("a", "u", "assistant", "answer"),
            "s1": node("s1", "a", "system", "hidden one"),
            "s2": node("s2", "s1", "system", "hidden two"),
        },
    }
    client = _Client(gets={"/backend-api/conversation/c1": detail})
    tool = _register(conversations, client).tools["get_conversation"]

    result = await _invoke(tool, "c1", max_messages=2)
    assert [message["text"] for message in result["messages"]] == ["question", "answer"]
    assert result["message_count"] == 2


def test_all_registered_rest_handlers_are_async() -> None:
    client = _Client()
    expected = {
        account: {"account_status", "list_models"},
        apps: {"list_apps"},
        codex: {"list_codex_envs", "list_codex_tasks"},
        conversations: {"list_conversations", "get_conversation", "list_tasks"},
        gpts: {"list_custom_gpts"},
        images: {"generate_image", "get_file_info", "get_file_download_url"},
        instructions: {"custom_instructions_get"},
        memory: {"memory_list", "memory_search"},
        voice: {"list_voices"},
        writes: {"custom_instructions_set", "codex_task_create"},
    }

    for module, names in expected.items():
        tools = _register(module, client).tools
        assert set(tools) == names
        assert all(inspect.iscoroutinefunction(tool) for tool in tools.values())
