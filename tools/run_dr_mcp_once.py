#!/usr/bin/env python3
"""Run one DR request (light or heavy) through a fresh stdio MCP server.

Account follows the invoking shell (CODEX_HOME / ~/.codex fallback).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


LIGHT_QUERY = (
    "Summarize what changed in Python 3.14 versus 3.13, citing python.org "
    "release notes. Under 150 words. Proceed without clarification."
)
HEAVY_QUERY = (
    "Compare the context lengths of Qwen3-4B and Gemma-4-12B-it using their "
    "primary model cards or repos. State two primary links and keep the "
    "report under 250 words. Proceed without clarification."
)
INCOMPLETE_MARKERS = (
    "⚠ report may be incomplete",
    "⚠ dr connector unavailable",
    "(no response)",
)
ACK_PREFIXES = ("deep research has started", "deep research is starting")


def is_complete_result(text: str, is_error: bool) -> bool:
    lowered = text.casefold()
    return bool(text.strip()) and not is_error and not any(
        marker in lowered for marker in INCOMPLETE_MARKERS
    ) and not lowered.lstrip().startswith(ACK_PREFIXES)


def ensure_output_unused(path: Path) -> None:
    reservation = path.with_name(path.name + ".inprogress")
    if path.exists() or reservation.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")


def reserve_output(path: Path) -> Path:
    """Atomically reserve a result path; keep the marker on any failure."""
    ensure_output_unused(path)
    marker = path.with_name(path.name + ".inprogress")
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(
            "A request may have been sent. Inspect provider state before retrying.\n"
        )
    return marker


def make_private_parents(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise FileNotFoundError(f"no existing parent for output: {path}")
        current = parent
    if not current.is_dir():
        raise NotADirectoryError(current)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def write_private_exclusive(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tool", choices=["deep_research", "deep_research_heavy"],
        required=True,
    )
    parser.add_argument(
        "--query-file", type=Path,
        help="UTF-8 file containing the query; defaults to a short built-in probe",
    )
    parser.add_argument(
        "--output", type=Path,
        help="result Markdown path (required; no default overwrite target)",
    )
    parser.add_argument(
        "--timeout", type=int, default=None,
        help="maximum request seconds (light default 420, heavy default 2100)",
    )
    args = parser.parse_args()
    default_timeout = 420 if args.tool == "deep_research" else 2100
    timeout_s = args.timeout or default_timeout
    if not 60 <= timeout_s <= 2400:
        parser.error("--timeout must be between 60 and 2400 seconds")
    if not args.output:
        parser.error("--output is required")
    query = (
        args.query_file.read_text(encoding="utf-8")
        if args.query_file
        else (LIGHT_QUERY if args.tool == "deep_research" else HEAVY_QUERY)
    )
    if not query.strip():
        parser.error("query must not be empty")
    repo = Path(__file__).resolve().parents[1]
    out = args.output.expanduser().resolve()
    make_private_parents(out.parent)
    marker = reserve_output(out)
    child_env = dict(os.environ)
    # Never inherit opt-in raw SSE capture into the child process.
    child_env.pop("GPT2AGENT_RAW_DUMP", None)
    child_env["PYTHONPATH"] = str(repo)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gpt2agent", "run", "--stdio"],
        env=child_env,
        cwd=str(repo),
    )
    started = time.monotonic()

    async def invoke():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(args.tool, {"query": query})

    try:
        response = await asyncio.wait_for(invoke(), timeout=timeout_s)
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - started
        failure = out.with_name(out.stem + ".failed" + out.suffix)
        write_private_exclusive(
            failure,
            f"# MCP {args.tool} attempt timed out\n\n"
            f"- Elapsed: `{elapsed:.1f}s`\n"
            "- Status: ambiguous; the provider may have accepted the request.\n"
            "- Reservation retained: inspect task state before retrying.\n",
        )
        raise RuntimeError(
            f"{args.tool} exceeded {timeout_s}s; inspect {failure} before retrying"
        ) from None
    except Exception as exc:
        elapsed = time.monotonic() - started
        failure = out.with_name(out.stem + ".failed" + out.suffix)
        write_private_exclusive(
            failure,
            f"# MCP {args.tool} attempt failed\n\n"
            f"- Elapsed: `{elapsed:.1f}s`\n"
            "- Status: ambiguous; the provider may have accepted the request.\n"
            "- Reservation retained: inspect task state before retrying.\n"
            f"- Exception type: `{type(exc).__name__}`\n",
        )
        raise
    elapsed = time.monotonic() - started
    texts = [item.text for item in response.content if getattr(item, "text", None)]
    result = "\n\n".join(texts).strip()
    if not is_complete_result(result, bool(response.isError)):
        partial = out.with_name(out.stem + ".incomplete" + out.suffix)
        write_private_exclusive(
            partial,
            f"# Incomplete MCP {args.tool} result\n\n"
            f"- Tool error: `{bool(response.isError)}`\n"
            f"- Elapsed: `{elapsed:.1f}s`\n\n{result}\n",
        )
        print(f"INCOMPLETE_RECEIPT={partial}")
        raise RuntimeError(
            "MCP returned an error, empty result, or explicit incomplete-report "
            "marker; no success artifact written"
        )
    fd = os.open(
        out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(
            f"# Fresh MCP {args.tool} result\n\n"
            f"- Tool error: `{bool(response.isError)}`\n"
            f"- Elapsed: `{elapsed:.1f}s`\n"
            f"- Tool: `{args.tool}`\n\n"
            "## Final tool result\n\n"
            + result
            + "\n"
        )
    marker.unlink()
    print(f"RESULT={out}")
    print(
        f"TOOL_ERROR={bool(response.isError)} ELAPSED_S={elapsed:.1f} "
        f"TEXT_CHARS={len(result)}"
    )
    return 0 if not response.isError else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
