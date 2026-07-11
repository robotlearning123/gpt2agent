"""Regression tests for the 2026-06-26 cross-model audit (cx + cx2 + ccz + Opus).

Each test pins a specific bug found in the v0.0.7 audit so it can't silently
regress. Test name → finding id maps to artifacts/verify and the audit synthesis.
No network: backend/SSE I/O is faked.
"""

from __future__ import annotations

import asyncio
import json
import stat

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────


def _mk_codex_auth(tmp_path, token: str = "TOK") -> None:
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(json.dumps({"tokens": {"access_token": token}}))


class _Resp:
    def __init__(self, status, text, *, json_exc=False, json_val=None):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers: dict[str, str] = {}
        self._json_exc = json_exc
        self._json_val = json_val

    def json(self):
        if self._json_exc:
            raise ValueError("not json")
        return self._json_val


class _Sess:
    def __init__(self, resp):
        self._resp = resp
        self.headers: dict = {}

    def get(self, _url, **kwargs):
        callback = kwargs.get("content_callback")
        if callback is not None:
            callback(self._resp.content)
        return self._resp


# ── C2: backend.get() non-JSON 2xx guard (parity with post()) ─────────────────


def test_backend_get_non_json_2xx_raises_clean_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _mk_codex_auth(tmp_path)
    from gpt2agent import backend as be

    client = be.BackendClient()
    client._session = _Sess(_Resp(200, "<html>cloudflare</html>", json_exc=True))
    from gpt2agent.errors import BackendContractError

    with pytest.raises(BackendContractError, match="expected a JSON 2xx response"):
        client.get("/backend-api/me")


def test_backend_get_empty_2xx_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _mk_codex_auth(tmp_path)
    from gpt2agent import backend as be

    client = be.BackendClient()
    client._session = _Sess(_Resp(200, "   ", json_exc=True))
    assert client.get("/backend-api/me") is None


def test_backend_get_redacts_secret_in_non_json_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _mk_codex_auth(tmp_path)
    from gpt2agent import backend as be

    client = be.BackendClient()
    leak = "Bearer eyJ" + "a" * 40 + " gateway boom"
    client._session = _Sess(_Resp(200, leak, json_exc=True))
    from gpt2agent.errors import BackendContractError

    with pytest.raises(BackendContractError) as ei:
        client.get("/x")
    assert "eyJ" + "a" * 40 not in str(ei.value)
    assert "gateway boom" not in str(ei.value)


# ── C4: heavy DR captures top-level conversation_id so Phase-2 poll fires ──────


class _FrameResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_content(self):
        for ln in self._lines:
            yield (ln + "\n").encode()


class _FrameSession:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, *a, **k):
        return _FrameResp(self._lines)


class _PollBackend:
    """Quota probe via post(); Phase-2 conversation poll via get()."""

    class _S:
        headers = {"User-Agent": "t"}

    _session = _S()

    def __init__(self, report_detail):
        self._report_detail = report_detail

    def _reload_token_if_stale(self):
        pass

    def request_headers(self):
        return dict(self._session.headers)

    def post(self, *a, **k):
        return {"limits_progress": [{"feature_name": "deep_research", "remaining": 9}]}

    def get(self, *a, **k):
        return self._report_detail


class _StubSentinel:
    def __init__(self, *a, **k):
        pass

    async def get_tokens(self, _operation_headers=None):
        return {"chat-requirements": "x", "proof": "", "turnstile": ""}


def test_heavy_dr_polls_after_toplevel_conversation_id(monkeypatch):
    from gpt2agent import sse as sse_mod

    # Frames: tool invoked + top-level conversation_id (NOT via a marker event),
    # report streams partially, stream closes WITHOUT finished_successfully.
    frames = [
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "msg-tool",
                        "author": {"role": "assistant"},
                        "recipient": "api_tool_chatgpt_deep_research",
                        "content": {"content_type": "text", "parts": ["call"]},
                        "status": "in_progress",
                        "metadata": {},
                    }
                },
                "c": 1,
                "conversation_id": "conv-xyz",
            }
        ),
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "msg-report",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": ["partial"]},
                        "status": "in_progress",
                        "metadata": {},
                    }
                },
                "c": 2,
            }
        ),
        "data: [DONE]",
    ]

    report_detail = {
        "mapping": {
            "n1": {
                "message": {
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["FINAL REPORT"]},
                    "status": "finished_successfully",
                    "create_time": 2,
                    "metadata": {},
                }
            }
        }
    }

    monkeypatch.setattr(sse_mod, "AsyncSession", lambda *a, **k: _FrameSession(frames))
    monkeypatch.setattr(sse_mod, "SentinelGate", _StubSentinel)

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(sse_mod.asyncio, "sleep", _no_sleep)

    client = sse_mod.ConversationClient(_PollBackend(report_detail))

    async def _go():
        out = []
        async for ev in client.deep_research_heavy("q"):
            out.append(ev)
        return out

    events = asyncio.run(_go())
    dones = [e for e in events if e.get("type") == "done"]
    assert dones, f"expected a done event from polling, got {events}"
    assert dones[-1]["text"] == "FINAL REPORT"


# ── C1: null `message` in a v-patch frame must not crash the stream ────────────


def test_stream_tolerates_null_message_frame(monkeypatch):
    from gpt2agent import sse as sse_mod

    frames = [
        "data: " + json.dumps({"v": {"message": None}}),  # would AttributeError pre-fix
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "m1",
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["hello"]},
                        "status": "in_progress",
                    }
                }
            }
        ),
        "data: [DONE]",
    ]

    class _Backend:
        class _S:
            headers = {"User-Agent": "t"}

        _session = _S()

        def _reload_token_if_stale(self):
            pass

        def request_headers(self):
            return dict(self._session.headers)

    monkeypatch.setattr(sse_mod, "AsyncSession", lambda *a, **k: _FrameSession(frames))
    monkeypatch.setattr(sse_mod, "SentinelGate", _StubSentinel)

    client = sse_mod.ConversationClient(_Backend())

    async def _go():
        out = []
        async for ev in client.stream("gpt-5-3", [{"role": "user", "content": "x"}]):
            if isinstance(ev, str):
                out.append(ev)
        return "".join(out)

    assert asyncio.run(_go()) == "hello"


# ── C5: install TOML section editor preserves [[array-of-table]] sections ──────


def test_install_replace_preserves_array_of_table(monkeypatch):
    from gpt2agent import install as inst

    content = (
        "[mcp_servers.gpt2agent]\n"
        'command = "old"\n'
        "\n"
        "[[mcp_servers.other.headers]]\n"
        'name = "X"\n'
        "\n"
        "[model]\n"
        'name = "gpt"\n'
    )
    out = inst._replace_or_append_toml_section(
        content, "mcp_servers.gpt2agent", ['command = "gpt2agent"']
    )
    # The array-of-table block must survive (pre-fix it was swallowed as body).
    assert "[[mcp_servers.other.headers]]" in out
    assert 'name = "X"' in out
    assert "[model]" in out
    assert 'command = "gpt2agent"' in out


# ── S1/S3/S4: secret files written 0o600, never world-readable ────────────────


def test_setup_save_token_is_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from gpt2agent import setup as su

    # Pre-create a world-readable token.json so the test also catches the
    # existing-file case (O_CREAT's mode is ignored when the file exists).
    d = tmp_path / ".gpt2agent"
    d.mkdir(parents=True, exist_ok=True)
    pre = d / "token.json"
    pre.write_text("{}")
    pre.chmod(0o644)

    su.save_token("secret-bearer")
    assert json.loads(pre.read_text())["access_token"] == "secret-bearer"
    assert stat.S_IMODE(pre.stat().st_mode) == 0o600


def test_auth_save_token_tightens_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".gpt2agent"
    d.mkdir(parents=True, exist_ok=True)
    pre = d / "token.json"
    pre.write_text("{}")
    pre.chmod(0o644)

    from gpt2agent import auth

    # _from_browser returns a token → get_token saves it.
    monkeypatch.setattr(auth, "_from_browser", lambda: {"access_token": "B", "source": "browser"})
    # Force the browser path: no codex/saved sources.
    monkeypatch.setattr(auth, "_from_codex", lambda: None)
    monkeypatch.setattr(auth, "_from_browser_use", lambda: None)
    pre.write_text("{}")  # ensure _from_saved sees no usable token
    monkeypatch.setattr(auth, "_from_saved", lambda: None)

    auth.get_token(interactive=True)
    assert stat.S_IMODE(pre.stat().st_mode) == 0o600


def test_install_backup_not_wider_than_source(tmp_path):
    from gpt2agent import install as inst
    from pathlib import Path

    src = Path(tmp_path) / "config.json"
    src.write_text('{"secret": "x"}')
    src.chmod(0o600)
    bak = inst._backup(src)
    assert bak is not None
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600


# ── S7: _log_redact scrubs a bare "token" JSON field ──────────────────────────


def test_log_redact_bare_token_field():
    from gpt2agent._log_redact import redact_error

    out = redact_error('{"token": "eyJSECRET.jwt.value"}')
    assert "eyJSECRET" not in out
    assert "<REDACTED>" in out
    # Must not mangle the word "token" in prose.
    assert redact_error("the token expired") == "the token expired"


# ── S5: tool-output redaction scrubs pasted secrets, not just PII ─────────────


def test_tool_redact_secrets():
    from gpt2agent.tools._redact import redact

    jwt = "eyJhbGciOi" + "A" * 20 + ".eyJzdWIi" + "B" * 20 + ".sig" + "C" * 20
    assert "<JWT>" in redact(f"my key is {jwt} ok")
    assert jwt not in redact(jwt)
    assert "<APIKEY>" in redact("sk-" + "a" * 30)
    assert "Bearer <REDACTED>" in redact("Authorization: Bearer " + "z" * 40)
    assert "<TOKEN>" in redact("github_pat_" + "A" * 24)
    # PII still works; ordinary prose untouched.
    assert redact("ping me at a@b.com") == "ping me at <EMAIL>"
    assert redact("just some words") == "just some words"


# ── C7: auth._from_saved accepts every token shape backend accepts ────────────


@pytest.mark.parametrize(
    "blob",
    [
        {"access_token": "T"},
        {"token": "T"},
        {"tokens": {"access_token": "T"}},
    ],
)
def test_auth_from_saved_accepts_all_shapes(tmp_path, monkeypatch, blob):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".gpt2agent" / "token.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(blob))
    from gpt2agent import auth

    res = auth._from_saved()
    assert res is not None and res["access_token"] == "T"


def test_auth_get_token_prefers_codex_home_over_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    saved = tmp_path / ".gpt2agent" / "token.json"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps({"access_token": "STALE_SAVED"}))

    codex_home = tmp_path / ".codex-alt"
    codex_auth = codex_home / "auth.json"
    codex_auth.parent.mkdir(parents=True, exist_ok=True)
    codex_auth.write_text(json.dumps({"tokens": {"access_token": "CODEX_HOME_TOKEN"}}))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    from gpt2agent import auth

    assert auth.get_token(interactive=False) == "CODEX_HOME_TOKEN"


@pytest.mark.parametrize(
    "blob",
    [
        {"access_token": "T"},
        {"token": "T"},
        {"tokens": {"access_token": "T"}},
    ],
)
def test_setup_from_saved_accepts_all_shapes(tmp_path, monkeypatch, blob):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".gpt2agent" / "token.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(blob))
    from gpt2agent import setup

    assert setup._token_from_saved() == "T"


# ── C8: apps preserves explicit is_connected=False ────────────────────────────


def test_apps_preserves_explicit_false(monkeypatch):
    from gpt2agent.tools import apps

    captured = {}

    class _MCP:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    class _Client:
        def get(self, *a, **k):
            return {"apps": [{"id": "connector_x", "enabled": True, "is_connected": False}]}

    apps.register(_MCP(), _Client())
    out = asyncio.run(captured["list_apps"]())
    assert out[0]["connected"] is False


# ── C9: load_config raises on an explicit-but-missing path ────────────────────


def test_load_config_missing_explicit_path_raises(tmp_path):
    from gpt2agent.server import load_config
    from pathlib import Path

    with pytest.raises(FileNotFoundError):
        load_config(Path(tmp_path) / "nope.toml")


# ── C6: get_conversation follows the active branch in chronological order ──────


def test_get_conversation_follows_active_chain():
    from gpt2agent.tools import conversations

    captured = {}

    class _MCP:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    def _node(nid, parent, role, text, t):
        return {
            "id": nid,
            "parent": parent,
            "message": {
                "id": nid,
                "author": {"role": role},
                "content": {"content_type": "text", "parts": [text]},
                "status": "finished_successfully",
                "create_time": t,
            },
        }

    detail = {
        "id": "c1",
        "title": "t",
        "current_node": "a2",
        "mapping": {
            "root": {"id": "root", "parent": None, "message": None},
            "u1": _node("u1", "root", "user", "hello", 1),
            "a1": _node("a1", "u1", "assistant", "hi", 2),
            # An abandoned sibling branch that must NOT appear in the active chain.
            "a1b": _node("a1b", "u1", "assistant", "STALE BRANCH", 2),
            "u2": _node("u2", "a1", "user", "more", 3),
            "a2": _node("a2", "u2", "assistant", "done", 4),
        },
    }

    class _Client:
        def get(self, *a, **k):
            return detail

    conversations.register(_MCP(), _Client())
    out = asyncio.run(captured["get_conversation"]("c1"))
    texts = [m["text"] for m in out["messages"]]
    assert texts == ["hello", "hi", "more", "done"]
    assert "STALE BRANCH" not in texts

    limited = asyncio.run(captured["get_conversation"]("c1", max_messages=2))
    assert [m["text"] for m in limited["messages"]] == ["more", "done"]
