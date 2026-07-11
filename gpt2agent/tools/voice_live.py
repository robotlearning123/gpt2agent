"""GPT-Live Mode B export — control-only MCP surface.

Audio stays in the browser sidecar. These tools talk to the sidecar's localhost
control plane (default http://127.0.0.1:8741) using text/control only.

Headless Turnstile / bot-detection bypass is out of scope. Start the export with:

  cd sidecar && node browser/sidecar.mjs --profile ./.chrome-gptlive --audio q.wav

See sidecar/README.md and GET /help on the control port.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from mcp.types import ToolAnnotations

if TYPE_CHECKING:
    from gpt2agent.backend import BackendClient

_DEFAULT_CONTROL = "http://127.0.0.1:8741"
_ENV_CONTROL = "GPT2AGENT_LIVE_CONTROL"
_MAX_TEXT = 8000
_TIMEOUT_S = 5.0

_EXPORT_HELP = """GPT-Live Mode B export (experimental, optional, disabled-by-default).

Architecture:
  human mic ──WebRTC──▶ headed Chrome (ChatGPT Voice UI)
                              │ datachannel text only
                              ▼
                     sidecar export plane ──▶ agent hook / MCP tools
                              │
                     speak wire (text) ──▶ Live TTS (audio stays in browser)

How to start:
  1. Sign into chatgpt.com in a dedicated Chrome profile (one-time human login).
  2. cd sidecar && node browser/sidecar.mjs --profile ./.chrome-gptlive --audio q.wav
  3. Use voice_live_status / voice_live_get_transcript / voice_live_send_text / voice_live_end.

Boundary:
  - No raw audio, SDP, bearer tokens, or cookies cross this MCP boundary.
  - Cloudflare Turnstile bypass is OUT OF SCOPE. Token-only/headless SDP is not
    the supported export path (server aborts ~1s after listening).
  - Mode A (Live natively calling external MCP tools) is not supported.
"""


def _control_base() -> str:
    return (os.environ.get(_ENV_CONTROL) or _DEFAULT_CONTROL).rstrip("/")


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_control_base()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {"ok": True}
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return {"ok": True, "data": parsed}
            return parsed
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        return {"ok": False, "error": f"HTTP {e.code}", "detail": detail}
    except urllib.error.URLError as e:
        return {
            "ok": False,
            "error": "control plane unreachable",
            "detail": str(e.reason if hasattr(e, "reason") else e),
            "hint": (
                "Start the browser sidecar first: "
                "node sidecar/browser/sidecar.mjs --profile ./.chrome-gptlive --audio q.wav"
            ),
            "control": _control_base(),
        }
    except TimeoutError:
        return {"ok": False, "error": "control plane timeout", "control": _control_base()}
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid JSON from control plane"}


def _bound_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    t = text.strip()
    if len(t) > _MAX_TEXT:
        raise ValueError(f"text exceeds {_MAX_TEXT} characters")
    return t


def _strip_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Defense in depth: never return credential/media-shaped keys to the agent.

    Blocks exact secret/media field names (and *_token / *password suffixes).
    Does not redact boolean flags that merely mention audio (e.g. audioCrossesBoundary).
    """
    blocked = {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "cookie",
        "cookies",
        "password",
        "secret",
        "client_secret",
        "audio",
        "audio_bytes",
        "pcm",
        "sdp",
        "wire",
        "proof_token",
        "sentinel",
    }
    out: dict[str, Any] = {}
    for k, v in payload.items():
        lk = k.lower()
        is_blocked = (
            lk in blocked
            or lk.endswith("_token")
            or lk.endswith("token")
            or lk.endswith("_secret")
            or lk.endswith("password")
            or lk.endswith("_cookie")
            or lk.endswith("_sdp")
            or lk.endswith("sdp")
        )
        if is_blocked:
            out[k] = "[redacted]"
        elif isinstance(v, dict):
            out[k] = _strip_secrets(v)
        else:
            out[k] = v
    return out


def register(mcp, client: Any = None) -> None:
    """Register control-only GPT-Live export tools (no audio transport)."""

    # client is unused: Live media is not driven through BackendClient.
    _ = client

    annotations_ro = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    annotations_write = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )

    @mcp.tool(
        name="voice_live_export_help",
        annotations=annotations_ro,
    )
    async def voice_live_export_help() -> str:
        """How to export GPT-Live to an agent (Mode B) and the Turnstile boundary."""
        return _EXPORT_HELP + f"\nControl base: {_control_base()}\n"

    @mcp.tool(
        name="voice_live_status",
        annotations=annotations_ro,
    )
    async def voice_live_status() -> dict[str, Any]:
        """Status of the local GPT-Live export control plane (no audio/secrets)."""
        result = _request("GET", "/status")
        return _strip_secrets(result)

    @mcp.tool(
        name="voice_live_get_transcript",
        annotations=annotations_ro,
    )
    async def voice_live_get_transcript(clear: bool = False) -> dict[str, Any]:
        """Drain buffered human/agent transcript text from the Live export plane."""
        path = "/transcript?clear=1" if clear else "/transcript"
        result = _request("GET", path)
        return _strip_secrets(result)

    @mcp.tool(
        name="voice_live_send_text",
        annotations=annotations_write,
    )
    async def voice_live_send_text(text: str) -> dict[str, Any]:
        """Make GPT-Live speak agent reply text (TTS via browser; no audio on MCP)."""
        try:
            body = {"text": _bound_text(text)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        result = _request("POST", "/send_text", body)
        return _strip_secrets(result)

    @mcp.tool(
        name="voice_live_end",
        annotations=annotations_write,
    )
    async def voice_live_end() -> dict[str, Any]:
        """End the GPT-Live export session via the local control plane."""
        result = _request("POST", "/end")
        return _strip_secrets(result)
