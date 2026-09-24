#!/usr/bin/env python3
"""Probe one light-DR turn, recording every parsed SSE frame (investigation)."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


async def main() -> int:
    import gpt2agent.sse as sse
    from gpt2agent.backend import BackendClient

    frames = []
    orig = sse._raise_for_sse_error

    def record(obj):
        frames.append(obj)
        return orig(obj)  # keep raising as in production

    sse._raise_for_sse_error = record

    conv = sse.ConversationClient(BackendClient())
    query = (
        "Summarize what changed in Python 3.14 versus 3.13, citing python.org "
        "release notes. Under 150 words. Proceed without clarification."
    )
    events = 0
    try:
        async for event in conv.deep_research(query):
            events += 1
            etype = event.get("type")
            text = str(event.get("text") or event.get("call") or "")[:150]
            print(f"EVENT {events} type={etype} {text}", flush=True)
    except Exception as exc:
        print(f"FAILED after {events} events: {type(exc).__name__}: {exc}")
    finally:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "A-light-frames.jsonl")
        with open(out, "w", encoding="utf-8") as fh:
            for f in frames:
                fh.write(json.dumps(f, ensure_ascii=False) + "\n")
        print(f"FRAMES={len(frames)} -> {out}")
        for i, f in enumerate(frames):
            t = f.get("type")
            err = f.get("error")
            msg = f.get("message")
            role = (msg or {}).get("author", {}).get("role") if isinstance(msg, dict) else None
            ctype = ((msg or {}).get("content") or {}).get("content_type") if isinstance(msg, dict) else None
            status = (msg or {}).get("status") if isinstance(msg, dict) else None
            print(f"  frame {i}: type={t} role={role} ct={ctype} status={status}"
                  + (f" ERROR={json.dumps(err, ensure_ascii=False)[:200]}" if err else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
