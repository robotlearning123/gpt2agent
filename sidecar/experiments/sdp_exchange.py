#!/usr/bin/env python3
"""SDP exchange helper for the live GPT-Live connect experiment.

Reads an SDP offer on stdin, POSTs it to the realtime endpoint with the account
bearer (curl_cffi Chrome impersonation — the same path gpt2agent uses, avoids the
Cloudflare challenge plain fetch hits), writes the SDP answer to stdout.

Token is read from ~/.codex/auth.json; it is never printed or passed via argv.
Usage:  python sdp_exchange.py [vp|vps|wm]   < offer.sdp   > answer.sdp
"""
import json
import os
import sys

from curl_cffi import requests

mode = sys.argv[1] if len(sys.argv) > 1 else "vp"
offer = sys.stdin.read()
tok = json.load(open(os.path.expanduser("~/.codex/auth.json")))["tokens"]["access_token"]
url = f"https://chatgpt.com/realtime/{mode}?dcid=0"
r = requests.post(
    url,
    data=offer,
    headers={
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/sdp",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    },
    impersonate="chrome124",
    timeout=30,
)
sys.stderr.write(f"[sdp_exchange] HTTP {r.status_code} len={len(r.text)}\n")
if r.status_code not in (200, 201):
    sys.stderr.write(r.text[:400] + "\n")
    sys.exit(2)
sys.stdout.write(r.text)
