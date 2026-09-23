"""MCP SDK compatibility: the server constructor must adapt to both layouts.

mcp 1.x (``FastMCP``) takes ``host``/``port`` keyword arguments; mcp 2.x
(``MCPServer``, which ``FastMCP`` aliases) dropped them — they moved onto the
``run()``/app factories. Passing them unconditionally crashed
``gpt2agent run`` at startup (measured against mcp 2.2.0, 2026-09-23).
"""

from __future__ import annotations

import pytest

from gpt2agent import server


class _V2Style:
    """mcp 2.x-style constructor: no host/port parameters."""

    def __init__(self, name: str, log_level: str | None = None) -> None:
        self.name = name
        self.log_level = log_level


class _V1Style:
    """mcp 1.x-style constructor: host/port accepted."""

    def __init__(
        self,
        name: str,
        host: str = "127.0.0.1",
        port: int = 8000,
        log_level: str | None = None,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.log_level = log_level


def test_guard_omits_host_port_for_v2_style_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "FastMCP", _V2Style)
    kwargs = server._fastmcp_kwargs({"host": "10.0.0.1", "port": 8123})
    assert kwargs == {"log_level": "WARNING"}
    _V2Style("gpt2agent", **kwargs)  # constructing must not raise


def test_guard_passes_host_port_for_v1_style_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "FastMCP", _V1Style)
    kwargs = server._fastmcp_kwargs({"host": "10.0.0.1", "port": 8123})
    assert kwargs == {"host": "10.0.0.1", "port": 8123, "log_level": "WARNING"}


def test_guard_defaults_host_port_when_config_omits_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "FastMCP", _V1Style)
    kwargs = server._fastmcp_kwargs({})
    assert kwargs == {"host": "127.0.0.1", "port": 9000, "log_level": "WARNING"}


def test_power_the_unpatched_call_is_what_crashes() -> None:
    """Power demo: the naive call really does raise on a v2-style ctor."""
    with pytest.raises(TypeError, match="host"):
        _V2Style("gpt2agent", host="127.0.0.1", port=9000, log_level="WARNING")


def test_build_server_call_site_uses_the_guard_under_v2_style_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Call-site regression test.

    Pins the actual ``build_server`` construction, not just the helper: a
    revert to an unconditional ``FastMCP(host=..., port=...)`` call must fail
    here when the SDK constructor is v2-shaped. The stub is a subclass of the
    real FastMCP so tool registration still exercises the full code path.
    """
    import gpt2agent.backend as backend_mod
    import gpt2agent.sse as sse_mod

    real = server.FastMCP

    class _V2LikeReal(real):  # type: ignore[misc, valid-type]
        def __init__(self, name: str, log_level: str | None = None) -> None:
            super().__init__(name, log_level=log_level)

    monkeypatch.setattr(server, "FastMCP", _V2LikeReal)
    monkeypatch.setattr(backend_mod, "BackendClient", lambda *a, **k: object())
    monkeypatch.setattr(sse_mod, "ConversationClient", lambda *a, **k: object())

    cfg = {
        "server": {"host": "10.0.0.1", "port": 8123},
        "models": {"chat": "gpt-5-6", "agent": "agent-mode"},
        "browser": {},
    }
    mcp = server.build_server(cfg)
    assert isinstance(mcp, _V2LikeReal)  # constructed without host/port
    assert len(mcp._tool_manager._tools) == 30
