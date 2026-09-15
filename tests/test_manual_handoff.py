"""Tests for Phase 0 `manual=True` handoff mode on conversation-class tools.

Contract (docs/dev/specs/phase0-manual-handoff.md): every conversation-class
MCP tool gains a `manual: bool = False` parameter (appended last). With
`manual=True` the tool makes ZERO backend/conv calls and early-returns
`json.dumps(handoff, indent=2)` — a JSON string for all 9 tools, even the
dict-returning ones. `handoff["prompt"]` must be byte-equal to the text the
REST/SSE path compiles for the same input.

Self-contained: reuses the FakeMCP / _run / _FailConv / _build_with_conv
patterns from test_tools.py without importing it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

from gpt2agent.tools import images, tools_features

# Byte-exact prompt wrappers the REST/SSE path compiles (hardcoded as the
# strongest lock — the manual prompt must equal the REST prompt byte-for-byte).
DR_PREFIX = (
    "Begin the deep research immediately without asking for confirmation. "
    "Do not ask clarifying questions; proceed with the best interpretation. "
)
MEMORY_PREFIX = (
    "Please commit the following to memory verbatim. "
    "Do not summarize, paraphrase, or ask for confirmation:\n\n"
)
CHATGPT_URL = "https://chatgpt.com/"
READBACK = {"list": "list_conversations", "fetch": "get_conversation"}

SERVER_TOOL_NAMES = (
    "chat",
    "agent",
    "deep_research",
    "deep_research_heavy",
    "gpt_chat",
    "memory_create_via_chat",
)


class FakeMCP:
    """Captures @mcp.tool()-decorated functions by name (decorator returns fn unchanged)."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a: Any, **k: Any):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _FailClient:
    """Any REST call is a failure while exercising the manual path."""

    def get(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")

    def post(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")


class _FailConv:
    """Any SSE/conv call is a failure while exercising the manual path."""

    async def complete(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")

    def deep_research(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")

    def deep_research_heavy(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")

    async def tool_call(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")

    async def image_gen(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")


def _run(fn, *args: Any, **kwargs: Any) -> Any:
    return asyncio.run(fn(*args, **kwargs))


def _reg(module, client: Any, conv: Any) -> FakeMCP:
    mcp = FakeMCP()
    module.register(mcp, client, conv)
    return mcp


def _build_with_conv(monkeypatch, conv):
    import gpt2agent.backend as backend_mod
    import gpt2agent.sse as sse_mod
    from gpt2agent.server import build_server

    monkeypatch.setattr(backend_mod, "BackendClient", lambda *a, **k: object())
    monkeypatch.setattr(sse_mod, "ConversationClient", lambda *a, **k: conv)
    mcp = build_server({"server": {"host": "127.0.0.1", "port": 9000},
                        "models": {"chat": "gpt-5-3", "agent": "agent-mode"}})
    return mcp._tool_manager._tools


@pytest.fixture(autouse=True)
def _no_backend_network(monkeypatch):
    """Class-level guard: any BackendClient HTTP call fails the test."""
    from gpt2agent.backend import BackendClient

    def _boom(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")

    monkeypatch.setattr(BackendClient, "get", _boom)
    monkeypatch.setattr(BackendClient, "post", _boom)


def _all_tool_fns(monkeypatch) -> dict[str, Any]:
    """All 9 conversation-class tool functions, keyed by tool name."""
    tools = _build_with_conv(monkeypatch, _FailConv())
    fns = {name: tools[name].fn for name in SERVER_TOOL_NAMES}
    tf = _reg(tools_features, _FailClient(), _FailConv()).tools
    fns["code_interpreter"] = tf["code_interpreter"]
    fns["canvas_execute"] = tf["canvas_execute"]
    fns["generate_image"] = _reg(
        images, _FailClient(), _FailConv()).tools["generate_image"]
    return fns


def _assert_handoff(raw: Any, *, tool: str, prompt: str, url: str,
                    model_hint: Any, temporary_hint: bool) -> dict:
    """Shared handoff contract: JSON string, common keys, exact prompt."""
    assert isinstance(raw, str), f"{tool}: manual mode must return a JSON string"
    h = json.loads(raw)
    assert h["status"] == "manual_handoff"
    assert h["tool"] == tool
    assert h["prompt"] == prompt
    assert h["url"] == url
    assert h["model_hint"] == model_hint
    assert h["temporary_hint"] is temporary_hint
    assert isinstance(h["steps"], list) and len(h["steps"]) > 0
    assert all(isinstance(s, str) and s for s in h["steps"])
    assert h["readback"] == READBACK
    return h


# --------------------------------------------------------------------------- #
#  Signature contract: `manual: bool = False` appended last on all 9 tools
# --------------------------------------------------------------------------- #


def test_manual_param_exists_defaults_false_and_is_last(monkeypatch) -> None:
    fns = _all_tool_fns(monkeypatch)
    assert sorted(fns) == sorted([
        *SERVER_TOOL_NAMES, "code_interpreter", "canvas_execute", "generate_image"])
    for name, fn in fns.items():
        params = list(inspect.signature(fn).parameters.values())
        assert params, name
        last = params[-1]
        assert last.name == "manual", f"{name}: `manual` must be the last parameter"
        assert last.default is False, f"{name}: `manual` must default to False"


# --------------------------------------------------------------------------- #
#  server.py tools
# --------------------------------------------------------------------------- #


def test_chat_manual_handoff_defaults(monkeypatch) -> None:
    tools = _build_with_conv(monkeypatch, _FailConv())
    h = _assert_handoff(
        _run(tools["chat"].fn, "hello world", manual=True),
        tool="chat", prompt="hello world", url=CHATGPT_URL,
        model_hint="gpt-5-3", temporary_hint=True)  # cfg models.chat default


def test_chat_manual_handoff_custom_model_and_temporary(monkeypatch) -> None:
    tools = _build_with_conv(monkeypatch, _FailConv())
    h = _assert_handoff(
        _run(tools["chat"].fn, "ping", model="gpt-6-pro", temporary=False,
             manual=True),
        tool="chat", prompt="ping", url=CHATGPT_URL,
        model_hint="gpt-6-pro", temporary_hint=False)


def test_agent_manual_handoff(monkeypatch) -> None:
    tools = _build_with_conv(monkeypatch, _FailConv())
    h = _assert_handoff(
        _run(tools["agent"].fn, "browse the web", manual=True),
        tool="agent", prompt="browse the web", url=CHATGPT_URL,
        model_hint=None, temporary_hint=False)
    assert h["mode"] == "agent"


def test_deep_research_manual_auto_confirm_true(monkeypatch) -> None:
    tools = _build_with_conv(monkeypatch, _FailConv())
    h = _assert_handoff(
        _run(tools["deep_research"].fn, "quantum news", auto_confirm=True,
             manual=True),
        tool="deep_research", prompt=DR_PREFIX + "quantum news",
        url=CHATGPT_URL, model_hint=None, temporary_hint=False)
    assert h["mode"] == "deep_research"


def test_deep_research_manual_auto_confirm_false(monkeypatch) -> None:
    tools = _build_with_conv(monkeypatch, _FailConv())
    h = _assert_handoff(
        _run(tools["deep_research"].fn, "quantum news", auto_confirm=False,
             manual=True),
        tool="deep_research", prompt="quantum news",  # raw query, no prefix
        url=CHATGPT_URL, model_hint=None, temporary_hint=False)
    assert h["mode"] == "deep_research"


def test_deep_research_heavy_manual_auto_confirm_true(monkeypatch) -> None:
    tools = _build_with_conv(monkeypatch, _FailConv())
    h = _assert_handoff(
        _run(tools["deep_research_heavy"].fn, "literature review",
             auto_confirm=True, manual=True),
        tool="deep_research_heavy", prompt=DR_PREFIX + "literature review",
        url=CHATGPT_URL, model_hint=None, temporary_hint=False)
    assert h["mode"] == "deep_research"


def test_deep_research_heavy_manual_auto_confirm_false(monkeypatch) -> None:
    tools = _build_with_conv(monkeypatch, _FailConv())
    h = _assert_handoff(
        _run(tools["deep_research_heavy"].fn, "literature review",
             auto_confirm=False, manual=True),
        tool="deep_research_heavy", prompt="literature review",
        url=CHATGPT_URL, model_hint=None, temporary_hint=False)
    assert h["mode"] == "deep_research"


def test_gpt_chat_manual_handoff_url_slug(monkeypatch) -> None:
    tools = _build_with_conv(monkeypatch, _FailConv())
    # "g/"-prefixed gizmo: slug strips the prefix for the URL.
    h = _assert_handoff(
        _run(tools["gpt_chat"].fn, "g/abc123", "hi gpt", manual=True),
        tool="gpt_chat", prompt="hi gpt",
        url="https://chatgpt.com/g/abc123",
        model_hint=None, temporary_hint=False)
    assert h["gizmo_id"] == "g/abc123"
    # Bare gizmo id lands at the same URL; the arg is echoed verbatim.
    h2 = _assert_handoff(
        _run(tools["gpt_chat"].fn, "abc123", "hi gpt", manual=True),
        tool="gpt_chat", prompt="hi gpt",
        url="https://chatgpt.com/g/abc123",
        model_hint=None, temporary_hint=False)
    assert h2["gizmo_id"] == "abc123"


def test_memory_create_via_chat_manual_handoff(monkeypatch) -> None:
    tools = _build_with_conv(monkeypatch, _FailConv())
    _assert_handoff(
        _run(tools["memory_create_via_chat"].fn, "SECRET-XYZ", manual=True),
        tool="memory_create_via_chat",
        prompt=MEMORY_PREFIX + "SECRET-XYZ",
        url=CHATGPT_URL, model_hint=None, temporary_hint=False)


# --------------------------------------------------------------------------- #
#  tools_features.py + images.py (module-registered, conv-backed)
# --------------------------------------------------------------------------- #


def test_code_interpreter_manual_handoff() -> None:
    fn = _reg(tools_features, _FailClient(), _FailConv()).tools["code_interpreter"]
    _assert_handoff(
        _run(fn, "print(1)", manual=True),
        tool="code_interpreter", prompt="print(1)", url=CHATGPT_URL,
        model_hint=None, temporary_hint=False)


def test_canvas_execute_manual_handoff_prefixes_prompt() -> None:
    fn = _reg(tools_features, _FailClient(), _FailConv()).tools["canvas_execute"]
    h = _assert_handoff(
        _run(fn, "make a chart", model="o3-pro", manual=True),
        tool="canvas_execute", prompt="Use Canvas to: make a chart",
        url=CHATGPT_URL, model_hint="o3-pro", temporary_hint=False)
    assert h["mode"] == "canvas"


def test_generate_image_manual_handoff() -> None:
    fn = _reg(images, _FailClient(), _FailConv()).tools["generate_image"]
    h = _assert_handoff(
        _run(fn, "a cat", manual=True),
        tool="generate_image", prompt="a cat", url=CHATGPT_URL,
        model_hint=None, temporary_hint=False)
    assert h["mode"] == "images"
