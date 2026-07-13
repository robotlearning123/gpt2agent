"""GPT-Live → coding-agent bridge — observe-only MCP surface (human → agent).

Audio stays in the browser sidecar. These tools talk to the sidecar's localhost
control plane (default http://127.0.0.1:8741) using text/control only: read the
observed human transcript and bridge status, and end the session.

There is NO "make Live speak" tool — GPT-Live silently drops client-injected
speech, so the agent reply reaches the human out-of-band (a text overlay in the
extension bridge), not through this MCP surface.

Headless Turnstile / bot-detection bypass is out of scope. The reliable bridge is
your real signed-in Chrome + sidecar/extension + sidecar/agent-gateway.mjs. Those
Node/extension files ship with the SOURCE REPO, not the PyPI wheel — clone
https://github.com/robotlearning123/gpt2agent to run them.

See sidecar/README.md and GET /help on the control port.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from mcp.types import ToolAnnotations

_DEFAULT_CONTROL = "http://127.0.0.1:8741"
_ENV_CONTROL = "GPT2AGENT_LIVE_CONTROL"
_TIMEOUT_S = 5.0

_EXPORT_HELP = """GPT-Live → coding-agent bridge (experimental, optional). Direction: human → agent.

Architecture:
  human mic ──WebRTC──▶ signed-in Chrome (ChatGPT Voice UI)
                              │ datachannel: chat_message_delta (human transcript)
                              ▼
                     bridge layer ──▶ coding agent (repo/tools) via agent gateway
                              │
                     agent reply ──▶ text overlay to the human (NOT spoken by Live)

The Node sidecar + Chrome extension are NOT bundled in the PyPI wheel — clone the
source repo to get them: https://github.com/robotlearning123/gpt2agent (sidecar/).

Reliable path (real human voice):
  1. Sign into chatgpt.com in a dedicated Chrome profile (one-time human login).
  2. Load sidecar/extension as an unpacked extension in that Chrome.
  3. AGENT_CMD='claude -p' node sidecar/agent-gateway.mjs   (agent + control plane)
  4. Start voice on chatgpt.com and talk. Observe via voice_live_status /
     voice_live_get_transcript; end via voice_live_end.
  (browser/sidecar.mjs with a fake WAV mic is a TEST harness — Live does not
   transcribe synthetic audio.)

Boundary:
  - No raw audio, SDP, bearer tokens, or cookies cross this MCP boundary.
  - agent→Live speak-injection is UNSUPPORTED (server silently drops it); there is
    no "make Live speak" tool. The reply reaches the human out-of-band.
  - Cloudflare Turnstile bypass is OUT OF SCOPE.
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


_BLOCKED_KEYS = frozenset(
    {
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
)

# A JWT or "Bearer <jwt>" appearing anywhere inside a string value.
_JWT_RE = re.compile(r"(?:Bearer\s+)?[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def _key_is_blocked(key: str) -> bool:
    lk = key.lower()
    return (
        lk in _BLOCKED_KEYS
        or lk.endswith("_token")
        or lk.endswith("token")
        or lk.endswith("_secret")
        or lk.endswith("password")
        or lk.endswith("_cookie")
        or lk.endswith("_sdp")
        or lk.endswith("sdp")
    )


def _redact_value(value: Any, depth: int = 0) -> Any:
    """Recursively redact secrets in dicts, lists, and strings (JWT/Bearer)."""
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if _key_is_blocked(k) else _redact_value(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(v, depth + 1) for v in value]
    if isinstance(value, str) and _JWT_RE.search(value):
        return _JWT_RE.sub("[redacted]", value)
    return value


def _strip_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Defense in depth: never return credential/media-shaped values to the agent.

    Redacts blocked key names AND recurses through dicts, lists, and strings so an
    embedded bearer/JWT (e.g. inside a `lastError` message or an array element)
    cannot leak. Does not redact boolean flags that merely mention audio
    (e.g. audioCrossesBoundary).
    """
    return _redact_value(payload)


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
        """Status of the local GPT-Live bridge control plane (no audio/secrets)."""
        result = _request("GET", "/status")
        return _strip_secrets(result)

    @mcp.tool(
        name="voice_live_get_transcript",
        annotations=annotations_ro,
    )
    async def voice_live_get_transcript(clear: bool = False) -> dict[str, Any]:
        """Drain the observed human/agent transcript text from the bridge (human → agent)."""
        path = "/transcript?clear=1" if clear else "/transcript"
        result = _request("GET", path)
        return _strip_secrets(result)

    @mcp.tool(
        name="voice_live_end",
        annotations=annotations_write,
    )
    async def voice_live_end() -> dict[str, Any]:
        """End the GPT-Live bridge session via the local control plane."""
        result = _request("POST", "/end")
        return _strip_secrets(result)
