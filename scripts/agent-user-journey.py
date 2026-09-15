#!/usr/bin/env python3
"""Simulated agent-user journey against a gpt2agent MCP server (stdio).

Drives the installed server the way a real agent user would: full tool
discovery, live read-only workflows in a realistic order, handoff modes with
awkward inputs, and fail-closed error paths. Read-only/offline only — no
quota spend, no writes, no browser.

Usage: agent-user-journey.py <abs-path-to-venv-gpt2agent-binary>
Prints one PASS/FAIL line per case plus a RESULT summary; exit 0 iff all pass.
"""
from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CASES: list[str] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    CASES.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def text_of(result) -> str:
    return result.content[0].text if result.content and not result.isError else ""


def payloads(result) -> list:
    """A result may carry MULTIPLE content blocks — e.g. list_conversations
    streams one JSON object per conversation (J4 finding, 2026-09-15)."""
    if result.isError or not result.content:
        return []
    out = []
    for block in result.content:
        try:
            out.append(json.loads(block.text))
        except (ValueError, TypeError):
            pass
    return out


async def journey(binpath: str) -> None:
    params = StdioServerParameters(command=binpath, args=["run", "--stdio"])

    # ── session 1: discovery + live read-only workflows ──────────────────
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            tools = (await s.list_tools()).tools
            report("J1 tool discovery (>=25 tools)", len(tools) >= 25,
                   f"count={len(tools)}")

            r = await s.call_tool("list_models", {})
            # list tools stream ONE JSON object per content block (models:
            # one dict per model); older builds returned a single list —
            # accept both (J2 finding, 2026-09-15)
            def _slug(m):
                return m if isinstance(m, str) else m.get("slug", "")
            entries = [e for p in payloads(r) for e in (p if isinstance(p, list) else [p])]
            report("J2 list_models live (>=20 slugs, any shape)",
                   len(entries) >= 20 and all(_slug(m) for m in entries),
                   f"blocks={len(r.content)} n={len(entries)}")

            r = await s.call_tool("account_status", {})
            st = json.loads(text_of(r))
            report("J3 account_status live",
                   bool(st.get("subscription")) and "expires_at" in st,
                   st.get("subscription", ""))

            r = await s.call_tool("list_conversations", {"limit": 3})
            convs = payloads(r)  # one content block per conversation
            report("J4 list_conversations live (multi-block shape)",
                   len(convs) >= 1 and all(c.get("id") for c in convs),
                   f"n={len(convs)}")
            if not (convs and convs[0].get("id")):
                convs = [{"id": None}]

            r = await s.call_tool("get_conversation",
                                  {"conversation_id": convs[0]["id"]})
            detail = json.loads(text_of(r))
            report("J5 get_conversation real id",
                   bool(detail.get("id") or detail.get("title")
                        or detail.get("mapping") or detail.get("messages")),
                   f"keys={sorted(detail)[:5]}")

            r = await s.call_tool("memory_search", {"query": "project"})
            mem = payloads(r)
            report("J6 memory_search live (memory objects)",
                   len(mem) >= 1 and all(m.get("content") for m in mem
                                         if isinstance(m, dict)) and any(isinstance(m, dict) for m in mem),
                   f"n={len(mem)}")

            r = await s.call_tool("list_custom_gpts", {})
            gpts = payloads(r)  # one block per GPT
            report("J7 list_custom_gpts live (multi-block)",
                   len(gpts) >= 1 and any(
                       g.get("short_url") or g.get("name")
                       for g in gpts if isinstance(g, dict)) and any(isinstance(g, dict) for g in gpts),
                   f"n={len(gpts)}")

            r = await s.call_tool("list_apps", {})
            apps = [e for p in payloads(r) for e in (p if isinstance(p, list) else [p])]
            report("J8 list_apps live (any shape)", len(apps) >= 1,
                   f"n={len(apps)}")

            r = await s.call_tool("list_codex_envs", {})
            envs = [e for p in payloads(r) for e in (p if isinstance(p, list) else [p])]
            report("J9a list_codex_envs live (any shape)", len(envs) >= 1,
                   f"n={len(envs)}")

            r = await s.call_tool("list_codex_tasks", {"limit": 3})
            tasks = [e for p in payloads(r) for e in (p if isinstance(p, list) else [p])]
            report("J9b list_codex_tasks live (any shape)", isinstance(tasks, list),
                   f"n={len(tasks)}")

            # awkward input through the manual handoff (byte-identity shape)
            awkward = "总结这个→✓ 你好 café\nline2 — \"quoted\" 'single' <tag> &amp; " + "x" * 500
            r = await s.call_tool("chat", {"prompt": awkward, "manual": True})
            h = json.loads(text_of(r))
            report("J10 chat manual unicode/long byte-identical",
                   h.get("status") == "manual_handoff" and h.get("prompt") == awkward)

            r = await s.call_tool("gpt_chat", {"gizmo_id": "g/abc123",
                                                "prompt": "hi", "manual": True})
            h = json.loads(text_of(r))
            report("J11 gpt_chat manual g/ normalization",
                   h.get("url") == "https://chatgpt.com/g/abc123")

            r = await s.call_tool("deep_research", {"query": "q", "manual": True})
            h = json.loads(text_of(r))
            report("J12 DR manual carries imperative prefix",
                   h.get("prompt", "").startswith("Begin the deep research"))

            # fail-closed: browser extra not installed in this venv
            r = await s.call_tool("chat", {"prompt": "x", "browser": True})
            body = text_of(r) or (r.content[0].text if r.content else "")
            report("J13 browser=True without extra -> install hint",
                   r.isError and 'gpt2agent[browser]' in body)

    # ── session 2: restart persistence ────────────────────────────────────
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            r = await s.call_tool("account_status", {})
            report("J14 server restart -> fresh session works",
                   bool(json.loads(text_of(r)).get("subscription")))


async def main() -> None:
    binpath = sys.argv[1] if len(sys.argv) > 1 else "gpt2agent"
    await journey(binpath)
    n_pass = sum(CASES)
    print(f"RESULT: {n_pass} passed, {len(CASES) - n_pass} failed")
    sys.exit(0 if n_pass == len(CASES) else 1)


asyncio.run(main())
