#!/usr/bin/env python3
"""One live light-DR POST with a payload variant; record frames + verdict.

Investigation probe for the 2026-09-23 "Error in message stream" abort.
Faithful to production: reuses ConversationClient._request_setup (limiter,
model cap, sentinel bridge) and _build_dr_payload, then applies overrides.

Exit prints one line: VERDICT=<ALIVE|ABORT|HTTPxxx> plus frame counts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


async def run(args: argparse.Namespace) -> int:
    import gpt2agent.sse as sse
    from gpt2agent.backend import BackendClient

    frames: list[dict] = []
    orig_raise = sse._raise_for_sse_error

    def record(obj: dict) -> None:
        frames.append(obj)
        err = obj.get("error")
        if err and not args.keep_going:
            raise _Abort(err)

    class _Abort(Exception):
        pass

    sse._raise_for_sse_error = record

    conv = sse.ConversationClient(BackendClient())
    query = (
        "Summarize what changed in Python 3.14 versus 3.13, citing python.org "
        "release notes. Under 150 words. Proceed without clarification."
    )
    payload = sse._build_dr_payload(query)
    if args.model:
        payload["model"] = args.model
    if args.hints is not None:
        payload["system_hints"] = [h.strip() for h in args.hints.split(",") if h.strip()]
        msg0 = payload.get("messages", [{}])[0]
        msg0.setdefault("metadata", {})["system_hints"] = payload["system_hints"]
    for key, val in (json.loads(args.set) if args.set else {}).items():
        if key.startswith("meta."):
            payload["messages"][0].setdefault("metadata", {})[key[5:]] = val
        else:
            payload[key] = val

    headers, conv_url = await conv._request_setup(payload.get("model", sse.DR_MODEL))
    print(f"POST {conv_url} model={payload.get('model')} "
          f"hints={payload.get('system_hints')} extra={sorted((json.loads(args.set) if args.set else {}).keys())}",
          flush=True)

    from curl_cffi.requests import AsyncSession

    assistant_seen = False
    last_role = None
    verdict = "ABORT"
    http_status = None
    async with AsyncSession(impersonate=sse.get_profile().impersonate, verify=True) as s:
        bc = getattr(conv, "_bridge_cookies", None)
        if bc:
            for k, v in bc.items():
                s.cookies.set(k, v)
        resp = await s.post(conv_url, headers=headers, json=payload,
                            timeout=args.max_wait, stream=True)
        http_status = resp.status_code
        if resp.status_code not in (200, 201):
            body = ""
            async for chunk in resp.aiter_content():
                body += chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
                if len(body) > 400:
                    break
            print(f"VERDICT=HTTP{resp.status_code} body={body[:400]!r}")
            return 2
        try:
            async for raw_line in resp.aiter_lines():
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8", "replace")
                line = raw_line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    verdict = "ALIVE"
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                try:
                    record(obj)
                except _Abort as exc:
                    print(f"IN-BAND ABORT: {exc}")
                    break
                msg = obj.get("message") or (obj.get("v") or {}).get("message")
                if isinstance(msg, dict):
                    last_role = (msg.get("author") or {}).get("role")
                    if last_role == "assistant":
                        assistant_seen = True
        except Exception as exc:  # noqa: BLE001 — probe: report and keep frames
            print(f"STREAM EXC {type(exc).__name__}: {exc}")
    if verdict != "ABORT":
        verdict = "ALIVE"
    print(f"VERDICT={verdict} http={http_status} frames={len(frames)} "
          f"assistant_seen={assistant_seen} last_role={last_role}")
    if args.frames_out:
        with open(args.frames_out, "w", encoding="utf-8") as fh:
            for f in frames:
                fh.write(json.dumps(f, ensure_ascii=False) + "\n")
        print(f"FRAMES_FILE={args.frames_out}")
    return 0 if verdict == "ALIVE" else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=None, help="override model slug")
    ap.add_argument("--hints", default=None,
                    help="comma-separated system_hints (replaces list)")
    ap.add_argument("--set", default=None,
                    help='JSON of top-level payload overrides; "meta.KEY" sets '
                         "messages[0].metadata[KEY]")
    ap.add_argument("--frames-out", default=None)
    ap.add_argument("--max-wait", type=int, default=240)
    ap.add_argument("--keep-going", action="store_true",
                    help="do not stop at the first in-band error frame")
    args = ap.parse_args()
    del orig_raise  # probe-local; sse patched above
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
