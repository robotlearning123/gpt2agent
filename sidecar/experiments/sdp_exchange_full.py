#!/usr/bin/env python3
"""Full authenticated GPT-Live SDP exchange (handshake completion experiment).

The bare `application/sdp` + Bearer POST returns a lenient 201 but an ephemeral
session the server drops ~1s after `listening`. The real web client posts
FormData(sdp + session JSON) with the account's oai-* headers AND the Sentinel
proof tokens. This helper reconstructs that using gpt2agent's own machinery:

  - `BackendClient._session` supplies the authenticated curl_cffi session with
    the full oai-device-id / oai-session-id / oai-client-version header set.
  - `SentinelGate.get_tokens()` solves the POW (+ turnstile) and yields the
    chat-requirements + proof tokens the client sends as `Openai-Sentinel-*`.

Reads offer on stdin, writes answer SDP on stdout. Tokens are never printed.

  SDP_PY=<venv> node ... (spawns) python sdp_exchange_full.py [vp|vps|wm]

Env knobs for iterating on the still-uncertain `session` object:
  SESSION_JSON='{"...":...}'   # override the whole session object
  NO_SENTINEL=1                # omit sentinel headers (A/B the proof's effect)
"""

import asyncio
import json
import os
import sys
import uuid

from curl_cffi import CurlMime

from gpt2agent.backend import BackendClient
from gpt2agent.sentinel import SentinelGate

mode = sys.argv[1] if len(sys.argv) > 1 else "vp"
offer = sys.stdin.read()
url = f"https://chatgpt.com/realtime/{mode}?dcid=0"

backend = BackendClient()
sess = backend._session  # authenticated curl_cffi session (oai-* + Authorization)

# The session object the voice client posts alongside the SDP. Fields are the
# best reconstruction from the web bundle; override via SESSION_JSON to iterate.
if os.environ.get("SESSION_JSON"):
    session_obj = json.loads(os.environ["SESSION_JSON"])
else:
    session_obj = {
        "voice_session_id": str(uuid.uuid4()),
        "protocol": "transceiver",
        "integrated_mode": False,
    }

headers = dict(sess.headers)
headers.pop("Content-Type", None)
headers.pop("content-type", None)  # let CurlMime set the multipart boundary
headers["Accept"] = "*/*"

if os.environ.get("NO_SENTINEL") != "1":
    # get_tokens() hard-fails if a Turnstile challenge is required but the
    # vendored solver can't produce it. The realtime handshake may only need
    # chat-requirements + POW proof, so tolerate turnstile failure (BEST_EFFORT)
    # and send whatever we could solve.
    try:
        tokens = asyncio.run(SentinelGate(backend).get_tokens())
    except RuntimeError as exc:
        if "Turnstile" not in str(exc) or os.environ.get("BEST_EFFORT") != "1":
            raise
        # Re-run the requirements call directly, keeping chat-requirements + proof.
        import gpt2agent._vendored.pow as _pow
        from curl_cffi.requests import Session as _S

        h = dict(sess.headers)
        h["Content-Type"] = "application/json"
        h["Accept"] = "*/*"
        ua = h.get("user-agent") or h.get("User-Agent") or ""
        p = _pow.get_requirements_token(ua)
        with _S(impersonate="chrome131") as s2:
            rr = s2.post(
                "https://chatgpt.com/backend-api/sentinel/chat-requirements",
                headers=h,
                json={"p": p},
                timeout=20,
            )
        rj = rr.json()
        tokens = {"chat-requirements": rj.get("token", ""), "proof": ""}
        pw = rj.get("proofofwork") or {}
        if pw.get("required"):
            tokens["proof"] = _pow.solve_pow(pw.get("seed"), pw.get("difficulty"), ua) or ""
        sys.stderr.write("[sdp_full] turnstile unsolved; proceeding with chat-requirements+proof\n")
    headers["Openai-Sentinel-Chat-Requirements-Token"] = tokens["chat-requirements"]
    if tokens.get("proof"):
        headers["Openai-Sentinel-Proof-Token"] = tokens["proof"]
    if tokens.get("turnstile"):
        headers["Openai-Sentinel-Turnstile-Token"] = tokens["turnstile"]

mp = CurlMime()
mp.addpart(name="sdp", data=offer.encode())
mp.addpart(name="session", data=json.dumps(session_obj).encode())

r = sess.post(url, multipart=mp, headers=headers, timeout=30)
sys.stderr.write(
    f"[sdp_full] HTTP {r.status_code} len={len(r.text)} "
    f"ct={r.headers.get('content-type', '')} sentinel={'off' if os.environ.get('NO_SENTINEL') == '1' else 'on'}\n"
)
if r.status_code not in (200, 201):
    sys.stderr.write(r.text[:400] + "\n")
    sys.exit(2)

body = r.text
if body.lstrip().startswith("{"):
    try:
        body = json.loads(body).get("sdp", body)
    except Exception:
        pass
sys.stdout.write(body)
