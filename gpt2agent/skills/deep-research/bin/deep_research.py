"""ChatGPT Pro Deep Research runner (calls gpt2agent's ConversationClient directly).

Two modes:
  light  -> conv.deep_research(query)        ~1 min,  citations preserved
  heavy  -> conv.deep_research_heavy(query)  5-30 min, report + citations
            recovered from the connector widget state (gpt2agent >=0.0.4)

Outputs (in --out-dir):
  report.md   final markdown report
  status.txt  shape-only START / DONE / INCOMPLETE / ERROR diagnostics

Raw SSE events and server metadata are deliberately never persisted.

Run with any Python that has gpt2agent installed, e.g.

  python deep_research.py [...]

or via the bundled `run.sh` wrapper, which discovers the right interpreter
from the `gpt2agent` CLI on PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from gpt2agent.backend import BackendClient
from gpt2agent.sse import ConversationClient


def _read_query(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    if arg.startswith("@"):
        return Path(arg[1:]).read_text()
    return arg


def _open_private_text(path: Path):
    """Open *path* for a private UTF-8 rewrite without following a symlink."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except (OSError, AttributeError):
        pass
    try:
        return os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def _write_private_text(path: Path, content: str) -> None:
    with _open_private_text(path) as stream:
        stream.write(content)


def _record_error(status_path: Path, started_at: float, exc: Exception) -> int:
    error_type = type(exc).__name__
    try:
        _write_private_text(
            status_path,
            f"ERROR\terror_type={error_type}\t"
            f"elapsed={time.time() - started_at:.0f}s\n",
        )
    except Exception as status_exc:
        # The status path itself may be the artifact that cannot be written.
        print(
            f"[error] could not update {status_path}: "
            f"{type(status_exc).__name__}: {status_exc}",
            file=sys.stderr,
            flush=True,
        )
    print(f"[error] {error_type}; private exception content omitted", flush=True)
    return 2


def _default_output_dir() -> Path:
    """Atomically create a unique private directory for one research run."""
    root = (Path.cwd() / "research").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(
            prefix=f"dr-{time.strftime('%Y%m%d-%H%M%S')}-",
            dir=root,
        )
    )


async def _run(query: str, mode: str, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        out_dir.chmod(0o700)
    except OSError:
        pass
    report_path = out_dir / "report.md"
    status_path = out_dir / "status.txt"

    t0 = time.time()
    initialization_error: Exception | None = None
    initial_artifacts = (
        (report_path, ""),
        (
            status_path,
            f"START\t{time.strftime('%Y-%m-%d %H:%M:%S')}\tmode={mode}\n",
        ),
    )
    for path, content in initial_artifacts:
        try:
            _write_private_text(path, content)
        except Exception as exc:
            initialization_error = initialization_error or exc
    if initialization_error is not None:
        return _record_error(status_path, t0, initialization_error)

    try:
        backend = BackendClient()
        conv = ConversationClient(backend)
        gen = (
            conv.deep_research_heavy(query)
            if mode == "heavy"
            else conv.deep_research(query)
        )
    except Exception as exc:
        return _record_error(status_path, t0, exc)

    n_events = 0
    seen_first_done = False
    terminal_done_complete = False
    terminal_done_reason = "no completed done event"
    light_final_text = ""
    light_refs: list = []
    light_groups: list = []
    progress_before_done: list[str] = []
    progress_after_done: list[str] = []
    tool_call_count = 0
    meta_event_count = 0
    tool_error_count = 0
    connector_failed = False

    try:
        async for ev in gen:
            n_events += 1
            et = ev.get("type")
            if et == "tool":
                tool_call_count += 1
            elif et == "tool_error":
                tool_error_count += 1
            elif et == "meta":
                meta_event_count += 1
            elif et == "clarification_auto_reply":
                # A clean `done` immediately before this event was only a
                # clarification question, not a completed report. Reset all
                # report/terminal candidates so only the follow-up round can
                # complete the run.
                seen_first_done = False
                terminal_done_complete = False
                terminal_done_reason = "clarification follow-up did not complete"
                light_final_text = ""
                light_refs = []
                light_groups = []
                progress_before_done = []
                progress_after_done = []
            elif et == "done":
                txt = ev.get("text", "") or ""
                if ev.get("connector_failed"):
                    connector_failed = True
                if ev.get("timeout"):
                    terminal_done_complete = False
                    terminal_done_reason = "polling timed out"
                elif ev.get("terminated_abnormally"):
                    terminal_done_complete = False
                    terminal_done_reason = "stream terminated abnormally"
                else:
                    terminal_done_complete = True
                    terminal_done_reason = ""
                # Pick the LONGEST done as the real report. The research model
                # often emits a short tool-dispatch JSON as the FIRST done; the
                # real report is a later, longer done.
                if not seen_first_done or len(txt) > len(light_final_text):
                    light_final_text = txt
                    light_refs = ev.get("content_references", []) or light_refs
                    light_groups = ev.get("search_result_groups", []) or light_groups
                seen_first_done = True
            elif et == "progress":
                txt = ev.get("text", "")
                (progress_after_done if seen_first_done else progress_before_done).append(
                    txt
                )
            if n_events % 100 == 0:
                print(f"[t+{int(time.time() - t0)}s] events={n_events}", flush=True)
    except Exception as exc:
        return _record_error(status_path, t0, exc)

    # --- Build report body ---
    if mode == "heavy":
        body = "".join(progress_after_done)
        if not body and light_final_text:
            body = light_final_text  # Fallback: maybe wrapper actually delivered the real report.
    else:
        body = light_final_text or "".join(progress_before_done + progress_after_done) or "(empty)"

    # --- Sources (light mode only; heavy loses URLs upstream) ---
    sources = ""
    seen: set[str] = set()
    lines: list[str] = []
    for ref in light_refs:
        for item in (ref.get("items") or []):
            url = item.get("url", "")
            title = item.get("title", url)
            if url and url not in seen:
                seen.add(url)
                lines.append(f"- [{title}]({url})")
    if lines:
        sources = "\n\n---\n\n## Sources\n\n" + "\n".join(lines)

    notes_block = ""
    if connector_failed:
        notes_block += "\n\n> **Note:** Deep Research connector reported failure — text may be a fallback answer.\n"
    if tool_error_count:
        notes_block += "\n\n> **Tool error:** One or more tool calls failed.\n"
    if mode == "heavy" and not lines:
        notes_block += "\n\n> **Citations:** No grouped source list in this report's widget state; the model may have written source URLs inline in the body instead.\n"

    header = (
        f"# Deep Research Report\n\n"
        f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- Mode: `{mode}`\n"
        f"- Elapsed: {time.time() - t0:.0f}s\n"
        f"- Events: {n_events} (tool={tool_call_count}, "
        f"meta={meta_event_count}, tool_error={tool_error_count})\n\n"
        f"---\n\n"
    )

    try:
        _write_private_text(report_path, header + body + sources + notes_block)
    except Exception as exc:
        return _record_error(status_path, t0, exc)

    elapsed = time.time() - t0
    if not terminal_done_complete:
        try:
            _write_private_text(
                status_path,
                f"INCOMPLETE\t{time.strftime('%Y-%m-%d %H:%M:%S')}\tmode={mode}\t"
                f"elapsed={elapsed:.0f}s\tevents={n_events}\t"
                f"body_chars={len(body)}\trefs={len(lines)}\t"
                f"tool_calls={tool_call_count}\tmeta_events={meta_event_count}\t"
                f"tool_errors={tool_error_count}\t"
                f"reason={terminal_done_reason}\n",
            )
        except Exception as exc:
            return _record_error(status_path, t0, exc)
        print(
            f"[incomplete] {terminal_done_reason}; inspect {status_path} and "
            f"{report_path}",
            flush=True,
        )
        return 3

    try:
        _write_private_text(
            status_path,
            f"DONE\t{time.strftime('%Y-%m-%d %H:%M:%S')}\tmode={mode}\t"
            f"elapsed={elapsed:.0f}s\tevents={n_events}\t"
            f"body_chars={len(body)}\trefs={len(lines)}\t"
            f"tool_calls={tool_call_count}\tmeta_events={meta_event_count}\t"
            f"tool_errors={tool_error_count}\t"
            f"connector_failed={connector_failed}\n",
        )
    except Exception as exc:
        return _record_error(status_path, t0, exc)
    print(f"[done] mode={mode} elapsed={elapsed:.0f}s body={len(body)} refs={len(lines)} -> {report_path}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("query", help="Inline string, '-' for stdin, or '@path' to read from file")
    p.add_argument("--heavy", action="store_true", help="Use deep_research_heavy (5-30 min)")
    p.add_argument("--out-dir", "-o", default=None, help="Output directory")
    args = p.parse_args()

    query = _read_query(args.query)
    if not query.strip():
        print("error: empty query", file=sys.stderr)
        return 1

    mode = "heavy" if args.heavy else "light"
    out_dir = Path(args.out_dir) if args.out_dir else _default_output_dir()
    return asyncio.run(_run(query, mode, out_dir))


if __name__ == "__main__":
    sys.exit(main())
