from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest
from mcp.types import ToolAnnotations

import gpt2agent.backend as backend_module
import gpt2agent.server as server_module
import gpt2agent.sse as sse_module
from gpt2agent.errors import InputValidationError
from gpt2agent.grok_build import GrokBuildClient
from gpt2agent.tool_manifest import CHATGPT_TOOL_NAMES, GROK_TOOL_NAMES, TOOL_NAMES
from gpt2agent.tools import grok_build


_FIELDS = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)

GROK_BUILD_ANNOTATIONS = {
    "grok_build_agent": (False, True, False, True),
    "grok_build_models": (True, False, True, False),
    "grok_build_status": (True, False, True, False),
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


class _FakeBuildClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.results = {
            "agent": {"surface": "build", "status": "completed", "text": "done"},
            "models": {"authenticated": True, "models": ["grok-4.5"]},
            "status": {"installed": True, "authenticated": True},
        }

    async def agent(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("agent", args, kwargs))
        return self.results["agent"]

    async def models(self) -> dict[str, Any]:
        self.calls.append(("models", (), {}))
        return self.results["models"]

    async def status(self) -> dict[str, Any]:
        self.calls.append(("status", (), {}))
        return self.results["status"]


class _Backend:
    pass


class _Conversation:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend


def _registered(client: _FakeBuildClient | None = None):
    mcp = _RecordingMCP()
    build_client = client or _FakeBuildClient()
    grok_build.register(mcp, build_client)
    return mcp.registrations, build_client


def _annotation_tuple(value: ToolAnnotations) -> tuple[bool | None, ...]:
    return tuple(getattr(value, field) for field in _FIELDS)


def test_grok_build_registry_is_exact_and_fully_annotated() -> None:
    registrations, _ = _registered()

    assert set(registrations) == set(GROK_BUILD_ANNOTATIONS)
    for name, (fn, kwargs) in registrations.items():
        assert fn.__name__ == name
        assert "structured_output" not in kwargs
        annotation = kwargs.get("annotations")
        assert isinstance(annotation, ToolAnnotations), name
        assert _annotation_tuple(annotation) == GROK_BUILD_ANNOTATIONS[name]
        assert all(value is not None for value in _annotation_tuple(annotation))


def test_grok_build_public_signatures_are_exact_and_secret_free() -> None:
    registrations, _ = _registered()
    agent = inspect.signature(registrations["grok_build_agent"][0])

    assert tuple(agent.parameters) == (
        "prompt",
        "cwd",
        "mode",
        "model",
        "max_turns",
        "subagents",
    )
    assert agent.parameters["prompt"].annotation == "str"
    assert agent.parameters["cwd"].annotation == "str | None"
    assert agent.parameters["cwd"].default is None
    assert agent.parameters["mode"].annotation == "Literal['plan', 'apply']"
    assert agent.parameters["mode"].default == "plan"
    assert agent.parameters["model"].annotation == "str | None"
    assert agent.parameters["model"].default is None
    assert agent.parameters["max_turns"].annotation == "int | None"
    assert agent.parameters["max_turns"].default is None
    assert agent.parameters["subagents"].annotation == "bool"
    assert agent.parameters["subagents"].default is False
    assert tuple(inspect.signature(registrations["grok_build_models"][0]).parameters) == ()
    assert tuple(inspect.signature(registrations["grok_build_status"][0]).parameters) == ()

    all_parameters = {
        parameter
        for fn, _kwargs in registrations.values()
        for parameter in inspect.signature(fn).parameters
    }
    assert all_parameters.isdisjoint(
        {"auth", "authorization", "cookie", "credential", "key", "secret", "token"}
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": ""},
        {"prompt": 7},
        {"prompt": "ok", "cwd": 7},
        {"prompt": "ok", "mode": "workspace"},
        {"prompt": "ok", "model": 7},
        {"prompt": "ok", "max_turns": True},
        {"prompt": "ok", "max_turns": 0},
        {"prompt": "ok", "max_turns": 101},
        {"prompt": "ok", "subagents": 1},
    ],
)
def test_grok_build_agent_validates_public_inputs_before_dispatch(
    kwargs: dict[str, Any],
) -> None:
    registrations, client = _registered()

    with pytest.raises(InputValidationError):
        asyncio.run(registrations["grok_build_agent"][0](**kwargs))

    assert client.calls == []


def test_grok_build_agent_calls_client_once_and_returns_exact_object() -> None:
    registrations, client = _registered()

    result = asyncio.run(
        registrations["grok_build_agent"][0](
            "inspect only",
            cwd="/repo",
            mode="apply",
            model="grok-4.5",
            max_turns=8,
            subagents=True,
        )
    )

    assert result is client.results["agent"]
    assert client.calls == [
        (
            "agent",
            ("inspect only",),
            {
                "cwd": "/repo",
                "mode": "apply",
                "model": "grok-4.5",
                "max_turns": 8,
                "subagents": True,
            },
        )
    ]


@pytest.mark.parametrize("tool_name, method_name", [("grok_build_models", "models"), ("grok_build_status", "status")])
def test_grok_build_read_handlers_call_client_once_and_return_exact_object(
    tool_name: str,
    method_name: str,
) -> None:
    registrations, client = _registered()

    result = asyncio.run(registrations[tool_name][0]())

    assert result is client.results[method_name]
    assert client.calls == [(method_name, (), {})]


def test_provider_manifests_partition_the_exact_global_manifest() -> None:
    assert GROK_TOOL_NAMES == tuple(name for name in TOOL_NAMES if name.startswith("grok_"))
    assert CHATGPT_TOOL_NAMES == tuple(
        name for name in TOOL_NAMES if not name.startswith("grok_")
    )
    assert GROK_TOOL_NAMES == tuple(GROK_BUILD_ANNOTATIONS)
    assert set(CHATGPT_TOOL_NAMES).isdisjoint(GROK_TOOL_NAMES)
    assert len(CHATGPT_TOOL_NAMES) + len(GROK_TOOL_NAMES) == len(TOOL_NAMES)


def test_server_registers_build_tools_without_binary_roots_or_auth(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "GROK_AUTH_PATH",
        "GROK_CODE_XAI_API_KEY",
        "GROK_HOME",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    def unexpected_startup_probe(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Grok Build account or execution probe ran during startup")

    for method_name in ("_run", "status", "models", "agent"):
        monkeypatch.setattr(
            GrokBuildClient,
            method_name,
            unexpected_startup_probe,
        )
    monkeypatch.setattr(server_module, "FastMCP", _RecordingMCP)
    monkeypatch.setattr(backend_module, "BackendClient", _Backend)
    monkeypatch.setattr(sse_module, "ConversationClient", _Conversation)

    mcp = server_module.build_server(
        {
            "server": {"host": "127.0.0.1", "port": 9000},
            "models": {"chat": "gpt-5-3"},
            "grok_build": {"command": str(tmp_path / "missing-grok"), "roots": []},
        }
    )

    assert set(GROK_BUILD_ANNOTATIONS) <= set(mcp.registrations)
