from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

import gpt2agent.backend as backend_module
import gpt2agent.server as server_module
import gpt2agent.sse as sse_module
import gpt2agent.tool_contracts as tool_contracts


_FIELDS = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)

_EXPECTED: dict[str, tuple[bool, bool, bool, bool]] = {
    "chat": (False, False, False, True),
    "agent": (False, True, False, True),
    "deep_research": (False, False, False, True),
    "deep_research_heavy": (False, False, False, True),
    "gpt_chat": (False, True, False, True),
    "memory_create_via_chat": (False, True, False, False),
    "account_status": (True, False, True, False),
    "list_models": (True, False, True, False),
    "memory_list": (True, False, True, False),
    "memory_search": (True, False, True, False),
    "custom_instructions_get": (True, False, True, False),
    "list_codex_envs": (True, False, True, False),
    "list_codex_tasks": (True, False, True, False),
    "list_custom_gpts": (True, False, True, False),
    "list_conversations": (True, False, True, False),
    "get_conversation": (True, False, True, False),
    "list_tasks": (True, False, True, False),
    "list_apps": (True, False, True, False),
    "custom_instructions_set": (False, True, True, False),
    "codex_task_create": (False, True, False, True),
    "generate_image": (False, False, False, False),
    "get_file_info": (True, False, True, False),
    "get_file_download_url": (True, False, True, False),
    "code_interpreter": (False, False, False, False),
    "canvas_execute": (False, False, False, False),
    "list_scheduled_tasks": (True, False, True, False),
    "list_plugins": (True, False, True, False),
    "list_installed_plugins": (True, False, True, False),
    "list_work_models": (True, False, True, False),
    "sites_access": (True, False, True, False),
    "list_sites": (True, False, True, False),
    "account_capabilities": (True, False, True, False),
}

class _RecordingMCP:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.registrations: dict[str, tuple[Any, dict[str, Any]]] = {}
        self.resources: dict[str, tuple[Any, dict[str, Any]]] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def decorator(fn):
            self.registrations[fn.__name__] = (fn, kwargs)
            return fn

        return decorator

    def resource(self, uri: str, *args: Any, **kwargs: Any):
        def decorator(fn):
            self.resources[uri] = (fn, kwargs)
            return fn

        return decorator


class _Backend:
    pass


class _Conversation:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend


def _annotation_tuple(value: ToolAnnotations) -> tuple[bool | None, ...]:
    return tuple(getattr(value, field) for field in _FIELDS)


def _registered_tools(monkeypatch) -> dict[str, tuple[Any, dict[str, Any]]]:
    monkeypatch.setattr(server_module, "FastMCP", _RecordingMCP)
    monkeypatch.setattr(backend_module, "BackendClient", _Backend)
    monkeypatch.setattr(sse_module, "ConversationClient", _Conversation)

    mcp = server_module.build_server(
        {
            "server": {"host": "127.0.0.1", "port": 9000},
            "models": {"chat": "gpt-5-3", "agent": "agent-mode"},
        }
    )
    return mcp.registrations


def test_annotation_manifest_is_exact() -> None:
    actual = {
        name: tuple(values[field] for field in _FIELDS)
        for name, values in tool_contracts.TOOL_ANNOTATION_MANIFEST.items()
    }
    assert actual == _EXPECTED


def test_tool_annotations_returns_fresh_explicit_models() -> None:
    first = tool_contracts.tool_annotations("account_status")
    second = tool_contracts.tool_annotations("account_status")
    assert isinstance(first, ToolAnnotations)
    assert first is not second
    assert _annotation_tuple(first) == _EXPECTED["account_status"]
    assert all(value is not None for value in _annotation_tuple(first))


def test_complete_registry_emits_exact_annotations_without_output_changes(monkeypatch) -> None:
    registrations = _registered_tools(monkeypatch)
    assert set(registrations) == set(_EXPECTED)

    for name, (fn, kwargs) in registrations.items():
        assert fn.__name__ == name
        assert "structured_output" not in kwargs
        annotation = kwargs.get("annotations")
        assert isinstance(annotation, ToolAnnotations), name
        assert _annotation_tuple(annotation) == _EXPECTED[name]
