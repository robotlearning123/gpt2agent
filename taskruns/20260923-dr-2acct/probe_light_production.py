#!/usr/bin/env python3
"""Record every frame the FIXED production deep_research loop sees."""
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
    sse._raise_for_sse_error = lambda obj: (frames.append(obj), orig(obj))[1]

    conv = sse.ConversationClient(BackendClient())
    query = (
        "Summarize what changed in Python 3.14 versus 3.13, citing python.org "
        "release notes. Under 150 words. Proceed without clarification."
    )
    events = []
    try:
        async for ev in conv.deep_research(query):
            events.append({k: v for k, v in ev.items() if k != "text"} | {"textlen": len(ev.get("text") or "")})
    except Exception as exc:
        print(f"EXC {type(exc).__name__}: {exc}")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PROD-frames.jsonl")
    with open(out, "w", encoding="utf-8") as fh:
        for f in frames:
            fh.write(json.dumps(f, ensure_ascii=False) + "\n")
    print(f"events={len(events)} types={[e['type'] for e in events]}")
    for e in events:
        print(" ", e)
    print(f"FRAMES={len(frames)} -> {out}")
    # tail trace
    for i, f in enumerate(frames[-10:], start=len(frames) - 10):
        p, o, v = f.get("p"), f.get("o"), f.get("v")
        msg = f.get("message") if isinstance(f.get("message"), dict) else (v.get("message") if isinstance(v, dict) else None)
        role = (msg or {}).get("author", {}).get("role") if isinstance(msg, dict) else None
        desc = f"p={p} o={o} v={str(v)[:60]!r} role={role}"
        print(f"  tail {i}: {desc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
