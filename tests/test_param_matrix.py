"""Offline parameter-contract matrix for the 9 conversation-class tools.

Locks in code what the live matrix verifies manually. For each of
chat/agent/deep_research/deep_research_heavy/gpt_chat/memory_create_via_chat/
generate_image/code_interpreter/canvas_execute:

* ``manual=True`` returns a JSON handoff whose ``prompt`` is byte-identical
  to the wrapper text the REST/SSE path compiles for the same input —
  verified by capturing the argument the tool actually passes to the conv
  layer (a recording fake), plus the shared wrapper constants the codebase
  defines for that purpose.
* ``model``/``temporary``/``auto_confirm`` propagate correctly.
* Zero network: ``BackendClient.get``/``post`` are monkeypatched to raise
  and the recording conv's call list must stay empty on the manual path.

Plus unit contracts for ``citations.apply_inline_citations`` and the
``ConversationClient._bridge_headers`` sentinel-bridge gating.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

from gpt2agent.citations import apply_inline_citations
from gpt2agent.server import _MEMORY_PROMPT_PREFIX, build_server
from gpt2agent.sse import ConversationClient
from gpt2agent.tools import images, tools_features
from gpt2agent.tools.manual import CHATGPT_URL, build_handoff, gpt_chat_url
from gpt2agent.tools.tools_features import CANVAS_PROMPT_PREFIX

# _DR_IMPERATIVE_PREFIX is a local inside build_server() (not importable), so
# it is duplicated here as an explicit lock — the byte-identity checks below
# additionally compare the handoff prompt against the text the REST path
# actually compiles, so this constant is not the sole oracle.
DR_IMPERATIVE_PREFIX = (
    "Begin the deep research immediately without asking for confirmation. "
    "Do not ask clarifying questions; proceed with the best interpretation. "
)
READBACK = {"list": "list_conversations", "fetch": "get_conversation"}

SERVER_TOOLS = (
    "chat",
    "agent",
    "deep_research",
    "deep_research_heavy",
    "gpt_chat",
    "memory_create_via_chat",
)
MODULE_TOOLS = ("code_interpreter", "canvas_execute", "generate_image")
ALL_TOOLS = SERVER_TOOLS + MODULE_TOOLS

CFG = {
    "server": {"host": "127.0.0.1", "port": 9000},
    "models": {
        "chat": "mx-chat-model",
        "agent": "mx-agent-model",
        "heavy_dr": "mx-heavy-dr-model",
    },
}

# Minimal kwargs to invoke each tool (prompt-carrying arg included).
_CALL_KWARGS = {
    "chat": {"prompt": "mx"},
    "agent": {"prompt": "mx"},
    "deep_research": {"query": "mx"},
    "deep_research_heavy": {"query": "mx"},
    "gpt_chat": {"gizmo_id": "g/abc123", "prompt": "mx"},
    "memory_create_via_chat": {"content": "mx"},
    "code_interpreter": {"prompt": "mx"},
    "canvas_execute": {"prompt": "mx"},
    "generate_image": {"prompt": "mx"},
}


class FakeMCP:
    """Captures @mcp.tool()-decorated functions by name."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a: Any, **k: Any):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class _FailClient:
    """Any REST call fails the test — proves the path under test is offline."""

    def get(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")

    def post(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")


class _RecordingConv:
    """Records the exact arguments a tool hands to the conv (SSE) layer."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, model, messages, **kw):
        self.calls.append(
            {"method": "complete", "model": model, "messages": messages, **kw}
        )
        return "rest-reply"

    async def deep_research(self, q, **kw):
        self.calls.append({"method": "deep_research", "q": q, **kw})
        yield {"type": "done", "text": "report", "content_references": []}

    async def deep_research_heavy(self, q, **kw):
        self.calls.append({"method": "deep_research_heavy", "q": q, **kw})
        yield {"type": "done", "text": "report", "content_references": []}

    async def tool_call(self, prompt, **kw):
        self.calls.append({"method": "tool_call", "prompt": prompt, **kw})
        return {"text": "ok"}

    async def image_gen(self, prompt, **kw):
        self.calls.append({"method": "image_gen", "prompt": prompt, **kw})
        return {"assets": []}


def _run(fn, *args: Any, **kwargs: Any) -> Any:
    return asyncio.run(fn(*args, **kwargs))


def _server_tools(monkeypatch, conv: _RecordingConv) -> dict:
    """build_server() with BackendClient/ConversationClient faked out."""
    import gpt2agent.backend as backend_mod
    import gpt2agent.sse as sse_mod

    monkeypatch.setattr(backend_mod, "BackendClient", lambda *a, **k: object())
    monkeypatch.setattr(sse_mod, "ConversationClient", lambda *a, **k: conv)
    mcp = build_server(CFG)
    return mcp._tool_manager._tools


def _module_tools(module) -> tuple[dict, _RecordingConv]:
    conv = _RecordingConv()
    mcp = FakeMCP()
    module.register(mcp, _FailClient(), conv)
    return mcp.tools, conv


def _all_fns(monkeypatch) -> tuple[dict, list[_RecordingConv]]:
    """All 9 conversation-class tool fns plus their recording convs."""
    conv = _RecordingConv()
    tools = _server_tools(monkeypatch, conv)
    fns = {name: tools[name].fn for name in SERVER_TOOLS}
    tf, tf_conv = _module_tools(tools_features)
    fns["code_interpreter"] = tf["code_interpreter"]
    fns["canvas_execute"] = tf["canvas_execute"]
    img, img_conv = _module_tools(images)
    fns["generate_image"] = img["generate_image"]
    return fns, [conv, tf_conv, img_conv]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any BackendClient HTTP call fails the test — the file stays offline."""
    from gpt2agent.backend import BackendClient

    def _boom(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")

    monkeypatch.setattr(BackendClient, "get", _boom)
    monkeypatch.setattr(BackendClient, "post", _boom)


def _handoff(raw: Any) -> dict:
    assert isinstance(raw, str), "manual mode must return a JSON string"
    h = json.loads(raw)
    assert h["status"] == "manual_handoff"
    return h


def _complete_prompt(call: dict) -> str:
    """The compiled prompt text out of a recorded complete() call."""
    return call["messages"][0]["content"]


# --------------------------------------------------------------------------- #
#  Envelope + signature contract across all 9 tools
# --------------------------------------------------------------------------- #


def test_manual_is_last_param_defaulting_false_on_all_nine(monkeypatch) -> None:
    fns, _ = _all_fns(monkeypatch)
    assert sorted(fns) == sorted(ALL_TOOLS)
    for name, fn in fns.items():
        last = list(inspect.signature(fn).parameters.values())[-1]
        assert last.name == "manual", f"{name}: `manual` must be last"
        assert last.default is False, f"{name}: `manual` must default to False"


def test_all_nine_manual_handoff_envelope(monkeypatch) -> None:
    fns, convs = _all_fns(monkeypatch)
    for name, fn in fns.items():
        h = _handoff(_run(fn, **_CALL_KWARGS[name], manual=True))
        assert h["tool"] == name
        assert isinstance(h["prompt"], str) and h["prompt"]
        assert h["url"].startswith(CHATGPT_URL)
        assert "model_hint" in h and "temporary_hint" in h
        assert isinstance(h["steps"], list) and len(h["steps"]) > 0
        assert all(s.split(". ", 1)[0].isdigit() for s in h["steps"])
        if h["temporary_hint"]:
            assert "note" in h["readback"]
        else:
            assert h["readback"] == READBACK
    # manual=True never reaches the conv layer for any tool
    assert all(c.calls == [] for c in convs)


# --------------------------------------------------------------------------- #
#  server.py tools — byte-identical prompt + parameter propagation
# --------------------------------------------------------------------------- #


def test_chat_byte_identical_and_params(monkeypatch) -> None:
    conv = _RecordingConv()
    tools = _server_tools(monkeypatch, conv)

    h = _handoff(
        _run(tools["chat"].fn, "hello", model="gpt-6-pro",
             temporary=False, manual=True)
    )
    assert h["tool"] == "chat"
    assert h["url"] == CHATGPT_URL
    assert h["model_hint"] == "gpt-6-pro"
    assert h["temporary_hint"] is False
    # The tool delegates to the shared builder with the same arguments.
    assert h == build_handoff(
        "chat", "hello", model="gpt-6-pro", temporary=False)
    assert conv.calls == []

    _run(tools["chat"].fn, "hello", model="gpt-6-pro", temporary=False)
    assert len(conv.calls) == 1
    call = conv.calls[0]
    assert call["method"] == "complete"
    assert call["model"] == "gpt-6-pro"
    assert call["temporary"] is False
    assert h["prompt"] == _complete_prompt(call) == "hello"


def test_chat_defaults_propagate(monkeypatch) -> None:
    conv = _RecordingConv()
    tools = _server_tools(monkeypatch, conv)

    h = _handoff(_run(tools["chat"].fn, "hi", manual=True))
    assert h["model_hint"] == CFG["models"]["chat"]  # config default
    assert h["temporary_hint"] is True  # temporary defaults True
    assert "note" in h["readback"]  # temporary → browser-copy note

    _run(tools["chat"].fn, "hi")
    call = conv.calls[0]
    assert call["model"] == CFG["models"]["chat"]
    assert call["temporary"] is True
    assert h["prompt"] == _complete_prompt(call)


def test_agent_byte_identical_and_params(monkeypatch) -> None:
    conv = _RecordingConv()
    tools = _server_tools(monkeypatch, conv)

    h = _handoff(_run(tools["agent"].fn, "run the errand", manual=True))
    assert h["tool"] == "agent"
    assert h["mode"] == "agent"
    assert h["model_hint"] is None
    assert h["temporary_hint"] is False
    assert conv.calls == []

    _run(tools["agent"].fn, "run the errand")
    call = conv.calls[0]
    assert call["method"] == "complete"
    assert call["model"] == CFG["models"]["agent"]
    assert call["temporary"] is False
    assert call["poll_async"] is True  # agent mode runs async
    assert h["prompt"] == _complete_prompt(call) == "run the errand"


@pytest.mark.parametrize("tool", ["deep_research", "deep_research_heavy"])
@pytest.mark.parametrize("auto_confirm", [True, False])
def test_dr_byte_identical_and_auto_confirm(monkeypatch, tool, auto_confirm):
    conv = _RecordingConv()
    tools = _server_tools(monkeypatch, conv)

    h = _handoff(
        _run(tools[tool].fn, "topic", auto_confirm=auto_confirm, manual=True)
    )
    assert h["tool"] == tool
    assert h["mode"] == "deep_research"
    assert h["model_hint"] is None
    assert h["temporary_hint"] is False
    expected = (DR_IMPERATIVE_PREFIX + "topic") if auto_confirm else "topic"
    assert h["prompt"] == expected
    assert conv.calls == []

    _run(tools[tool].fn, "topic", auto_confirm=auto_confirm)
    assert len(conv.calls) == 1
    call = conv.calls[0]
    assert call["method"] == tool
    assert call["q"] == expected
    assert h["prompt"] == call["q"]  # byte-identical to the compiled query


def test_deep_research_heavy_model_propagates(monkeypatch) -> None:
    conv = _RecordingConv()
    tools = _server_tools(monkeypatch, conv)
    _run(tools["deep_research_heavy"].fn, "topic")
    assert conv.calls[0]["model"] == CFG["models"]["heavy_dr"]


def test_gpt_chat_byte_identical_and_gizmo(monkeypatch) -> None:
    conv = _RecordingConv()
    tools = _server_tools(monkeypatch, conv)

    h = _handoff(_run(tools["gpt_chat"].fn, "g/abc123", "hi gpt", manual=True))
    assert h["tool"] == "gpt_chat"
    assert h["gizmo_id"] == "g/abc123"
    assert h["url"] == gpt_chat_url("g/abc123")
    assert h["url"] == "https://chatgpt.com/g/abc123"  # slug prefix stripped
    assert h["model_hint"] is None
    assert h["temporary_hint"] is False
    assert conv.calls == []

    _run(tools["gpt_chat"].fn, "g/abc123", "hi gpt")
    call = conv.calls[0]
    assert call["method"] == "complete"
    assert call["model"] == CFG["models"]["chat"]
    assert call["gizmo_id"] == "g/abc123"
    assert call["temporary"] is False
    assert h["prompt"] == _complete_prompt(call) == "hi gpt"


def test_memory_create_via_chat_byte_identical(monkeypatch) -> None:
    conv = _RecordingConv()
    tools = _server_tools(monkeypatch, conv)

    h = _handoff(
        _run(tools["memory_create_via_chat"].fn, "fact-XYZ", manual=True))
    assert h["tool"] == "memory_create_via_chat"
    assert h["model_hint"] is None
    assert h["temporary_hint"] is False
    # Shared constant is the wrapper the REST path compiles.
    assert h["prompt"] == _MEMORY_PROMPT_PREFIX + "fact-XYZ"
    assert conv.calls == []

    _run(tools["memory_create_via_chat"].fn, "fact-XYZ")
    call = conv.calls[0]
    assert call["method"] == "complete"
    assert call["model"] == CFG["models"]["chat"]
    assert call["temporary"] is False
    assert h["prompt"] == _complete_prompt(call)


def test_shared_wrapper_constants_locked() -> None:
    """The shared wrapper constants keep their documented contents."""
    assert _MEMORY_PROMPT_PREFIX == (
        "Please commit the following to memory verbatim. "
        "Do not summarize, paraphrase, or ask for confirmation:\n\n"
    )
    assert CANVAS_PROMPT_PREFIX == "Use Canvas to: "


# --------------------------------------------------------------------------- #
#  Module-registered tools (tools_features.py / images.py)
# --------------------------------------------------------------------------- #


def test_code_interpreter_byte_identical_and_model() -> None:
    tools, conv = _module_tools(tools_features)

    h = _handoff(
        _run(tools["code_interpreter"], "print(1)", model="o3-pro",
             manual=True)
    )
    assert h["tool"] == "code_interpreter"
    # Contract: the manual handoff does not forward `model` for this tool.
    assert h["model_hint"] is None
    assert h["temporary_hint"] is False
    assert conv.calls == []

    _run(tools["code_interpreter"], "print(1)", model="o3-pro")
    call = conv.calls[0]
    assert call["method"] == "tool_call"
    assert call["model"] == "o3-pro"
    assert call["temporary"] is False
    assert h["prompt"] == call["prompt"] == "print(1)"


def test_canvas_execute_byte_identical_prefix_and_model() -> None:
    tools, conv = _module_tools(tools_features)

    h = _handoff(
        _run(tools["canvas_execute"], "make a chart", model="o3-pro",
             manual=True)
    )
    assert h["tool"] == "canvas_execute"
    assert h["mode"] == "canvas"
    assert h["model_hint"] == "o3-pro"  # canvas does forward model_hint
    assert h["temporary_hint"] is False
    assert h["prompt"] == CANVAS_PROMPT_PREFIX + "make a chart"
    assert conv.calls == []

    _run(tools["canvas_execute"], "make a chart", model="o3-pro")
    call = conv.calls[0]
    assert call["method"] == "tool_call"
    assert call["model"] == "o3-pro"
    assert call["temporary"] is False
    assert h["prompt"] == call["prompt"] == CANVAS_PROMPT_PREFIX + "make a chart"


def test_generate_image_byte_identical_and_model() -> None:
    tools, conv = _module_tools(images)

    h = _handoff(
        _run(tools["generate_image"], "a cat", model="gpt-6-pro", manual=True)
    )
    assert h["tool"] == "generate_image"
    assert h["mode"] == "images"
    assert h["model_hint"] is None
    assert h["temporary_hint"] is False
    assert conv.calls == []

    _run(tools["generate_image"], "a cat", model="gpt-6-pro")
    call = conv.calls[0]
    assert call["method"] == "image_gen"
    assert call["model"] == "gpt-6-pro"
    assert h["prompt"] == call["prompt"] == "a cat"


# --------------------------------------------------------------------------- #
#  citations.apply_inline_citations
# --------------------------------------------------------------------------- #


def _marker(token: str) -> str:
    """Real DR marker shape: private-use chars wrapping a citeturn token."""
    return f"{chr(0xE200)}{token}{chr(0xE201)}"


def _ref(marker: str, urls: list | None) -> dict:
    return {"matched_text": marker, "safe_urls": urls}


def test_citations_numbered_by_ref_order_not_position() -> None:
    m1, m2 = _marker("citeturn0search0"), _marker("citeturn0search1")
    # m2 appears first in the text but second in refs → still numbered [2].
    out = apply_inline_citations(
        f"b {m2} a {m1}", [_ref(m1, ["u1"]), _ref(m2, ["u2"])])
    assert out == "b [2](u2) a [1](u1)"


def test_citations_sequential_markers() -> None:
    m1, m2 = _marker("citeturn0search0"), _marker("citeturn0search1")
    out = apply_inline_citations(
        f"x {m1} y {m2} z", [_ref(m1, ["u1"]), _ref(m2, ["u2"])])
    assert out == "x [1](u1) y [2](u2) z"


def test_citations_multi_url_single_marker() -> None:
    m = _marker("citeturn0search0")
    out = apply_inline_citations(f"a {m} b", [_ref(m, ["u1", "u2", "u3"])])
    assert out == "a [1](u1)[2](u2)[3](u3) b"


@pytest.mark.parametrize("urls", [None, [], [""], ["", ""]])
def test_citations_empty_urls_degrade_to_bracket(urls) -> None:
    m = _marker("citeturn0search0")
    out = apply_inline_citations(f"a {m} b", [_ref(m, urls)])
    assert out == "a [1] b"


def test_citations_blank_urls_filtered_among_real() -> None:
    m = _marker("citeturn0search0")
    out = apply_inline_citations(f"a {m}", [_ref(m, ["", "u1", None])])
    assert out == "a [1](u1)"


def test_citations_repeated_marker_reuses_index() -> None:
    m = _marker("citeturn0search0")
    out = apply_inline_citations(
        f"a {m} b {m}", [_ref(m, ["u1"]), _ref(m, ["u2"])])
    assert out == "a [1](u1) b [2](u2)"  # same marker, different URLs → different numbers


def test_citations_absent_marker_skipped_without_index() -> None:
    absent, m1 = _marker("citeturn0search9"), _marker("citeturn0search0")
    out = apply_inline_citations(
        f"x {m1}", [_ref(absent, ["uA"]), _ref(m1, ["u1"])])
    assert out == "x [1](u1)"  # absent marker did not consume index 1


def test_citations_malformed_refs_skipped() -> None:
    m = _marker("citeturn0search0")
    refs = [None, {}, {"matched_text": ""}, {"safe_urls": ["uX"]}, _ref(m, ["u"])]
    assert apply_inline_citations(f"a {m}", refs) == "a [1](u)"


def test_citations_private_use_range_stripped() -> None:
    low, mid, high = chr(0xE200), chr(0xE250), chr(0xE2FF)
    out = apply_inline_citations(f"a{low}b{mid}c{high}d", [])
    assert out == "abcd"
    # Boundary: chars just outside the range are preserved.
    out = apply_inline_citations(f"a{chr(0xE1FF)}b{chr(0xE300)}c", [])
    assert out == f"a{chr(0xE1FF)}b{chr(0xE300)}c"


def test_citations_no_refs_passthrough() -> None:
    text = "plain text, no markers"
    assert apply_inline_citations(text, None) == text
    assert apply_inline_citations(text, []) == text
    # Stripping still applies when refs are absent.
    assert apply_inline_citations(f"a{chr(0xE200)}b", None) == "ab"


def test_citations_empty_text() -> None:
    m = _marker("citeturn0search0")
    assert apply_inline_citations("", [_ref(m, ["u"])]) == ""
    assert apply_inline_citations("", None) == ""


# --------------------------------------------------------------------------- #
#  sse.ConversationClient._bridge_headers gating
# --------------------------------------------------------------------------- #


def _provision_bridge_dir(root, *, enabled: bool = True, vm: bool = True):
    (root / "wrapper" / "reverse").mkdir(parents=True)
    if vm:
        (root / "wrapper" / "reverse" / "vm.py").write_text("# stub\n")
    if enabled:
        (root / "ENABLED").touch()


def test_bridge_headers_off_env_forces_none(monkeypatch, tmp_path) -> None:
    """GPT2AGENT_SENTINEL_BRIDGE_OFF wins over a fully provisioned bridge."""
    _provision_bridge_dir(tmp_path)  # ENABLED + wrapper/reverse/vm.py present
    monkeypatch.setenv("GPT2AGENT_SENTINEL_BRIDGE", str(tmp_path))
    monkeypatch.setenv("GPT2AGENT_SENTINEL_BRIDGE_OFF", "1")
    conv = ConversationClient(object())
    assert asyncio.run(conv._bridge_headers()) is None


def test_bridge_headers_no_enabled_marker_forces_none(
        monkeypatch, tmp_path) -> None:
    """Neither env opt-in nor ENABLED marker → legacy gate path (None)."""
    import gpt2agent.sentinel_bridge as sb

    _provision_bridge_dir(tmp_path, enabled=False)  # vm.py but no ENABLED
    monkeypatch.delenv("GPT2AGENT_SENTINEL_BRIDGE_OFF", raising=False)
    monkeypatch.delenv("GPT2AGENT_SENTINEL_BRIDGE", raising=False)
    monkeypatch.setattr(sb, "_bridge_dir", lambda: tmp_path)
    conv = ConversationClient(object())
    assert asyncio.run(conv._bridge_headers()) is None


def test_bridge_headers_enabled_but_no_vm_forces_none(
        monkeypatch, tmp_path) -> None:
    """ENABLED marker present but wrapper/reverse/vm.py missing → None."""
    import gpt2agent.sentinel_bridge as sb

    _provision_bridge_dir(tmp_path, vm=False)  # ENABLED but no vm.py
    monkeypatch.delenv("GPT2AGENT_SENTINEL_BRIDGE_OFF", raising=False)
    monkeypatch.delenv("GPT2AGENT_SENTINEL_BRIDGE", raising=False)
    monkeypatch.setattr(sb, "_bridge_dir", lambda: tmp_path)
    conv = ConversationClient(object())
    assert asyncio.run(conv._bridge_headers()) is None


def test_bridge_headers_enabled_marker_mints(monkeypatch, tmp_path) -> None:
    """ENABLED + vm.py opens the mint path (faked internals: zero network)."""
    import gpt2agent.backend as backend_mod
    import gpt2agent.sentinel_bridge as sb

    _provision_bridge_dir(tmp_path)
    monkeypatch.delenv("GPT2AGENT_SENTINEL_BRIDGE_OFF", raising=False)
    monkeypatch.delenv("GPT2AGENT_SENTINEL_BRIDGE", raising=False)
    monkeypatch.setattr(sb, "_bridge_dir", lambda: tmp_path)
    monkeypatch.setattr(sb, "seed_session", lambda *a, **k: None)
    monkeypatch.setattr(sb, "new_device_id", lambda: "did-test")
    monkeypatch.setattr(backend_mod, "_load_token", lambda: "tok-test")

    minted: dict[str, Any] = {}

    class _FakeBridge:
        def mint(self, session, bearer, device_id):
            minted["bearer"] = bearer
            minted["device_id"] = device_id
            return {"openai-sentinel-turnstile-token": "ts-1"}

    monkeypatch.setattr(sb, "SentinelBridge", _FakeBridge)

    conv = ConversationClient(object())
    result = asyncio.run(conv._bridge_headers())
    assert result is not None
    headers, cookies = result
    assert headers["openai-sentinel-turnstile-token"] == "ts-1"
    assert minted == {"bearer": "tok-test", "device_id": "did-test"}
    assert conv._bridge_cookies == cookies
