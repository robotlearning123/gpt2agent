"""Sentinel bridge — restores the REST conversation path.

Technique (proven live 2026-09-17 on /backend-api/conversation, 200 SSE):
a consistent session seeds CF cookies, a browser-fingerprint config array
generates the requirements ``p`` token, PoW solves with that config, and a
bytecode-VM turnstile token (from requirements["turnstile"]["dx"]) completes
the three openai-sentinel-* headers.

The turnstile VM interpreter is loaded from an owner-supplied directory
(GPT2AGENT_SENTINEL_BRIDGE, default /tmp/cgpt-rev) because the from-scratch
interpreter (see docs/dev/specs/sentinel-vm.md) is still being completed —
that directory is NOT part of this distribution and must be provided
locally. When the built-in interpreter lands, this bridge disappears.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from random import random as _rand

_log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
CLIENT_VERSION = "prod-0917-fallback"
TIMEZONE = "America/New_York"
IP_LATLNG = "39.04,-77.49"


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


def fingerprint_config(device_id: str) -> list:
    """The 18-element browser fingerprint payload for the p token."""
    return [
        4880,
        datetime.now(ZoneInfo(TIMEZONE)).strftime(
            "%a %b %d %Y %H:%M:%S GMT%z (EDT)"),
        4294705152, _rand(), UA, None, CLIENT_VERSION, "en-US", "en-US,en",
        _rand(), "webkitGetUserMedia−function webkitGetUserMedia() "
        "{ [native code] }",
        "location", "window", 1700.5 + _rand(), device_id, "", 20,
        int(time.time() * 1000),
    ]


def p_token(config: list) -> str:
    """"gAAAAAC" + base64(compact JSON) of the config array."""
    from base64 import b64encode
    return "gAAAAAC" + b64encode(
        json.dumps(config, separators=(",", ":")).encode()).decode()


class SentinelBridge:
    """Mints the full sentinel header set for one session."""

    def __init__(self) -> None:
        self._ch = _load("challenges")
        self._vm = _load("vm")

    def mint(self, session, bearer: str, device_id: str) -> dict:
        """Seed cookies are expected on `session` already (one GET of
        chatgpt.com). Returns the headers to add to the conversation POST."""
        config = fingerprint_config(device_id)
        p = p_token(config)
        r = session.post(
            "https://chatgpt.com/backend-api/sentinel/chat-requirements",
            json={"p": p},
            headers={"Authorization": f"Bearer {bearer}",
                     "oai-device-id": device_id,
                     "oai-client-version": CLIENT_VERSION},
            timeout=30)
        req = r.json()
        if r.status_code != 200 or not req.get("token"):
            raise RuntimeError(
                f"sentinel requirements failed: {r.status_code} "
                f"{str(req)[:120]}")
        pw = req.get("proofofwork") or {}
        proof = ""
        if isinstance(pw, dict) and pw.get("seed") is not None:
            proof = self._ch.Challenges.solve_pow(
                pw["seed"], pw["difficulty"], config) or ""
        dx = (req.get("turnstile") or {}).get("dx") or ""
        ts = self._vm.VM.get_turnstile(dx, p, IP_LATLNG) if dx else ""
        if not ts:
            raise RuntimeError("sentinel bridge: turnstile token empty")
        return {
            "oai-device-id": device_id,
            "oai-client-version": CLIENT_VERSION,
            "openai-sentinel-chat-requirements-token": req["token"],
            "openai-sentinel-proof-token": proof,
            "openai-sentinel-turnstile-token": ts,
        }


def seed_session(session, device_id: str) -> None:
    """GET chatgpt.com once to seed CF cookies on the session."""
    last = None
    for _ in range(4):
        try:
            session.get("https://chatgpt.com/", timeout=25)
            return
        except Exception as exc:  # noqa: BLE001 - retry loop
            last = exc
            time.sleep(2)
    raise RuntimeError(f"sentinel bridge: cannot seed session: {last}")


def new_device_id() -> str:
    return str(uuid.uuid4())
