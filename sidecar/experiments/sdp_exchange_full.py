#!/usr/bin/env python3
"""Full authenticated GPT-Live SDP exchange — the real handshake.

Reconstructs `ML.startTransceiverSession` from the web bundle: POST
FormData(sdp + session JSON) to /realtime/vp with the account bearer AND the
Sentinel headers (Proof / Chat-Requirements / Turnstile), matching UA + device
id. Reuses gpt2agent's BackendClient + SentinelGate so the Sentinel proof-of-work
is solved the same way the chat endpoint does it.

Reads the SDP offer on stdin, writes the SDP answer to stdout.
"""
import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, "/home/robot/workspace/47-chatgpt2agent/gpt2agent")
from curl_cffi import requests, CurlMime  # noqa: E402
from gpt2agent.backend import BackendClient  # noqa: E402
from gpt2agent.sentinel import SentinelGate  # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "vp"
VOICE_MODE = {"vp": "advanced", "vps": "standard", "wm": "wingman"}.get(MODE, "advanced")
offer = sys.stdin.read()

backend = BackendClient()
backend._reload_token_if_stale()
sess_headers = dict(backend._session.headers)
# Read the bearer straight from auth.json (session headers aren't populated until
# a request runs) — this is the token the raw path used successfully.
access = json.load(open(os.path.expanduser("~/.codex/auth.json")))["tokens"]["access_token"]
tok = f"Bearer {access}"
ua = sess_headers.get("User-Agent")
device = sess_headers.get("OAI-Device-Id", str(uuid.uuid4()))

async def get_tokens_best_effort():
    # Retry get_tokens (Turnstile is probabilistic); if it keeps failing on
    # Turnstile, fall back to proof-only (the realtime POST may not require it).
    last = None
    for _ in range(4):
        try:
            return await SentinelGate(backend).get_tokens()
        except RuntimeError as e:
            last = e
            if "Turnstile" not in str(e):
                raise
    sys.stderr.write(f"[full] turnstile unsolved after retries ({last}); proof-only\n")
    # proof-only fallback: re-run the chat-requirements + POW without turnstile.
    from gpt2agent._vendored import pow as _pow
    from curl_cffi.requests import AsyncSession
    hdrs = dict(backend._session.headers)
    hdrs["Content-Type"] = "application/json"; hdrs["Accept"] = "*/*"
    p = _pow.get_requirements_token(hdrs.get("User-Agent"))
    async with AsyncSession(impersonate="chrome131", verify=True) as s:
        r = await s.post("https://chatgpt.com/backend-api/sentinel/chat-requirements",
                         headers=hdrs, json={"p": p}, timeout=20)
    resp = r.json()
    out = {"chat-requirements": resp.get("token", ""), "proof": ""}
    powb = resp.get("proofofwork") or {}
    if powb.get("required"):
        out["proof"] = await asyncio.to_thread(_pow.solve_pow, powb["seed"], powb["difficulty"], hdrs.get("User-Agent")) or ""
    return out

toks = asyncio.run(get_tokens_best_effort())
sys.stderr.write(f"[full] sentinel: chat-req={bool(toks.get('chat-requirements'))} "
                 f"proof={bool(toks.get('proof'))} turnstile={bool(toks.get('turnstile'))}\n")

headers = {
    "Authorization": tok,
    "User-Agent": ua,
    "OAI-Device-Id": device,
    "Origin": "https://chatgpt.com",
    "Referer": "https://chatgpt.com/",
    "OpenAI-Sentinel-Chat-Requirements-Token": toks.get("chat-requirements", ""),
    "OpenAI-Sentinel-Proof-Token": toks.get("proof", ""),
}
if toks.get("turnstile"):
    headers["OpenAI-Sentinel-Turnstile-Token"] = toks["turnstile"]

session = {"voice_session_id": str(uuid.uuid4()), "voice_mode": VOICE_MODE, "protocol": "transceiver"}
mp = CurlMime()
mp.addpart(name="sdp", data=offer.encode())
mp.addpart(name="session", data=json.dumps(session).encode())

url = f"https://chatgpt.com/realtime/{MODE}?dcid=0"
r = requests.post(url, multipart=mp, headers=headers, impersonate="chrome131", timeout=30)
sys.stderr.write(f"[full] HTTP {r.status_code} len={len(r.text)} ct={r.headers.get('content-type','')}\n")
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
