"""Unit tests for the 2026-06-18 audit hardening — no network.

Tests include:
  * `_log_redact.redact_error` — bare Bearer, named token fields, cookies, query tokens.
  * `server._http_bind_decision` — unauthenticated HTTP is disabled.
  * `sse._dr_report_from_widget_state` — author-role gate, prefix-must-start,
    and in-progress drafts rejected (DR-report spoofing/premature-done guards).
  * `_poll_dr_completion` requests the hidden-widget-state query flags.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from gpt2agent import server as server_mod
from gpt2agent import sse as sse_mod
from gpt2agent._log_redact import redact_error
from gpt2agent.errors import BackendContractError
from gpt2agent.server import _http_bind_decision


# --------------------------------------------------------------------------- #
#  _log_redact.redact_error
# --------------------------------------------------------------------------- #


def test_redact_bare_bearer() -> None:
    out = redact_error("upstream said: Bearer eyJabc.DEF-123_xyz tail", max_len=500)
    assert "eyJabc.DEF-123_xyz" not in out
    assert "Bearer <REDACTED>" in out


def test_redact_named_token_fields() -> None:
    out = redact_error('{"tokens": {"access_token": "s3cr3t-value"}}', max_len=500)
    assert "s3cr3t-value" not in out
    assert "<REDACTED>" in out


def test_redact_session_cookie() -> None:
    out = redact_error("Cookie: __Secure-next-auth.session-token=a.b.c; Path=/", max_len=500)
    assert "a.b.c" not in out


def test_redact_query_token_keeps_other_params() -> None:
    out = redact_error("GET /download?access_token=zzz999&file=report.md", max_len=500)
    assert "zzz999" not in out
    assert "file=report.md" in out  # non-secret params preserved


def test_redact_base64_bearer_fully() -> None:
    # realistic base64 token (+, /, and trailing = padding) must be fully masked
    tok = "eyJhbGci0iJ.payLoad+ab/cd9z=="
    out = redact_error(f"Authorization failed for Bearer {tok}", max_len=500)
    assert tok not in out
    assert "payLoad" not in out and "ab/cd9z" not in out
    assert "Bearer <REDACTED>" in out


def test_redact_bearer_does_not_eat_short_prose() -> None:
    # "Bearer of bad news" must NOT be redacted — the {16,} floor preserves prose
    out = redact_error("the Bearer of bad news arrived", max_len=500)
    assert out == "the Bearer of bad news arrived"


def test_redact_camelcase_query_token() -> None:
    out = redact_error("/cb?accessToken=SEKRET&sessionToken=ALSOSEKRET&ok=1", max_len=500)
    assert "SEKRET" not in out and "ALSOSEKRET" not in out
    assert "ok=1" in out


def test_redact_quoted_authorization_header() -> None:
    out = redact_error('{"Authorization": "Bearer eyJsecret"}', max_len=500)
    assert "eyJsecret" not in out


def test_redact_truncates_long_body() -> None:
    assert redact_error("A" * 1000, max_len=50).endswith("...[truncated]")


def test_image_result_drops_opaque_upstream_metadata() -> None:
    secret = "Bearer synthetic-private-image-secret"
    client = object.__new__(sse_mod.ConversationClient)

    result = client._extract_image_result(
        "conversation-1",
        {
            "metadata": {"authorization": secret, "private_prompt": secret},
            "content": {
                "parts": [
                    {
                        "content_type": "image_asset_pointer",
                        "asset_pointer": "sediment://file-1",
                        "width": 1024,
                        "height": 768,
                        "size_bytes": 1234,
                        "metadata": {
                            "authorization": secret,
                            "generation": {
                                "serialization_title": "Image Generation metadata"
                            },
                        },
                    }
                ]
            },
        },
    )

    assert secret not in repr(result)
    assert set(result) == {"conversation_id", "assets"}
    assert set(result["assets"][0]) == {
        "asset_pointer",
        "file_id",
        "width",
        "height",
        "size_bytes",
    }


def test_image_result_rejects_opaque_conversation_id_without_secret_echo() -> None:
    secret = "Bearer " + "q" * 24
    client = object.__new__(sse_mod.ConversationClient)

    with pytest.raises(BackendContractError) as caught:
        client._extract_image_result(
            {"authorization": secret},
            {"content": {"parts": []}},
        )

    assert secret not in str(caught.value)


def test_image_result_redacts_secret_shaped_ids_and_validates_asset_scalars() -> None:
    secret = "sk-" + "r" * 24
    client = object.__new__(sse_mod.ConversationClient)

    result = client._extract_image_result(
        secret,
        {
            "content": {
                "parts": [
                    {
                        "content_type": "image_asset_pointer",
                        "asset_pointer": f"sediment://{secret}",
                        "width": 1,
                        "metadata": {
                            "generation": {
                                "serialization_title": "Image Generation metadata"
                            }
                        },
                    }
                ]
            }
        },
    )

    assert secret not in repr(result)
    assert result["conversation_id"] == "<APIKEY>"
    assert result["assets"][0]["file_id"] == "<APIKEY>"


def test_image_result_rejects_oversized_asset_list_and_numeric_values() -> None:
    client = object.__new__(sse_mod.ConversationClient)
    asset = {
        "content_type": "image_asset_pointer",
        "asset_pointer": "sediment://file-safe",
        "width": 1,
        "metadata": {
            "generation": {
                "serialization_title": "Image Generation metadata"
            }
        },
    }

    with pytest.raises(BackendContractError, match="asset list"):
        client._extract_image_result(
            "conversation-1", {"content": {"parts": [asset] * 101}}
        )
    with pytest.raises(BackendContractError, match="bounded non-negative integer"):
        client._extract_image_result(
            "conversation-1",
            {
                "content": {
                    "parts": [{**asset, "width": 1 << 80}],
                }
            },
        )


# --------------------------------------------------------------------------- #
#  server._http_bind_decision
# --------------------------------------------------------------------------- #


def test_bind_loopback_is_refused_without_per_user_authentication() -> None:
    assert _http_bind_decision("127.0.0.1", False) == "refuse"
    assert _http_bind_decision("localhost", False) == "refuse"
    assert _http_bind_decision("::1", False) == "refuse"


def test_bind_non_loopback_refused_without_optin() -> None:
    assert _http_bind_decision("0.0.0.0", False) == "refuse"
    assert _http_bind_decision("192.168.1.5", False) == "refuse"


def test_bind_non_loopback_legacy_optin_is_ignored() -> None:
    assert _http_bind_decision("0.0.0.0", True) == "refuse"
    assert _http_bind_decision("192.168.1.5", True) == "refuse"


def test_build_server_rejects_non_loopback_before_constructing_backend() -> None:
    with pytest.raises(ValueError, match="server.host must be a loopback address"):
        server_mod.build_server(
            {
                "server": {"host": "0.0.0.0", "port": 9000},
                "models": {"chat": "gpt-5-3"},
            }
        )


def test_build_server_canonicalizes_accepted_loopback_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gpt2agent.backend as backend_mod
    import gpt2agent.model_catalog as model_catalog_mod
    import gpt2agent.sse as sse_mod

    class _Backend:
        pass

    class _Catalog:
        def __init__(self, _backend: object) -> None:
            pass

    class _Conversation:
        def __init__(self, _backend: object) -> None:
            pass

    monkeypatch.setattr(backend_mod, "BackendClient", _Backend)
    monkeypatch.setattr(model_catalog_mod, "ModelCatalog", _Catalog)
    monkeypatch.setattr(sse_mod, "ConversationClient", _Conversation)

    mcp = server_mod.build_server(
        {
            "server": {"host": " LOCALHOST ", "port": 9000},
            "models": {"chat": "gpt-5-3"},
        }
    )

    assert mcp.settings.host == "localhost"


class _RecordingMCP:
    def __init__(self) -> None:
        self.transports: list[str] = []

    def run(self, *, transport: str) -> None:
        self.transports.append(transport)


@pytest.mark.parametrize("argv", [["gpt2agent"], ["gpt2agent", "run"]])
def test_cli_defaults_to_stdio(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    mcp = _RecordingMCP()
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(
        server_mod,
        "load_config",
        lambda _path=None: {
            "server": {"host": "127.0.0.1", "port": 9000},
            "models": {"chat": "gpt-5-3"},
        },
    )
    monkeypatch.setattr(server_mod, "build_server", lambda _cfg: mcp)

    server_mod.main()

    assert mcp.transports == ["stdio"]


def test_cli_http_transport_is_disabled_before_server_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["gpt2agent", "run", "--http"])
    monkeypatch.setattr(
        server_mod,
        "load_config",
        lambda _path=None: {
            "server": {"host": "127.0.0.1", "port": 9000},
            "models": {"chat": "gpt-5-3"},
        },
    )
    monkeypatch.setattr(
        server_mod,
        "build_server",
        lambda _cfg: pytest.fail("disabled HTTP must not construct the account server"),
    )

    with pytest.raises(SystemExit, match="HTTP transport is disabled"):
        server_mod.main()

def test_cli_stdio_ignores_legacy_non_loopback_http_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _RecordingMCP()
    built: list[dict[str, Any]] = []
    monkeypatch.setattr(sys, "argv", ["gpt2agent", "run", "--stdio"])
    monkeypatch.setattr(
        server_mod,
        "load_config",
        lambda _path=None: {
            "server": {"host": "0.0.0.0", "port": 9000},
            "models": {"chat": "gpt-5-3"},
        },
    )

    def _build(cfg: dict[str, Any]) -> _RecordingMCP:
        built.append(cfg)
        return mcp

    monkeypatch.setattr(server_mod, "build_server", _build)

    server_mod.main()

    assert built[0]["server"]["host"] == "127.0.0.1"
    assert mcp.transports == ["stdio"]


def test_current_config_guidance_never_recommends_removed_remote_bypass() -> None:
    guidance = "\n".join(
        [
            Path("config.example.toml").read_text(encoding="utf-8"),
            Path("gpt2agent/setup.py").read_text(encoding="utf-8"),
        ]
    )
    assert "GPT2AGENT_ALLOW_REMOTE" not in guidance


def test_active_repository_surfaces_never_recommend_disabled_http() -> None:
    root = Path(".")
    historical_prefixes = (
        Path("docs/superpowers"),
        Path("tests"),
    )
    generated_prefixes = (Path(".venv"),)
    historical_files = {Path("CHANGELOG.md"), Path("QA_REPORT.html")}
    text_suffixes = {".html", ".md", ".py", ".sh", ".toml"}
    forbidden = (
        "gpt2agent run --http",
        "--transport http",
        "streamable-http",
        "Streamable HTTP",
        "streamable HTTP",
        "streamable-HTTP",
        "loopback-only HTTP",
        "loopback-only transport",
    )
    violations: list[str] = []

    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if (
            not path.is_file()
            or path.suffix not in text_suffixes
            or relative in historical_files
            or any(relative.is_relative_to(prefix) for prefix in historical_prefixes)
            or any(relative.is_relative_to(prefix) for prefix in generated_prefixes)
        ):
            continue
        text = path.read_text(encoding="utf-8")
        for phrase in forbidden:
            if phrase in text:
                violations.append(f"{relative}: {phrase}")

    assert violations == []


# --------------------------------------------------------------------------- #
#  sse._dr_report_from_widget_state hardening
# --------------------------------------------------------------------------- #


def _carrier(role: str, *, report_status: str, widget_status: str, body: str, lead: str = "") -> dict:
    state = {
        "status": widget_status,
        "report_message": {
            "content": {"content_type": "text", "parts": [body]},
            "status": report_status,
            "metadata": {"content_references": [{"items": []}]},
        },
    }
    part = lead + sse_mod._WIDGET_STATE_TEXT_PREFIX + json.dumps(state)
    return {
        "mapping": {
            "n": {
                "message": {
                    "author": {"role": role, "name": "api_tool.widget_state"},
                    "recipient": "all",
                    "status": "finished_successfully",
                    "content": {"content_type": "text", "parts": [part]},
                    "metadata": {
                        "exclusive_key": "widget_state:Deep Research App_start",
                        "is_visually_hidden_from_conversation": True,
                    },
                }
            }
        }
    }


def test_widget_tool_completed_accepted() -> None:
    detail = _carrier("tool", report_status="finished_successfully", widget_status="completed", body="# Real report")
    text, refs = sse_mod._dr_report_from_widget_state(detail)
    assert text == "# Real report"
    assert refs


def test_widget_user_role_spoof_rejected() -> None:
    """A user-authored message containing the widget prefix must NOT be parsed as the report."""
    detail = _carrier("user", report_status="finished_successfully", widget_status="completed", body="# SPOOFED")
    text, refs = sse_mod._dr_report_from_widget_state(detail)
    assert text == ""
    assert refs == []


def test_widget_arbitrary_tool_name_spoof_rejected() -> None:
    detail = _carrier(
        "tool",
        report_status="finished_successfully",
        widget_status="completed",
        body="# SPOOFED",
    )
    detail["mapping"]["n"]["message"]["author"]["name"] = "arbitrary_tool"

    assert sse_mod._dr_report_from_widget_state(detail) == ("", [])


def test_widget_text_carrier_requires_connector_envelope_fields() -> None:
    for field in ("exclusive_key", "is_visually_hidden_from_conversation"):
        detail = _carrier(
            "tool",
            report_status="finished_successfully",
            widget_status="completed",
            body="# SPOOFED",
        )
        del detail["mapping"]["n"]["message"]["metadata"][field]
        assert sse_mod._dr_report_from_widget_state(detail) == ("", [])

    for field in ("recipient", "status"):
        detail = _carrier(
            "tool",
            report_status="finished_successfully",
            widget_status="completed",
            body="# SPOOFED",
        )
        del detail["mapping"]["n"]["message"][field]
        assert sse_mod._dr_report_from_widget_state(detail) == ("", [])


def test_widget_in_progress_draft_rejected() -> None:
    detail = _carrier("tool", report_status="in_progress", widget_status="running", body="half-written draft")
    text, _ = sse_mod._dr_report_from_widget_state(detail)
    assert text == ""


def test_widget_malformed_chatgpt_sdk_scalar_ignored() -> None:
    """A non-dict `chatgpt_sdk` (e.g. a stray string) must be ignored, not crash."""
    detail = {
        "mapping": {
            "n": {
                "message": {
                    "author": {"role": "tool"},
                    "content": {"content_type": "code", "parts": [None]},
                    "metadata": {"chatgpt_sdk": "not-a-dict"},
                }
            }
        }
    }
    text, refs = sse_mod._dr_report_from_widget_state(detail)  # must not raise
    assert text == ""
    assert refs == []


def test_widget_prefix_must_start_the_part() -> None:
    """Prefix embedded mid-string (e.g. inside a pasted blob) must not match."""
    detail = _carrier(
        "tool", report_status="finished_successfully", widget_status="completed",
        body="# x", lead="here is some chatter: ",
    )
    text, _ = sse_mod._dr_report_from_widget_state(detail)
    assert text == ""


# --------------------------------------------------------------------------- #
#  _poll_dr_completion requests the widget-state query flags (cx PR#9 test gap)
# --------------------------------------------------------------------------- #


def test_poll_requests_widget_state_query_flags() -> None:
    detail = _carrier("tool", report_status="finished_successfully", widget_status="completed", body="# R")
    seen: dict[str, str] = {}

    class _RecordingBackend:
        class _Sess:
            headers: dict[str, str] = {"User-Agent": "test"}

        _session = _Sess()

        def _reload_token_if_stale(self) -> None:
            pass

        def get(self, path: str, *a: Any, **k: Any) -> dict:
            seen["path"] = path
            return detail

    client = sse_mod.ConversationClient(_RecordingBackend())  # type: ignore[arg-type]

    async def _go() -> None:
        async for _ev in client._poll_dr_completion("cid", interval=0, max_wait=1):
            break

    asyncio.run(_go())
    assert "include_visually_hidden_messages=true" in seen["path"]
    assert "include_widget_state=true" in seen["path"]
