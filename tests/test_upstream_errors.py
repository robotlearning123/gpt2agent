"""Tests for the upstream-breakage error types and their raise sites.

chatgpt.com changed its Sentinel challenge (see the 0.0.12 "Known issues" entry):
the conversation path cannot be opened while every read-only REST surface still
answers. The contract pinned here is that this failure surfaces as a *specific*
``RuntimeError`` subclass carrying an honest explanation — existing
``except RuntimeError`` handlers and ``pytest.raises(RuntimeError, ...)``
assertions keep working, while new code can tell upstream breakage apart from a
token or configuration problem.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from gpt2agent import sentinel as sentinel_mod
from gpt2agent import sse as sse_mod
from gpt2agent.backend import UpstreamChallengeError, UpstreamEndpointError


# ── the types themselves ─────────────────────────────────────────────────────


def test_upstream_errors_subclass_runtimeerror() -> None:
    """Both must remain catchable as RuntimeError — existing handlers depend on it."""
    assert issubclass(UpstreamChallengeError, RuntimeError)
    assert issubclass(UpstreamEndpointError, RuntimeError)


@pytest.mark.parametrize("exc_type", [UpstreamChallengeError, UpstreamEndpointError])
def test_upstream_errors_satisfy_runtimeerror_assertions(exc_type: type) -> None:
    with pytest.raises(RuntimeError, match="upstream"):
        raise exc_type("an upstream change broke this tool")


# ── sentinel raise sites ─────────────────────────────────────────────────────


class _SentinelBackend:
    class _Session:
        headers: dict[str, str] = {"User-Agent": "test-agent"}

    _session = _Session()


def _patch_sentinel_response(
    monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    class _Response:
        status_code = 200
        text = json.dumps(payload)

        def json(self) -> dict:
            return payload

    class _Session:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _Response:
            return _Response()

    monkeypatch.setattr(sentinel_mod, "AsyncSession", _Session)
    monkeypatch.setattr(sentinel_mod._pow, "get_requirements_token", lambda _: "p")


def _unsolvable_turnstile(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sentinel_response(
        monkeypatch,
        {
            "token": "chat-token",
            "proofofwork": {"required": False},
            "turnstile": {"required": True, "dx": "challenge"},
        },
    )
    monkeypatch.setattr(sentinel_mod._turn, "solve_turnstile", lambda *_: None)


def test_turnstile_unsolved_raises_upstream_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unsolvable_turnstile(monkeypatch)
    gate = sentinel_mod.SentinelGate(_SentinelBackend())  # type: ignore[arg-type]

    with pytest.raises(UpstreamChallengeError) as excinfo:
        asyncio.run(gate.get_tokens())

    # The stage that failed stays on the first line — see also the older
    # assertions in test_audit_2026_07_09_streaming.py, which match on it.
    assert "required Turnstile challenge could not be solved" in str(excinfo.value)


def test_turnstile_without_dx_raises_upstream_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sentinel_response(
        monkeypatch,
        {
            "token": "chat-token",
            "proofofwork": {"required": False},
            "turnstile": {"required": True},
        },
    )
    gate = sentinel_mod.SentinelGate(_SentinelBackend())  # type: ignore[arg-type]

    with pytest.raises(UpstreamChallengeError):
        asyncio.run(gate.get_tokens())


def test_pow_unsolved_raises_upstream_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The POW stage is the same class of failure: the gate cannot be satisfied."""
    _patch_sentinel_response(
        monkeypatch,
        {
            "token": "chat-token",
            "proofofwork": {"required": True, "seed": "s", "difficulty": "d"},
            "turnstile": {"required": False},
        },
    )
    monkeypatch.setattr(sentinel_mod._pow, "solve_pow", lambda *_: None)
    gate = sentinel_mod.SentinelGate(_SentinelBackend())  # type: ignore[arg-type]

    with pytest.raises(UpstreamChallengeError) as excinfo:
        asyncio.run(gate.get_tokens())

    assert "required POW challenge could not be solved" in str(excinfo.value)


def test_challenge_message_tells_the_user_the_three_things(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not their token/config, which tools are affected, what still works."""
    _unsolvable_turnstile(monkeypatch)
    gate = sentinel_mod.SentinelGate(_SentinelBackend())  # type: ignore[arg-type]

    with pytest.raises(UpstreamChallengeError) as excinfo:
        asyncio.run(gate.get_tokens())

    message = str(excinfo.value)
    # 1. upstream change, not the user's token or configuration
    assert "not a problem with your token or configuration" in message
    assert "re-logging in will not fix it" in message
    # 2. which tool families are affected
    for tool in (
        "chat",
        "agent",
        "deep_research",
        "generate_image",
        "memory_create_via_chat",
    ):
        assert tool in message, f"{tool} missing from the affected-tool list"
    # 3. read-only tools still work, and where to get a live status
    assert "Read-only tools" in message
    assert "gpt2agent doctor" in message


def test_conversation_path_surfaces_challenge_error_as_runtimeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP chat/DR/agent tools see the specific error, still a RuntimeError."""

    class _BlockedGate:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def get_tokens(self) -> dict[str, str]:
            raise UpstreamChallengeError(
                "required Turnstile challenge could not be solved.\n"
                "This is a change on ChatGPT's side."
            )

    class _Backend:
        class _Session:
            headers: dict[str, str] = {"User-Agent": "test-agent"}

        _session = _Session()

        def _reload_token_if_stale(self) -> None:
            pass

    monkeypatch.setattr(sse_mod, "SentinelGate", _BlockedGate)

    with pytest.raises(UpstreamChallengeError):
        asyncio.run(
            sse_mod.ConversationClient(_Backend()).complete(  # type: ignore[arg-type]
                "gpt-5-6", [{"role": "user", "content": "hi"}]
            )
        )


# ── list_apps endpoint change ────────────────────────────────────────────────


class _MCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a: Any, **k: Any):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class _Client:
    def __init__(self, error: Exception | None = None, payload: dict | None = None):
        self.error = error
        self.payload = payload

    def get(self, path: str, target_path: str | None = None, **_: Any) -> Any:
        if self.error is not None:
            raise self.error
        return self.payload


def test_list_apps_405_raises_upstream_endpoint_error() -> None:
    from gpt2agent.tools import apps

    client = _Client(error=RuntimeError("HTTP 405 for /backend-api/apps/list"))
    mcp = _MCP()
    apps.register(mcp, client)

    with pytest.raises(UpstreamEndpointError) as excinfo:
        asyncio.run(mcp.tools["list_apps"]())

    message = str(excinfo.value)
    assert "/backend-api/apps/list" in message
    assert "405" in message
    assert "not a problem with your token" in message


def test_list_apps_405_from_post_shape_also_classified() -> None:
    """backend.get and backend.post spell the 405 differently; both must map."""
    from gpt2agent.tools import apps

    client = _Client(error=RuntimeError("405 Method Not Allowed: /backend-api/apps/list"))
    mcp = _MCP()
    apps.register(mcp, client)

    with pytest.raises(UpstreamEndpointError):
        asyncio.run(mcp.tools["list_apps"]())


def test_list_apps_other_errors_pass_through_unchanged() -> None:
    from gpt2agent.tools import apps

    client = _Client(error=RuntimeError("401 Unauthorized — token expired"))
    mcp = _MCP()
    apps.register(mcp, client)

    with pytest.raises(RuntimeError, match="401") as excinfo:
        asyncio.run(mcp.tools["list_apps"]())

    assert not isinstance(excinfo.value, UpstreamEndpointError)


def test_list_apps_success_path_unchanged() -> None:
    from gpt2agent.tools import apps

    client = _Client(
        payload={
            "apps": [
                {"id": "connector_openai_codex_tasks", "enabled": True, "is_connected": True},
                {"id": "asdk_app_1", "enabled": False, "connected": False},
                "not-a-dict",
            ]
        }
    )
    mcp = _MCP()
    apps.register(mcp, client)

    out = asyncio.run(mcp.tools["list_apps"]())
    assert [a["id"] for a in out] == ["connector_openai_codex_tasks", "asdk_app_1"]
    assert out[1]["connected"] is False  # explicit False survives
