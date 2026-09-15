"""Contract tests for the Phase-1 browser transport (gpt2agent/browser.py).

Spec: docs/dev/specs/phase1-browser-transport.md. Playwright is an OPTIONAL
extra (gpt2agent[browser]) — every browser interaction here is faked via
sys.modules injection of stub "playwright"/"playwright.async_api" modules;
the real playwright is never imported or launched. Server-wiring tests stub
"gpt2agent.browser" itself the same way.

Follows repo conventions: async code is driven with asyncio.run inside sync
test functions; the live smoke is SKIP_LIVE-gated like test_deep_research.py.
All gpt2agent.browser imports happen inside test functions so the file still
collects cleanly while the module does not exist yet.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest


_SEL_BLOCK_START = "# === selectors — the ONLY place selectors live ==="
_SEL_BLOCK_END = "# === end selectors ==="
_SEL_NAMES = (
    "SEL_PROMPT",
    "SEL_SEND",
    "SEL_TEMPORARY",
    "SEL_MODEL_BUTTON",
    "SEL_MODEL_OPTION",
    "SEL_STREAMING",
    "SEL_ASSISTANT",
    "SEL_LOGGED_OUT",
)
_INSTALL_HINT = 'pip install "gpt2agent[browser]"'
_CHATGPT_URL = "https://chatgpt.com/"

_SKIP_LIVE = os.environ.get("SKIP_LIVE", "1") == "1"


def _browser_mod():
    return importlib.import_module("gpt2agent.browser")


# --------------------------------------------------------------------------- #
#  Fake Playwright — async CM → chromium.launch_persistent_context → ctx → page
# --------------------------------------------------------------------------- #


class _FakeLocator:
    """Per-selector locator double. count==0 simulates a selector miss: the
    awaitables raise builtin TimeoutError (which real playwright TimeoutError
    is a subclass of) so implementations that detect misses via count() OR
    via try/except around wait_for/click both hit the fail-closed path."""

    def __init__(self, page: "_FakePage", selector: str, count: int = 0,
                 text: str = "", match: str | None = None) -> None:
        self._page = page
        self.selector = selector
        self._count = count
        self._text = text
        self._match = match
        self.click_calls = 0
        self.wait_for_calls = 0
        self.filter_has_text: list[Any] = []
        self.press_args: list[str] = []

    @property
    def first(self) -> "_FakeLocator":
        return self

    @property
    def last(self) -> "_FakeLocator":
        return self

    def filter(self, *args: Any, has_text: Any = None, **kwargs: Any) -> "_FakeLocator":
        want = has_text if has_text is not None else (args[0] if args else None)
        self.filter_has_text.append(want)
        return _FakeLocator(
            self._page, self.selector,
            count=1 if want is not None and want == self._match else 0,
            text=self._text,
        )

    async def count(self) -> int:
        return self._count

    async def wait_for(self, *args: Any, **kwargs: Any) -> None:
        self.wait_for_calls += 1
        if self._count == 0:
            raise TimeoutError(f"no element for {self.selector}")

    async def click(self, *args: Any, **kwargs: Any) -> None:
        self.click_calls += 1
        self._page.clicks.append(self.selector)
        if self._count == 0:
            raise TimeoutError(f"no element for {self.selector}")

    async def press_sequentially(self, text: str, *args: Any, **kwargs: Any) -> None:
        self.press_args.append(text)
        self._page.sequenced.append((self.selector, text))
        if self._count == 0:
            raise TimeoutError(f"no element for {self.selector}")

    async def inner_text(self, *args: Any, **kwargs: Any) -> str:
        if self._count == 0:
            raise TimeoutError(f"no element for {self.selector}")
        return self._text


class _FakePage:
    """selector spec: {selector_value: {"count": int, "text": str, "match": str}}.
    'match' is the has_text value for which .filter() returns a match (count 1)."""

    def __init__(self, spec: dict[str, dict[str, Any]]) -> None:
        self._spec = spec
        self.locator_calls: list[str] = []
        self.locators: dict[str, list[_FakeLocator]] = {}
        self.clicks: list[str] = []
        self.sequenced: list[tuple[str, str]] = []
        self.goto_calls: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        self.locator_calls.append(selector)
        cfg = self._spec.get(selector, {})
        loc = _FakeLocator(
            self, selector,
            count=cfg.get("count", 0),
            text=cfg.get("text", ""),
            match=cfg.get("match"),
        )
        self.locators.setdefault(selector, []).append(loc)
        return loc

    async def goto(self, url: str, *args: Any, **kwargs: Any) -> None:
        self.goto_calls.append(url)

    async def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def wait_for_timeout(self, *args: Any, **kwargs: Any) -> None:
        return None


def _install_fake_playwright(monkeypatch, page: _FakePage) -> dict[str, Any]:
    """Inject stub playwright + playwright.async_api into sys.modules."""
    record: dict[str, Any] = {"launch": [], "ctx_closed": 0, "pw_entered": 0}

    class _FakeContext:
        async def new_page(self) -> _FakePage:
            return page

        async def close(self) -> None:
            record["ctx_closed"] += 1

    class _FakeChromium:
        async def launch_persistent_context(self, user_data_dir: str,
                                            **kwargs: Any) -> _FakeContext:
            record["launch"].append(
                {"user_data_dir": user_data_dir, "kwargs": kwargs})
            return _FakeContext()

    class _FakePlaywright:
        def __init__(self) -> None:
            self.chromium = _FakeChromium()

    class _FakeACM:
        async def __aenter__(self) -> _FakePlaywright:
            record["pw_entered"] += 1
            return _FakePlaywright()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    pkg = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    api.async_playwright = lambda: _FakeACM()
    api.TimeoutError = TimeoutError  # builtin, like real playwright >=1.49
    api.Error = Exception
    pkg.async_api = api
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    return record


def _full_spec(b) -> dict[str, dict[str, Any]]:
    """Every selector present; streaming count 0 (exits poll immediately)."""
    return {
        b.SEL_PROMPT: {"count": 1},
        b.SEL_SEND: {"count": 1},
        b.SEL_TEMPORARY: {"count": 1},
        b.SEL_MODEL_BUTTON: {"count": 0},
        b.SEL_MODEL_OPTION: {"count": 0},
        b.SEL_STREAMING: {"count": 0},
        b.SEL_ASSISTANT: {"count": 1, "text": "ASSISTANT REPLY"},
        b.SEL_LOGGED_OUT: {"count": 0},
    }


# --------------------------------------------------------------------------- #
#  Server-wiring helpers (pattern from tests/test_tools.py::_build_with_conv)
# --------------------------------------------------------------------------- #


class _Conv:
    def __init__(self) -> None:
        self.complete_calls: list[dict[str, Any]] = []
        self.reply = "REST REPLY"

    async def complete(self, model, messages, *, temporary=True, gizmo_id=None,
                       poll_async=False):
        self.complete_calls.append({"model": model, "messages": messages,
                                    "temporary": temporary})
        return self.reply


def _build_tools(monkeypatch, conv, browser_cfg=None, *, has_browser=True):
    import gpt2agent.backend as backend_mod
    import gpt2agent.sse as sse_mod
    from gpt2agent.server import build_server

    monkeypatch.setattr(backend_mod, "BackendClient", lambda *a, **k: object())
    monkeypatch.setattr(sse_mod, "ConversationClient", lambda *a, **k: conv)
    cfg: dict[str, Any] = {
        "server": {"host": "127.0.0.1", "port": 9000},
        "models": {"chat": "gpt-5-3", "agent": "agent-mode"},
    }
    if has_browser:
        cfg["browser"] = browser_cfg if browser_cfg is not None else {}
    return build_server(cfg)._tool_manager._tools


def _stub_browser_module(monkeypatch, reply: str = "STUB REPLY") -> dict[str, Any]:
    """Inject a stub gpt2agent.browser module; records ctor kwargs + chat calls."""
    mod = types.ModuleType("gpt2agent.browser")
    made: dict[str, Any] = {"instances": []}

    class BrowserTransport:
        def __init__(self, profile_dir=None, headed=True, timeout_s=180.0):
            self.ctor = {"profile_dir": profile_dir, "headed": headed,
                         "timeout_s": timeout_s}
            self.calls: list[dict[str, Any]] = []
            made["instances"].append(self)

        async def chat(self, prompt, model=None, temporary=True):
            self.calls.append({"prompt": prompt, "model": model,
                               "temporary": temporary})
            return reply

    mod.BrowserTransport = BrowserTransport
    monkeypatch.setitem(sys.modules, "gpt2agent.browser", mod)
    return made


# --------------------------------------------------------------------------- #
#  1. Import surface
# --------------------------------------------------------------------------- #


def test_module_imports_without_playwright(monkeypatch) -> None:
    """Importing gpt2agent.browser must not require the optional extra."""
    # sys.modules[name] = None forces ImportError on `import name`, simulating
    # playwright being absent even if it happens to be installed here.
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    monkeypatch.delitem(sys.modules, "gpt2agent.browser", raising=False)
    b = _browser_mod()
    assert issubclass(b.BrowserDriftError, RuntimeError)
    for name in _SEL_NAMES:
        assert isinstance(getattr(b, name), str) and getattr(b, name), name


# --------------------------------------------------------------------------- #
#  2. Selector-block invariant
# --------------------------------------------------------------------------- #


def test_selector_block_invariant() -> None:
    """Every lowercase 'selector' lives inside the marked constants block, and
    the banned page APIs never appear anywhere in the file."""
    b = _browser_mod()
    src = Path(inspect.getfile(b)).read_text(encoding="utf-8")
    lines = src.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == _SEL_BLOCK_START]
    ends = [i for i, line in enumerate(lines) if line.strip() == _SEL_BLOCK_END]
    assert len(starts) == 1, "exactly one selector-block start marker required"
    assert len(ends) == 1, "exactly one selector-block end marker required"
    start, end = starts[0], ends[0]
    assert start < end, "end marker must follow start marker"
    # Marker lines themselves contain 'selectors' — range is inclusive.
    for i, line in enumerate(lines):
        if "selector" in line:
            assert start <= i <= end, (
                f"'selector' outside the constants block at line {i + 1}: {line}"
            )
    block = "\n".join(lines[start:end + 1])
    for name in _SEL_NAMES:
        assert name in block, f"{name} must be defined in the selectors block"
    assert "wait_for_selector" not in src
    assert "query_selector" not in src  # covers query_selector_all too


# --------------------------------------------------------------------------- #
#  3. Lazy playwright import — missing extra → install-hint RuntimeError
# --------------------------------------------------------------------------- #


def test_lazy_import_error_message(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    b = _browser_mod()
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(b.BrowserTransport().chat("x"))
    assert _INSTALL_HINT in str(ei.value)


# --------------------------------------------------------------------------- #
#  4. Happy path
# --------------------------------------------------------------------------- #


def test_happy_path_launch_goto_verbatim_prompt_and_reply(
        monkeypatch, tmp_path) -> None:
    b = _browser_mod()
    page = _FakePage(_full_spec(b))
    rec = _install_fake_playwright(monkeypatch, page)
    prof = tmp_path / "chrome-profile"
    transport = b.BrowserTransport(profile_dir=prof, headed=True)
    prompt = "line one\nline two — café ✓ 你好"

    out = asyncio.run(transport.chat(prompt, model=None, temporary=False))

    assert out == "ASSISTANT REPLY"
    # launch: persistent context on the given profile, system Chrome, headed.
    assert len(rec["launch"]) == 1
    launch = rec["launch"][0]
    assert launch["user_data_dir"] == str(prof)
    assert launch["kwargs"].get("channel") == "chrome"
    assert launch["kwargs"].get("headless") is False
    assert rec["pw_entered"] == 1
    assert rec["ctx_closed"] == 1  # context closed in a finally
    assert page.goto_calls == [_CHATGPT_URL]
    # prompt typed VERBATIM into SEL_PROMPT via press_sequentially.
    assert (b.SEL_PROMPT, prompt) in page.sequenced
    assert b.SEL_SEND in page.clicks


def test_streaming_appears_late_is_awaited(monkeypatch) -> None:
    """grok BLOCKING finding (2026-09-15): right after send, the streaming
    indicator has not rendered yet — its absence is the PRE-send DOM and must
    not be read as completion. Here streaming/assistant are absent for the
    first two polls, streaming shows for polls 3-4, then clears. The
    transport must actually OBSERVE the indicator present before treating
    absence as done; the pre-fix code returned on poll 1 and then raised a
    false SEL_ASSISTANT drift."""
    b = _browser_mod()
    spec = _full_spec(b)
    state = {"stream_reads": 0, "present_observed": False}

    class _Delayed(dict):
        def get(self, key, default=None):
            v = super().get(key, default)
            if key == b.SEL_STREAMING:
                state["stream_reads"] += 1
                if state["stream_reads"] <= 2:
                    return {"count": 0}
                if state["stream_reads"] <= 4:
                    state["present_observed"] = True
                    return {"count": 1}
                return {"count": 0}
            if key == b.SEL_ASSISTANT:
                # the assistant node only renders once the reply starts
                if state["stream_reads"] >= 3:
                    return {"count": 1, "text": "ASSISTANT REPLY"}
                return {"count": 0}
            return v

    page = _FakePage(_Delayed(spec))
    _install_fake_playwright(monkeypatch, page)

    async def _nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(b.asyncio, "sleep", _nosleep)

    out = asyncio.run(b.BrowserTransport().chat("hi", temporary=False))

    assert out == "ASSISTANT REPLY"
    assert state["present_observed"] is True, (
        "transport returned without ever observing the streaming indicator "
        "PRESENT — first-poll absence was wrongly treated as completion"
    )


def test_default_profile_dir(monkeypatch) -> None:
    b = _browser_mod()
    page = _FakePage(_full_spec(b))
    rec = _install_fake_playwright(monkeypatch, page)
    asyncio.run(b.BrowserTransport().chat("hi", temporary=False))
    assert rec["launch"][-1]["user_data_dir"] == str(
        Path.home() / ".gpt2agent" / "chrome-profile")


def test_headless_flag_follows_headed_arg(monkeypatch) -> None:
    b = _browser_mod()
    page = _FakePage(_full_spec(b))
    rec = _install_fake_playwright(monkeypatch, page)
    asyncio.run(b.BrowserTransport(headed=False).chat("hi", temporary=False))
    assert rec["launch"][-1]["kwargs"].get("headless") is True


# --------------------------------------------------------------------------- #
#  5. Temporary-chat toggle
# --------------------------------------------------------------------------- #


def test_temporary_true_clicks_toggle(monkeypatch) -> None:
    b = _browser_mod()
    page = _FakePage(_full_spec(b))
    _install_fake_playwright(monkeypatch, page)
    asyncio.run(b.BrowserTransport().chat("hi", temporary=True))
    assert b.SEL_TEMPORARY in page.clicks


def test_temporary_false_never_clicks_toggle(monkeypatch) -> None:
    b = _browser_mod()
    spec = _full_spec(b)
    spec[b.SEL_TEMPORARY] = {"count": 1}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)
    asyncio.run(b.BrowserTransport().chat("hi", temporary=False))
    assert b.SEL_TEMPORARY not in page.clicks


# --------------------------------------------------------------------------- #
#  6. Model picker (best-effort)
# --------------------------------------------------------------------------- #


def test_model_picker_absent_is_not_an_error(monkeypatch) -> None:
    b = _browser_mod()
    spec = _full_spec(b)
    spec[b.SEL_MODEL_BUTTON] = {"count": 0}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)
    out = asyncio.run(b.BrowserTransport().chat("hi", model="gpt-x",
                                                temporary=False))
    assert out == "ASSISTANT REPLY"
    assert b.SEL_MODEL_BUTTON not in page.clicks


def test_model_picker_matching_option_clicked(monkeypatch) -> None:
    b = _browser_mod()
    spec = _full_spec(b)
    spec[b.SEL_MODEL_BUTTON] = {"count": 1}
    spec[b.SEL_MODEL_OPTION] = {"count": 1, "match": "gpt-x"}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)
    out = asyncio.run(b.BrowserTransport().chat("hi", model="gpt-x",
                                                temporary=False))
    assert out == "ASSISTANT REPLY"
    assert b.SEL_MODEL_BUTTON in page.clicks
    assert b.SEL_MODEL_OPTION in page.clicks
    filters = [f for loc in page.locators.get(b.SEL_MODEL_OPTION, [])
               for f in loc.filter_has_text]
    assert "gpt-x" in filters


def test_model_option_miss_fails_closed(monkeypatch) -> None:
    b = _browser_mod()
    spec = _full_spec(b)
    spec[b.SEL_MODEL_BUTTON] = {"count": 1}
    spec[b.SEL_MODEL_OPTION] = {"count": 1, "match": "__never_matches__"}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)
    with pytest.raises(b.BrowserDriftError) as ei:
        asyncio.run(b.BrowserTransport().chat("hi", model="gpt-x",
                                              temporary=False))
    assert "selector" not in str(ei.value)


# --------------------------------------------------------------------------- #
#  7. Selector-miss fail-closed
# --------------------------------------------------------------------------- #


def test_prompt_miss_logged_out_absent_drifts(monkeypatch) -> None:
    b = _browser_mod()
    spec = _full_spec(b)
    spec[b.SEL_PROMPT] = {"count": 0}
    spec[b.SEL_LOGGED_OUT] = {"count": 0}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)
    with pytest.raises(b.BrowserDriftError) as ei:
        asyncio.run(b.BrowserTransport().chat("hi", temporary=False))
    assert "SEL_PROMPT" in str(ei.value)
    assert "selector" not in str(ei.value)


def test_logged_out_warns_login_hint_then_drifts(monkeypatch, capsys, caplog) -> None:
    b = _browser_mod()
    spec = _full_spec(b)
    spec[b.SEL_PROMPT] = {"count": 0}
    spec[b.SEL_LOGGED_OUT] = {"count": 1}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)
    with caplog.at_level("WARNING", logger="gpt2agent.browser"):
        with pytest.raises(b.BrowserDriftError) as ei:
            asyncio.run(b.BrowserTransport().chat("hi", temporary=False))
    assert "SEL_PROMPT" in str(ei.value)
    assert "log in once" in caplog.text
    # stdout is the MCP stdio protocol channel — the hint must NOT print there
    assert capsys.readouterr().out == ""


def test_send_miss_fails_closed(monkeypatch) -> None:
    b = _browser_mod()
    spec = _full_spec(b)
    spec[b.SEL_SEND] = {"count": 0}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)
    with pytest.raises(b.BrowserDriftError) as ei:
        asyncio.run(b.BrowserTransport().chat("hi", temporary=False))
    assert "SEL_SEND" in str(ei.value)


def test_temporary_toggle_miss_fails_closed(monkeypatch) -> None:
    b = _browser_mod()
    spec = _full_spec(b)
    spec[b.SEL_TEMPORARY] = {"count": 0}
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)
    with pytest.raises(b.BrowserDriftError) as ei:
        asyncio.run(b.BrowserTransport().chat("hi", temporary=True))
    assert "SEL_TEMPORARY" in str(ei.value)


# --------------------------------------------------------------------------- #
#  8. Streaming deadline → builtin TimeoutError
# --------------------------------------------------------------------------- #


def test_streaming_deadline_raises_builtin_timeout(monkeypatch) -> None:
    b = _browser_mod()
    spec = _full_spec(b)
    spec[b.SEL_STREAMING] = {"count": 1}  # never finishes streaming
    page = _FakePage(spec)
    _install_fake_playwright(monkeypatch, page)
    with pytest.raises(TimeoutError) as ei:
        asyncio.run(b.BrowserTransport(timeout_s=0.01).chat(
            "hi", temporary=False))
    assert not isinstance(ei.value, b.BrowserDriftError)
    assert "timeout" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
#  9. Server wiring — chat(..., browser=...)
# --------------------------------------------------------------------------- #


def test_server_defaults_gain_browser_section() -> None:
    from gpt2agent.server import _DEFAULTS

    bcfg = _DEFAULTS.get("browser")
    assert bcfg is not None, "_DEFAULTS must gain a 'browser' section"
    assert bcfg.get("enabled") is False
    assert bcfg.get("headed") is True
    assert bcfg.get("timeout_s") == 180


def test_manual_wins_over_browser(monkeypatch) -> None:
    conv = _Conv()
    made = _stub_browser_module(monkeypatch)
    tools = _build_tools(monkeypatch, conv, {"enabled": True})
    raw = asyncio.run(tools["chat"].fn("hello world", manual=True, browser=True))
    handoff = json.loads(raw)
    assert handoff["status"] == "manual_handoff"
    assert handoff["prompt"] == "hello world"
    assert made["instances"] == []          # browser path never touched
    assert conv.complete_calls == []        # REST path never touched


def test_browser_enabled_invokes_transport(monkeypatch, tmp_path) -> None:
    conv = _Conv()
    made = _stub_browser_module(monkeypatch, reply="BROWSER SAYS HI")
    prof = tmp_path / "prof"
    tools = _build_tools(monkeypatch, conv, {
        "enabled": True, "headed": False, "timeout_s": 42,
        "profile_dir": str(prof)})
    out = asyncio.run(tools["chat"].fn(
        "hi there", model="gpt-x", temporary=False, browser=True))
    assert out == "BROWSER SAYS HI"
    assert len(made["instances"]) == 1
    inst = made["instances"][0]
    assert inst.ctor["headed"] is False
    assert inst.ctor["timeout_s"] == 42
    assert str(inst.ctor["profile_dir"]) == str(prof)
    assert inst.calls == [{"prompt": "hi there", "model": "gpt-x",
                           "temporary": False}]
    assert conv.complete_calls == []        # REST path bypassed


@pytest.mark.parametrize("section", ["disabled", "absent"])
def test_browser_not_enabled_raises(monkeypatch, section) -> None:
    conv = _Conv()
    made = _stub_browser_module(monkeypatch)
    tools = _build_tools(
        monkeypatch, conv,
        {"enabled": False} if section == "disabled" else None,
        has_browser=(section == "disabled"))
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(tools["chat"].fn("hi", browser=True))
    assert "[browser]" in str(ei.value)
    assert "enabled" in str(ei.value)
    assert made["instances"] == []


def test_browser_default_false_uses_rest_path(monkeypatch) -> None:
    conv = _Conv()
    made = _stub_browser_module(monkeypatch)
    tools = _build_tools(monkeypatch, conv, {"enabled": True})
    out = asyncio.run(tools["chat"].fn("hi"))
    assert out == "REST REPLY"
    assert conv.complete_calls and conv.complete_calls[-1]["model"] == "gpt-5-3"
    assert made["instances"] == []


# --------------------------------------------------------------------------- #
#  10. Live smoke — real Chrome, real account (SKIP_LIVE-gated)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(_SKIP_LIVE, reason="SKIP_LIVE=1 — set SKIP_LIVE=0 to run live")
def test_browser_chat_live_smoke(monkeypatch) -> None:
    import gpt2agent.backend as backend_mod
    import gpt2agent.sse as sse_mod
    from gpt2agent.server import build_server

    monkeypatch.setattr(backend_mod, "BackendClient", lambda *a, **k: object())
    monkeypatch.setattr(sse_mod, "ConversationClient", lambda *a, **k: object())
    mcp = build_server({
        "server": {"host": "127.0.0.1", "port": 9000},
        "models": {"chat": "gpt-5-3"},
        "browser": {"enabled": True, "headed": True, "timeout_s": 180},
    })
    out = asyncio.run(mcp._tool_manager._tools["chat"].fn(
        "Reply with exactly: BROWSER OK", browser=True))
    assert "BROWSER OK" in out
