"""Sentinel bridge — restores the REST conversation path.

Technique (proven live 2026-09-17 on /backend-api/conversation, 200 SSE):
a consistent session seeds CF cookies, a browser-fingerprint config array
generates the requirements ``p`` token, PoW solves with that config, and a
bytecode-VM turnstile token (from requirements["turnstile"]["dx"]) completes
the three openai-sentinel-* headers.

v0.0.17: the mint now runs inside the shared ``SimProfile`` session —
same impersonation/UA/device-id as the conversation POST (previously the
mint ran chrome133a+Win/Chrome-140 while the POST went out chrome131+
macOS/Chrome-131, a cross-checked fingerprint mismatch). The mint also
performs the frontend's ``f/conversation/prepare`` handshake so callers
can send ``x-conduit-token`` + ``oai-echo-logs``, and ``oai-client-version``
is the site's real ``data-build`` value instead of a fabricated string.

The turnstile VM interpreter is loaded from an owner-supplied directory
(GPT2AGENT_SENTINEL_BRIDGE, default ~/.gpt2agent/sentinel-bridge) because
the from-scratch interpreter (see docs/dev/specs/sentinel-vm.md) is still
being completed — that directory is NOT part of this distribution and must
be provided locally. When the built-in interpreter lands, this bridge
disappears.
"""
from __future__ import annotations

import json
import logging
import os
import time
from base64 import b64encode
from pathlib import Path

from gpt2agent.ratelimit import get_limiter
from gpt2agent.sim import SimProfile, get_profile

_log = logging.getLogger(__name__)

#: Circuit-breaker key for mint failures — after 3 consecutive failures the
#: bridge cools down 10 min instead of hammering chat-requirements while
#: flagged (which makes upstream flagging worse).
_BREAKER_KEY = "sentinel_mint"
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN_S = 600.0

#: Signals upstream uses when it has flagged the session/account.
_UNUSUAL = ("unusual activity", "too many requests")


def _bridge_dir() -> Path:
    return Path(os.environ.get("GPT2AGENT_SENTINEL_BRIDGE", str(Path.home() / ".gpt2agent" / "sentinel-bridge")))


def _load(name: str):
    """Import a reverse-engineering module from the bridge directory.

    The bridge modules use package-relative imports internally, so they must
    be imported through their package path (bridge dir on sys.path), not as
    standalone files."""
    import sys
    d = str(_bridge_dir())
    if d not in sys.path:
        sys.path.insert(0, d)
    import importlib
    return importlib.import_module(f"wrapper.reverse.{name}")


def fingerprint_config(device_id: str, profile: SimProfile | None = None) -> list:
    """The 18-element browser fingerprint payload for the p token."""
    return (profile or get_profile()).fingerprint_config()


def p_token(config: list) -> str:
    """"gAAAAAC" + base64(compact JSON) of the config array."""
    return "gAAAAAC" + b64encode(
        json.dumps(config, separators=(",", ":")).encode()).decode()


def get_conduit(session, profile: SimProfile, bearer: str, model: str) -> str | None:
    """POST /backend-api/f/conversation/prepare → conduit token.

    The real frontend does this handshake before every conversation POST;
    the response carries ``conduit_token`` which must be echoed back as
    ``x-conduit-token``. Returns None when the endpoint refuses — callers
    then omit the header rather than abort (the token is additive).
    """
    body = {
        "action": "next",
        "fork_from_shared_post": False,
        "parent_message_id": "client-created-root",
        "model": model,
        "timezone_offset_min": profile.timezone_offset_min,
        "timezone": profile.timezone,
        "history_and_training_disabled": True,
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": [],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
    }
    try:
        r = session.post(
            "https://chatgpt.com/backend-api/f/conversation/prepare",
            json=body,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "x-conduit-token": "no-token",
            },
            timeout=30,
        )
        if r.status_code == 200:
            tok = (r.json() or {}).get("conduit_token")
            if tok:
                return tok
        _log.debug("conduit prepare %s: %s", r.status_code, str(r.text)[:120])
    except Exception as exc:  # noqa: BLE001 - conduit is additive, fail soft
        _log.debug("conduit prepare failed: %s", exc)
    return None


class SentinelBridge:
    """Mints the full sentinel header set inside the shared profile session."""

    def __init__(self, profile: SimProfile | None = None) -> None:
        self._profile = profile or get_profile()
        self._ch = _load("challenges")
        self._vm = _load("vm")

    def mint(self, bearer: str, *, model: str = "auto") -> dict:
        """Seed cookies are expected on the profile session already (one GET
        of chatgpt.com, done by ``SimProfile.ensure_session``).

        Returns ``{"headers": {...}, "cookies": {...}}`` — the headers carry
        the sentinel triple plus ``x-conduit-token``/``oai-echo-logs``/
        ``oai-device-id``/``oai-client-version`` so the conversation POST
        looks like the real frontend end-to-end."""
        prof = self._profile
        lim = get_limiter()
        until = lim.breaker_open(_BREAKER_KEY)
        if until:
            from datetime import datetime

            raise RuntimeError(
                "sentinel bridge circuit breaker open until "
                f"{datetime.fromtimestamp(until).isoformat(timespec='seconds')} "
                f"({_BREAKER_THRESHOLD}+ consecutive mint failures) — not "
                "retrying upstream; use browser=True meanwhile"
            )
        sess = prof.ensure_session()

        config = prof.fingerprint_config()
        p = p_token(config)
        r = sess.post(
            "https://chatgpt.com/backend-api/sentinel/chat-requirements",
            json={"p": p},
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
            },
            timeout=30,
        )
        req = {}
        try:
            req = r.json()
        except Exception:
            pass
        # "Unusual activity" means upstream flagged the session — cool down
        # hard and drop the cookie jar; retrying as-is digs the hole deeper.
        blob = str(req or r.text).lower()
        if any(sig in blob for sig in _UNUSUAL):
            lim.note_cooldown(f"breaker:{_BREAKER_KEY}", time.time() + 1800)
            prof.drop_session()
            raise RuntimeError(
                "sentinel requirements flagged 'unusual activity' — "
                "cooling the bridge for 30 min; use browser=True")
        if r.status_code != 200 or not (isinstance(req, dict) and req.get("token")):
            lim.note_failure(
                _BREAKER_KEY, _BREAKER_THRESHOLD, _BREAKER_COOLDOWN_S
            )
            raise RuntimeError(
                f"sentinel requirements failed: {r.status_code} "
                f"{str(req or r.text)[:120]}")

        pw = req.get("proofofwork") or {}
        proof = ""
        if isinstance(pw, dict) and pw.get("seed") is not None:
            proof = self._ch.Challenges.solve_pow(
                pw["seed"], pw["difficulty"], config) or ""
        dx = (req.get("turnstile") or {}).get("dx") or ""
        # Upstream passes str([ip, city, region, lat, lng]) into the VM.
        ts = self._vm.VM.get_turnstile(dx, p, str(prof.ip_info)) if dx else ""
        if not ts:
            lim.note_failure(
                _BREAKER_KEY, _BREAKER_THRESHOLD, _BREAKER_COOLDOWN_S
            )
            raise RuntimeError("sentinel bridge: turnstile token empty")

        headers = {
            "oai-device-id": prof.device_id,
            "oai-client-version": prof.client_version,
            "oai-echo-logs": prof.echo_logs(),
            "openai-sentinel-chat-requirements-token": req["token"],
            "openai-sentinel-proof-token": proof,
            "openai-sentinel-turnstile-token": ts,
        }
        conduit = get_conduit(sess, prof, bearer, model)
        if conduit:
            headers["x-conduit-token"] = conduit
        lim.note_success(_BREAKER_KEY)
        # Keep the cookie jar continuous across process restarts.
        prof.persist_cookies()
        return {"headers": headers, "cookies": dict(sess.cookies)}


def seed_session(session, device_id: str) -> None:
    """Back-compat shim — seeding is owned by ``SimProfile.ensure_session``."""
    get_profile().seed_session(session)


def new_device_id() -> str:
    """Back-compat shim — the persistent id lives on the profile."""
    return get_profile().device_id
