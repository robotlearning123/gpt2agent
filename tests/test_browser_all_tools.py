"""Contract tests for Phase 1.5 — browser transport on ALL conversation tools.

Spec: docs/dev/specs/phase15-browser-all-tools.md. Extends the Phase-1
browser transport (chat-only) with:

* ``BrowserTransport.chat(prompt, model=None, temporary=True, mode=None,
  url=None, effort=None)`` — ``mode`` selects a composer mode via
  SEL_MODES_BUTTON + a per-mode option constant (fail-closed
  BrowserDriftError); ``url`` overrides the chatgpt.com root; ``effort``
  is a BEST-EFFORT reasoning-effort pick through the model picker
  (missing picker/option → WARNING on logger "gpt2agent.browser", never
  raises).
* ``browser: bool = False`` on the remaining 8 conversation-class tools
  (agent, deep_research, deep_research_heavy, gpt_chat,
  memory_create_via_chat, code_interpreter, canvas_execute,
  generate_image). Precedence: manual > browser > REST/SSE.
* ``gpt2agent.tools.manual.gpt_chat_url`` — g/ slug normalization helper.
* ``register(mcp, client, conv=None, cfg=None)`` /
  ``register_all(mcp, client, conv=None, cfg=None)`` cfg plumbing.

Fake-Playwright doubles and the sys.modules stub pattern are reused from
tests/test_browser_transport.py (tests/ is a package). All features under
test do not exist yet — this file is expected to fail until the Phase-1.5
implementation lands.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from gpt2agent.tools import images, tools_features
from tests.test_browser_transport import (
    _FakePage,
    _full_spec as _base_spec,
    _install_fake_playwright,
)

_SEL_BLOCK_START = "# === selectors — the ONLY place selectors live ==="
_SEL_BLOCK_END = "# === end selectors ==="
_NEW_SEL_NAMES = (
    "SEL_MODES_BUTTON",
    "SEL_MODE_AGENT",
    "SEL_MODE_DEEP_RESEARCH",
    "SEL_MODE_IMAGES",
)
_MODE_TO_SEL = {
    "agent": "SEL_MODE_AGENT",
    "research": "SEL_MODE_DEEP_RESEARCH",
    "images": "SEL_MODE_IMAGES",
}
_INSTALL_HINT = 'pip install "gpt2agent[browser]"'
_BROWSER_LOGGER = "gpt2agent.browser"

# Byte-exact prompt wrappers — the browser path must send the SAME compiled
# prompt the REST/SSE path would (hardcoded as the strongest lock).
DR_PREFIX = (
    "Begin the deep research immediately without asking for confirmation. "
    "Do not ask clarifying questions; proceed with the best interpretation. "
)
MEMORY_PREFIX = (
    "Please commit the following to memory verbatim. "
    "Do not summarize, paraphrase, or ask for confirmation:\n\n"
)
CANVAS_PREFIX = "Use Canvas to: "

_SKIP_LIVE = os.environ.get("SKIP_LIVE", "1") == "1"


class FakePWTimeout(Exception):
    """Playwright >=1.55's TimeoutError is NOT the builtin — it subclasses
    playwright Error -> Exception. Fakes raise THIS so fail-closed paths are
    proven against the real exception hierarchy (grok finding 2026-09-16)."""


def _browser_mod():
    return importlib.import_module("gpt2agent.browser")


def _spec15(b) -> dict[str, dict[str, Any]]:
    """Phase-1 full spec plus the four new mode selectors all present."""
    spec = _base_spec(b)
    spec[b.SEL_MODES_BUTTON] = {"count": 1}
    spec[b.SEL_MODE_AGENT] = {"count": 1}
    spec[b.SEL_MODE_DEEP_RESEARCH] = {"count": 1}
    spec[b.SEL_MODE_IMAGES] = {"count": 1}
    return spec


# --------------------------------------------------------------------------- #
#  Fakes: conv + MCP + stub gpt2agent.browser
# --------------------------------------------------------------------------- #


class _Conv:
    """Records every REST/SSE-path call; returns canned replies."""

    def __init__(self) -> None:
        self.complete_calls: list[dict[str, Any]] = []
        self.dr_calls: list[dict[str, Any]] = []
        self.dr_heavy_calls: list[dict[str, Any]] = []
        self.tool_call_calls: list[dict[str, Any]] = []
        self.image_gen_calls: list[dict[str, Any]] = []

    def touched(self) -> int:
        return (
            len(self.complete_calls)
            + len(self.dr_calls)
            + len(self.dr_heavy_calls)
            + len(self.tool_call_calls)
            + len(self.image_gen_calls)
        )

    async def complete(self, model, messages, *, temporary=True, gizmo_id=None,
                       poll_async=False):
        self.complete_calls.append(
            {"model": model, "messages": messages, "temporary": temporary,
             "gizmo_id": gizmo_id, "poll_async": poll_async})
        return "REST REPLY"

    async def deep_research(self, query, **kwargs):
        self.dr_calls.append({"query": query, **kwargs})
        yield {"type": "done", "text": "DR REPORT", "content_references": []}

    async def deep_research_heavy(self, query, *, model=None, **kwargs):
        self.dr_heavy_calls.append({"query": query, "model": model})
        yield {"type": "done", "text": "HEAVY DR REPORT",
               "content_references": []}

    async def tool_call(self, prompt, *, model="gpt-5-6", temporary=False,
                        **kwargs):
        self.tool_call_calls.append(
            {"prompt": prompt, "model": model, "temporary": temporary})
        return {"conversation_id": "c1", "text": "TOOL RESULT",
                "tool_calls": [], "tool_responses": []}

    async def image_gen(self, prompt, *, model="gpt-5-6", **kwargs):
        self.image_gen_calls.append({"prompt": prompt, "model": model})
        return {"conversation_id": "c1", "text": "IMG RESULT", "assets": []}


class _FailClient:
    """Any REST call is a failure while exercising the browser path."""

    def get(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")

    def post(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("network touched")


class FakeMCP:
    """Captures @mcp.tool()-decorated functions by name."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a: Any, **k: Any):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _build_tools(monkeypatch, conv, browser_cfg=None, *, has_browser=True,
                 models=None):
    import gpt2agent.backend as backend_mod
    import gpt2agent.sse as sse_mod
    from gpt2agent.server import build_server

    monkeypatch.setattr(backend_mod, "BackendClient", lambda *a, **k: object())
    monkeypatch.setattr(sse_mod, "ConversationClient", lambda *a, **k: conv)
    cfg: dict[str, Any] = {
        "server": {"host": "127.0.0.1", "port": 9000},
        "models": models if models is not None
        else {"chat": "gpt-5-3", "agent": "agent-mode"},
    }
    if has_browser:
        cfg["browser"] = browser_cfg if browser_cfg is not None else {}
    return build_server(cfg)._tool_manager._tools


def _stub_browser_module(monkeypatch, reply: str = "STUB REPLY") -> dict[str, Any]:
    """Inject a stub gpt2agent.browser whose BrowserTransport.chat accepts and
    records the full Phase-1.5 kwarg surface (mode/url/effort + anything else)."""
    mod = types.ModuleType("gpt2agent.browser")
    made: dict[str, Any] = {"instances": []}

    class BrowserTransport:
        def __init__(self, profile_dir=None, headed=True, timeout_s=180.0):
            self.ctor = {"profile_dir": profile_dir, "headed": headed,
                         "timeout_s": timeout_s}
            self.calls: list[dict[str, Any]] = []
            made["instances"].append(self)

        async def chat(self, prompt, model=None, temporary=True, mode=None,
                       url=None, effort=None, **extra):
            self.calls.append({"prompt": prompt, "model": model,
                               "temporary": temporary, "mode": mode,
                               "url": url, "effort": effort, "extra": extra})
            return reply

    mod.BrowserTransport = BrowserTransport
    monkeypatch.setitem(sys.modules, "gpt2agent.browser", mod)
    return made


# (tool name, positional args, extra kwargs, expected transport.chat record)
_BROWSER_TOOL_CASES = [
    pytest.param(
        "agent", ("do stuff",), {},
        {"prompt": "do stuff", "model": None, "temporary": False,
         "mode": "agent", "url": None, "effort": None, "extra": {}},
        id="agent"),
    pytest.param(
        "deep_research", ("dr topic",), {},
        {"prompt": DR_PREFIX + "dr topic", "model": None, "temporary": False,
         "mode": "research", "url": None, "effort": None, "extra": {}},
        id="deep_research"),
    pytest.param(
        "deep_research_heavy", ("dr topic",), {},
        {"prompt": DR_PREFIX + "dr topic", "model": None, "temporary": False,
         "mode": "research", "url": None, "effort": "Pro", "extra": {}},
        id="deep_research_heavy"),
    pytest.param(
        "gpt_chat", ("g/abc", "hi gpt"), {},
        {"prompt": "hi gpt", "model": None, "temporary": False,
         "mode": None, "url": "https://chatgpt.com/g/abc", "effort": None,
         "extra": {}},
        id="gpt_chat"),
    pytest.param(
        "memory_create_via_chat", ("remember XYZ",), {},
        {"prompt": MEMORY_PREFIX + "remember XYZ", "model": None,
         "temporary": False, "mode": None, "url": None, "effort": None,
         "extra": {}},
        id="memory_create_via_chat"),
    pytest.param(
        "code_interpreter", ("run code",), {"model": "gpt-x"},
        {"prompt": "run code", "model": "gpt-x", "temporary": False,
         "mode": None, "url": None, "effort": None, "extra": {}},
        id="code_interpreter"),
    pytest.param(
        "canvas_execute", ("make a chart",), {"model": "gpt-x"},
        {"prompt": CANVAS_PREFIX + "make a chart", "model": "gpt-x",
         "temporary": False, "mode": None, "url": None, "effort": None,
         "extra": {}},
        id="canvas_execute"),
    pytest.param(
        "generate_image", ("a cat",), {"model": "gpt-x"},
        {"prompt": "a cat", "model": "gpt-x", "temporary": False,
         "mode": "images", "url": None, "effort": None, "extra": {}},
        id="generate_image"),
]


# --------------------------------------------------------------------------- #
#  1. Transport — mode selection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["agent", "research", "images"])
def test_mode_selection_happy_path(monkeypatch, mode) -> None:
    """mode=X → SEL_MODES_BUTTON click, then the mode's option click, then the
    prompt is typed verbatim and the assistant reply returned."""
    b = _browser_mod()
    page = _FakePage(_spec15(b))
    _install_fake_playwright(monkeypatch, page)

    out = asyncio.run(
        b.BrowserTransport().chat("hi", temporary=False, mode=mode))

    assert out == "ASSISTANT REPLY"
    opt = getattr(b, _MODE_TO_SEL[mode])
    assert b.SEL_MODES_BUTTON in page.clicks
    assert opt in page.clicks
    assert page.clicks.index(b.SEL_MODES_BUTTON) < page.clicks.index(opt), (
        "the tools menu must be opened before the mode option is clicked")
    assert (b.SEL_PROMPT, "hi") in page.sequenced


def test_mode_none_never_opens_modes_menu(monkeypatch) -> None:
    """Default plain chat must not touch the composer tools menu."""
    b = _browser_mod()
    page = _FakePage(_spec15(b))
    _install_fake_playwright(monkeypatch, page)

    out = asyncio.run(b.BrowserTransport().chat("hi", temporary=False))

    assert out == "ASSISTANT REPLY"
    assert b.SEL_MODES_BUTTON not in page.clicks
    assert b.SEL_MODE_AGENT not in page.clicks
    assert b.SEL_MODE_DEEP_RESEARCH not in page.clicks
    assert b.SEL_MODE_IMAGES not in page.clicks


@pytest.mark.parametrize("mode", ["agent", "research", "images"])
def test_modes_button_miss_fails_closed(monkeypatch, mode) -> None:
    b = _browser_mod()
    spec = _spec15(b)
    spec[b.SEL_MODES_BUTTON] = {"count": 0}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)

    with pytest.raises(b.BrowserDriftError) as ei:
        asyncio.run(b.BrowserTransport().chat("hi", temporary=False, mode=mode))
    assert "SEL_MODES_BUTTON" in str(ei.value)


@pytest.mark.parametrize("mode", ["agent", "research", "images"])
def test_mode_option_miss_fails_closed(monkeypatch, mode) -> None:
    b = _browser_mod()
    spec = _spec15(b)
    spec[getattr(b, _MODE_TO_SEL[mode])] = {"count": 0}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)

    with pytest.raises(b.BrowserDriftError) as ei:
        asyncio.run(b.BrowserTransport().chat("hi", temporary=False, mode=mode))
    assert _MODE_TO_SEL[mode] in str(ei.value)


def test_unknown_mode_raises_value_error(monkeypatch) -> None:
    b = _browser_mod()
    page = _FakePage(_spec15(b))
    _install_fake_playwright(monkeypatch, page)

    with pytest.raises(ValueError):
        asyncio.run(
            b.BrowserTransport().chat("hi", temporary=False, mode="bogus"))


# --------------------------------------------------------------------------- #
#  2. Transport — effort (best-effort), url override, combined
# --------------------------------------------------------------------------- #


def test_effort_picker_absent_warns_and_proceeds(monkeypatch, caplog) -> None:
    """effort set but SEL_MODEL_CHIP missing → WARNING, no exception."""
    b = _browser_mod()
    spec = _spec15(b)
    spec[b.SEL_MODEL_CHIP] = {"count": 0}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=_BROWSER_LOGGER):
        out = asyncio.run(
            b.BrowserTransport().chat("hi", temporary=False, effort="Pro"))

    assert out == "ASSISTANT REPLY"
    assert any(r.name == _BROWSER_LOGGER and r.levelno >= logging.WARNING
               for r in caplog.records), (
        "a missing effort picker must log a WARNING on gpt2agent.browser")


def test_effort_option_miss_warns_and_proceeds(monkeypatch, caplog) -> None:
    """effort set, picker present, no matching option → WARNING, no error —
    unlike the model picker, the effort pick is best-effort on a miss."""
    b = _browser_mod()
    spec = _spec15(b)
    spec[b.SEL_MODEL_CHIP] = {"count": 1}
    spec[b.SEL_MODEL_OPTION] = {"count": 1, "match": "__never__"}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=_BROWSER_LOGGER):
        out = asyncio.run(
            b.BrowserTransport().chat("hi", temporary=False, effort="Pro"))

    assert out == "ASSISTANT REPLY"
    assert b.SEL_MODEL_CHIP in page.clicks  # picker was attempted
    filters = [f for loc in page.locators.get(b.SEL_MODEL_OPTION, [])
               for f in loc.filter_has_text]
    assert "Pro" in filters
    assert any(r.name == _BROWSER_LOGGER and r.levelno >= logging.WARNING
               for r in caplog.records)


def test_url_overrides_chatgpt_root(monkeypatch) -> None:
    b = _browser_mod()
    page = _FakePage(_spec15(b))
    _install_fake_playwright(monkeypatch, page)

    out = asyncio.run(b.BrowserTransport().chat(
        "hi", temporary=False, url="https://chatgpt.com/g/abc"))

    assert out == "ASSISTANT REPLY"
    assert page.goto_calls == ["https://chatgpt.com/g/abc"]


def test_mode_and_effort_combined(monkeypatch) -> None:
    """mode='research' + effort='Pro' → menu open + research option click +
    a model-picker effort attempt, all in one chat call."""
    b = _browser_mod()
    spec = _spec15(b)
    spec[b.SEL_MODEL_CHIP] = {"count": 1}
    spec[b.SEL_MODEL_OPTION] = {"count": 1, "match": "Pro"}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)

    out = asyncio.run(b.BrowserTransport().chat(
        "hi", temporary=False, mode="research", effort="Pro"))

    assert out == "ASSISTANT REPLY"
    assert b.SEL_MODES_BUTTON in page.clicks
    assert b.SEL_MODE_DEEP_RESEARCH in page.clicks
    assert b.SEL_MODEL_CHIP in page.clicks
    assert b.SEL_MODEL_OPTION in page.clicks
    filters = [f for loc in page.locators.get(b.SEL_MODEL_OPTION, [])
               for f in loc.filter_has_text]
    assert "Pro" in filters


# --------------------------------------------------------------------------- #
#  3. gpt_chat_url helper
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gizmo", ["g/abc", "abc"])
def test_gpt_chat_url_normalizes_slug(gizmo) -> None:
    from gpt2agent.tools.manual import gpt_chat_url

    assert gpt_chat_url(gizmo) == "https://chatgpt.com/g/abc"


# --------------------------------------------------------------------------- #
#  4. Tool wiring — browser=True on all 8 conversation tools
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,args,kwargs,expected", [p.values for p in _BROWSER_TOOL_CASES],
    ids=[p.id for p in _BROWSER_TOOL_CASES])
def test_browser_path_invokes_transport(monkeypatch, tmp_path, name, args,
                                        kwargs, expected) -> None:
    """browser=True + [browser] enabled → transport.chat called once with the
    pinned kwargs; ctor mirrors chat's existing branch; REST conv untouched."""
    conv = _Conv()
    made = _stub_browser_module(monkeypatch, reply="BROWSER REPLY")
    prof = tmp_path / "prof"
    tools = _build_tools(monkeypatch, conv, {
        "enabled": True, "headed": False, "timeout_s": 42,
        "profile_dir": str(prof)})

    out = asyncio.run(tools[name].fn(*args, browser=True, **kwargs))

    assert out == "BROWSER REPLY"
    assert len(made["instances"]) == 1
    inst = made["instances"][0]
    assert inst.ctor["headed"] is False
    assert inst.ctor["timeout_s"] == 42
    assert str(inst.ctor["profile_dir"]) == str(prof)
    assert inst.calls == [expected]
    assert conv.touched() == 0, f"{name}: browser path must bypass REST/SSE"


@pytest.mark.parametrize("gizmo", ["g/abc", "abc"])
def test_gpt_chat_browser_url_slug_forms(monkeypatch, gizmo) -> None:
    """Both gizmo_id spellings navigate to the same /g/ URL."""
    conv = _Conv()
    made = _stub_browser_module(monkeypatch)
    tools = _build_tools(monkeypatch, conv, {"enabled": True})

    asyncio.run(tools["gpt_chat"].fn(gizmo, "hi gpt", browser=True))

    assert len(made["instances"]) == 1
    assert made["instances"][0].calls[0]["url"] == (
        "https://chatgpt.com/g/abc")


def test_deep_research_heavy_effort_is_label_not_slug(monkeypatch) -> None:
    """effort is the picker LABEL 'Pro' — the [models].heavy_dr slug is for
    the REST model, not the browser effort picker (grok finding: passing the
    slug was a silent best-effort no-op)."""
    conv = _Conv()
    made = _stub_browser_module(monkeypatch)
    tools = _build_tools(
        monkeypatch, conv, {"enabled": True},
        models={"chat": "gpt-5-3", "agent": "agent-mode",
                "heavy_dr": "gpt-6-pro"})

    asyncio.run(tools["deep_research_heavy"].fn("q", browser=True))

    call = made["instances"][0].calls[0]
    assert call["mode"] == "research"
    assert call["effort"] == "Pro"


@pytest.mark.parametrize(
    "name,prompt_start", [
        ("deep_research", "Begin the deep research"),
        ("deep_research_heavy", "Begin the deep research"),
        ("canvas_execute", CANVAS_PREFIX),
        ("memory_create_via_chat",
         "Please commit the following to memory verbatim"),
    ])
def test_compiled_prompt_identity_through_browser(monkeypatch, name,
                                                  prompt_start) -> None:
    """The browser path must send the SAME compiled prompt the REST path
    would — the imperative/prefix wrappers are reused, never re-typed."""
    conv = _Conv()
    made = _stub_browser_module(monkeypatch)
    tools = _build_tools(monkeypatch, conv, {"enabled": True})
    kwargs = {"model": "gpt-x"} if name == "canvas_execute" else {}

    asyncio.run(tools[name].fn("payload", browser=True, **kwargs))

    assert made["instances"][0].calls[0]["prompt"].startswith(prompt_start)


@pytest.mark.parametrize(
    "name,args,kwargs,expected", [p.values for p in _BROWSER_TOOL_CASES],
    ids=[p.id for p in _BROWSER_TOOL_CASES])
def test_manual_wins_over_browser(monkeypatch, name, args, kwargs,
                                  expected) -> None:
    """manual=True beats browser=True on all 8 — handoff JSON, transport and
    REST conv both untouched."""
    conv = _Conv()
    made = _stub_browser_module(monkeypatch)
    tools = _build_tools(monkeypatch, conv, {"enabled": True})

    raw = asyncio.run(
        tools[name].fn(*args, manual=True, browser=True, **kwargs))

    handoff = json.loads(raw)
    assert handoff["status"] == "manual_handoff"
    assert made["instances"] == []
    assert conv.touched() == 0


@pytest.mark.parametrize("section", ["disabled", "absent"])
@pytest.mark.parametrize(
    "name,args,kwargs,expected", [p.values for p in _BROWSER_TOOL_CASES],
    ids=[p.id for p in _BROWSER_TOOL_CASES])
def test_browser_not_enabled_raises(monkeypatch, name, args, kwargs,
                                    expected, section) -> None:
    """browser=True without [browser] enabled → RuntimeError naming the tool,
    the [browser] enabled flag, and the optional-extra install hint."""
    conv = _Conv()
    made = _stub_browser_module(monkeypatch)
    tools = _build_tools(
        monkeypatch, conv,
        {"enabled": False} if section == "disabled" else None,
        has_browser=(section == "disabled"))

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(tools[name].fn(*args, browser=True, **kwargs))
    msg = str(ei.value)
    assert f"{name}(browser=True)" in msg
    assert "[browser] enabled" in msg
    assert "gpt2agent[browser]" in msg
    assert made["instances"] == []
    assert conv.touched() == 0


@pytest.mark.parametrize(
    "name,args,kwargs,expected", [p.values for p in _BROWSER_TOOL_CASES],
    ids=[p.id for p in _BROWSER_TOOL_CASES])
def test_browser_default_false_uses_rest_path(monkeypatch, name, args, kwargs,
                                              expected) -> None:
    """Default (no browser kwarg) → the existing REST/SSE path runs and the
    transport is never constructed."""
    conv = _Conv()
    made = _stub_browser_module(monkeypatch)
    tools = _build_tools(monkeypatch, conv, {"enabled": True})

    asyncio.run(tools[name].fn(*args, **kwargs))

    assert made["instances"] == []
    assert conv.touched() > 0, f"{name}: default path must use REST/SSE"


# --------------------------------------------------------------------------- #
#  5. cfg plumbing — register(mcp, client, conv=None, cfg=None)
# --------------------------------------------------------------------------- #


def test_register_all_signature_has_cfg() -> None:
    from gpt2agent.tools import register_all

    params = inspect.signature(register_all).parameters
    assert "cfg" in params, "register_all must accept cfg=None"
    assert params["cfg"].default is None


@pytest.mark.parametrize("module", [tools_features, images],
                         ids=["tools_features", "images"])
def test_module_register_signature_has_cfg(module) -> None:
    params = inspect.signature(module.register).parameters
    assert "cfg" in params, f"{module.__name__}.register must accept cfg=None"
    assert params["cfg"].default is None


@pytest.mark.parametrize(
    "module,tool,args,kwargs,expected", [
        pytest.param(
            tools_features, "code_interpreter", ("run code",),
            {"model": "gpt-x"},
            {"prompt": "run code", "model": "gpt-x", "temporary": False,
             "mode": None, "url": None, "effort": None, "extra": {}},
            id="code_interpreter"),
        pytest.param(
            tools_features, "canvas_execute", ("make a chart",),
            {"model": "gpt-x"},
            {"prompt": CANVAS_PREFIX + "make a chart", "model": "gpt-x",
             "temporary": False, "mode": None, "url": None, "effort": None,
             "extra": {}},
            id="canvas_execute"),
        pytest.param(
            images, "generate_image", ("a cat",), {"model": "gpt-x"},
            {"prompt": "a cat", "model": "gpt-x", "temporary": False,
             "mode": "images", "url": None, "effort": None, "extra": {}},
            id="generate_image"),
    ])
def test_module_register_cfg_drives_browser(monkeypatch, module, tool, args,
                                            kwargs, expected) -> None:
    """module.register(mcp, client, conv, cfg={...}) — the new cfg plumbing —
    must give the tool its [browser] section without build_server."""
    conv = _Conv()
    made = _stub_browser_module(monkeypatch, reply="BROWSER REPLY")
    mcp = FakeMCP()
    module.register(mcp, _FailClient(), conv,
                    cfg={"browser": {"enabled": True}})

    out = asyncio.run(mcp.tools[tool](*args, browser=True, **kwargs))

    assert out == "BROWSER REPLY"
    assert len(made["instances"]) == 1
    assert made["instances"][0].calls == [expected]
    assert conv.touched() == 0


# --------------------------------------------------------------------------- #
#  6. Lazy-import proofs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "modname", ["gpt2agent.server", "gpt2agent.tools.tools_features",
                "gpt2agent.tools.images"])
def test_no_top_level_browser_import(modname) -> None:
    """BrowserTransport must be imported lazily INSIDE the browser branch —
    a top-level 'from gpt2agent.browser import ...' would make the optional
    extra mandatory."""
    src = inspect.getsource(importlib.import_module(modname))
    for i, line in enumerate(src.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith(("from gpt2agent.browser",
                                "import gpt2agent.browser")):
            assert line != stripped, (
                f"{modname} line {i + 1}: top-level import of "
                f"gpt2agent.browser — must stay lazy inside the function: "
                f"{line}")


@pytest.mark.parametrize(
    "name,args,kwargs,expected", [p.values for p in _BROWSER_TOOL_CASES],
    ids=[p.id for p in _BROWSER_TOOL_CASES])
def test_missing_playwright_raises_install_hint(monkeypatch, name, args,
                                                kwargs, expected) -> None:
    """browser=True + enabled cfg + playwright absent → the real
    gpt2agent.browser loads (NOT stubbed) and transport.chat surfaces the
    lazy-import install hint from _load_playwright."""
    conv = _Conv()
    monkeypatch.delitem(sys.modules, "gpt2agent.browser", raising=False)
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    tools = _build_tools(monkeypatch, conv, {"enabled": True})

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(tools[name].fn(*args, browser=True, **kwargs))
    assert _INSTALL_HINT in str(ei.value)


# --------------------------------------------------------------------------- #
#  7. Selector-block invariant — the four new names
# --------------------------------------------------------------------------- #


def test_new_mode_selectors_live_in_constants_block() -> None:
    b = _browser_mod()
    src = Path(inspect.getfile(b)).read_text(encoding="utf-8")
    lines = src.splitlines()
    starts = [i for i, line in enumerate(lines)
              if line.strip() == _SEL_BLOCK_START]
    ends = [i for i, line in enumerate(lines)
            if line.strip() == _SEL_BLOCK_END]
    assert len(starts) == 1, "exactly one selector-block start marker required"
    assert len(ends) == 1, "exactly one selector-block end marker required"
    start, end = starts[0], ends[0]
    assert start < end, "end marker must follow start marker"
    block = "\n".join(lines[start:end + 1])
    for name in _NEW_SEL_NAMES:
        assert name in block, f"{name} must be defined in the selectors block"


# --------------------------------------------------------------------------- #
#  8. Live smokes — real Chrome, real account (SKIP_LIVE-gated)
# --------------------------------------------------------------------------- #


def _live_server(monkeypatch):
    import gpt2agent.backend as backend_mod
    import gpt2agent.sse as sse_mod
    from gpt2agent.server import build_server

    monkeypatch.setattr(backend_mod, "BackendClient", lambda *a, **k: object())
    monkeypatch.setattr(sse_mod, "ConversationClient", lambda *a, **k: object())
    return build_server({
        "server": {"host": "127.0.0.1", "port": 9000},
        "models": {"chat": "gpt-5-3", "agent": "agent-mode"},
        "browser": {"enabled": True, "headed": True, "timeout_s": 300},
    })


@pytest.mark.skipif(_SKIP_LIVE, reason="SKIP_LIVE=1 — set SKIP_LIVE=0 to run live")
def test_browser_agent_live_smoke(monkeypatch) -> None:
    mcp = _live_server(monkeypatch)
    out = asyncio.run(mcp._tool_manager._tools["agent"].fn(
        "Reply with exactly: BROWSER AGENT OK", browser=True))
    assert "BROWSER AGENT OK" in out


@pytest.mark.skipif(_SKIP_LIVE, reason="SKIP_LIVE=1 — set SKIP_LIVE=0 to run live")
def test_browser_deep_research_live_smoke(monkeypatch) -> None:
    mcp = _live_server(monkeypatch)
    out = asyncio.run(mcp._tool_manager._tools["deep_research"].fn(
        "In one sentence, what is MCP (Model Context Protocol)?",
        browser=True))
    assert isinstance(out, str) and out
