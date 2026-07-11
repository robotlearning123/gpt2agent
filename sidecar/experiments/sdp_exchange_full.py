#!/usr/bin/env python3
"""Full authenticated GPT-Live SDP exchange — the real handshake experiment.

Reconstructs `ML.startTransceiverSession` from the web bundle: POST
FormData(sdp + session JSON) to /realtime/{mode} with the account bearer AND the
Sentinel headers (Chat-Requirements / Proof / Turnstile), matching UA + device
id. Reuses gpt2agent's BackendClient + SentinelGate so the Sentinel proof-of-work
is solved the same way the chat endpoint does it.

Reads the SDP offer on stdin, writes the SDP answer to stdout. Tokens are never
printed.

  SDP_PY=<venv-python> node ... (spawns) python sdp_exchange_full.py [vp|vps|wm]

Findings (2026-07-11): the POW proof solves, but the Cloudflare Turnstile
challenge does NOT solve headlessly (gpt2agent's own solver fails here). With
proof-only the POST returns HTTP 201 but the browser peer goes connecting->failed
(the un-Turnstiled session is torn down at ICE). The autonomous no-login path is
blocked by Turnstile by design; a logged-in real browser (browser/sidecar.mjs)
solves it natively. Env knobs below are for continued disambiguation.

Env knobs:
  SESSION_JSON='{"...":...}'   # override the whole session object to iterate
  NO_SENTINEL=1                # omit sentinel headers (A/B the proof's effect)
"""

import asyncio
import json
import os
import sys
import uuid

from curl_cffi import CurlMime
from curl_cffi import requests as _rq

from gpt2agent.backend import BackendClient
from gpt2agent.sentinel import SentinelGate

MODE = sys.argv[1] if len(sys.argv) > 1 else "vp"
VOICE_MODE = {"vp": "advanced", "vps": "standard", "wm": "wingman"}.get(MODE, "advanced")
offer = sys.stdin.read()
url = f"https://chatgpt.com/realtime/{MODE}?dcid=0"

backend = BackendClient()
sess_headers = dict(backend._session.headers)
access = json.load(open(os.path.expanduser("~/.codex/auth.json")))["tokens"]["access_token"]
ua = sess_headers.get("User-Agent") or sess_headers.get("user-agent") or ""
device = sess_headers.get("OAI-Device-Id") or sess_headers.get("oai-device-id") or str(uuid.uuid4())


async def get_tokens_best_effort() -> dict:
    """SentinelGate.get_tokens with turnstile tolerated (it is probabilistic and
    the vendored solver fails headlessly). Falls back to chat-requirements+proof."""
    last = None
    for _ in range(4):
        try:
            return await SentinelGate(backend).get_tokens()
        except RuntimeError as e:
            last = e
            if "Turnstile" not in str(e):
                raise
    sys.stderr.write(f"[full] turnstile unsolved after retries ({last}); proof-only\n")
    from curl_cffi.requests import AsyncSession

    from gpt2agent._vendored import pow as _pow

    hdrs = dict(backend._session.headers)
    hdrs["Content-Type"] = "application/json"
    hdrs["Accept"] = "*/*"
    p = _pow.get_requirements_token(ua)
    async with AsyncSession(impersonate="chrome131", verify=True) as s:
        r = await s.post(
            "https://chatgpt.com/backend-api/sentinel/chat-requirements",
            headers=hdrs,
            json={"p": p},
            timeout=20,
        )
    resp = r.json()
    out = {"chat-requirements": resp.get("token", ""), "proof": ""}
    powb = resp.get("proofofwork") or {}
    if powb.get("required"):
        out["proof"] = (
            await asyncio.to_thread(_pow.solve_pow, powb["seed"], powb["difficulty"], ua) or ""
        )
    return out


headers = {
    "Authorization": f"Bearer {access}",
    "User-Agent": ua,
    "OAI-Device-Id": device,
    "Origin": "https://chatgpt.com",
    "Referer": "https://chatgpt.com/",
    "Accept": "*/*",
}

if os.environ.get("NO_SENTINEL") != "1":
    toks = asyncio.run(get_tokens_best_effort())
    sys.stderr.write(
        f"[full] sentinel: chat-req={bool(toks.get('chat-requirements'))} "
        f"proof={bool(toks.get('proof'))} turnstile={bool(toks.get('turnstile'))}\n"
    )
    headers["Openai-Sentinel-Chat-Requirements-Token"] = toks.get("chat-requirements", "")
    if toks.get("proof"):
        headers["Openai-Sentinel-Proof-Token"] = toks["proof"]
    if toks.get("turnstile"):
        headers["Openai-Sentinel-Turnstile-Token"] = toks["turnstile"]

if os.environ.get("SESSION_JSON"):
    session_obj = json.loads(os.environ["SESSION_JSON"])
else:
    session_obj = {
        "voice_session_id": str(uuid.uuid4()),
        "voice_mode": VOICE_MODE,
        "protocol": "transceiver",
    }

mp = CurlMime()
mp.addpart(name="sdp", data=offer.encode())
mp.addpart(name="session", data=json.dumps(session_obj).encode())

r = _rq.post(url, multipart=mp, headers=headers, impersonate="chrome131", timeout=30)
sys.stderr.write(
    f"[full] HTTP {r.status_code} len={len(r.text)} ct={r.headers.get('content-type', '')} "
    f"sentinel={'off' if os.environ.get('NO_SENTINEL') == '1' else 'on'}\n"
)
if r.status_code not in (200, 201):
    sys.stderr.write(r.text[:500] + "\n")
    sys.exit(2)

body = r.text
if body.lstrip().startswith("{"):
    try:
        body = json.loads(body).get("sdp", body)
    except Exception:
        pass
sys.stdout.write(body)
