"""``gpt2agent doctor`` — a truthful, read-only health report.

Answers one question: *which tools work against the live account right now?*

Every probe here is a read-only GET on the same path the corresponding MCP tool
uses, plus exactly one sentinel probe to classify the challenge state. Doctor
never sends a message, never creates a conversation, and never spends quota —
which is why the write tools are *classified from the code*, not exercised, and
reported as UNVERIFIED rather than pretending they were probed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gpt2agent.backend import BackendClient, TokenNotFoundError, UpstreamChallengeError
from gpt2agent.sentinel import SentinelGate
from gpt2agent.tools._backend import async_get

# Result column values. OK/FAIL come from a real probe; BLOCKED is inferred from
# the sentinel probe (the tool needs the gate, the gate is broken); UNVERIFIED
# means doctor has no safe way to check that tool and says so instead of guessing.
_OK = "OK"
_FAIL = "FAIL"
_BLOCKED = "BLOCKED"
_UNVERIFIED = "UNVERIFIED"

_TOOL_W = 25
_RESULT_W = 10
_DETAIL_W = 88

# Tools that go through ConversationClient -> SentinelGate (sse.py) and are
# therefore all blocked together when the challenge cannot be solved. Doctor
# cannot exercise any of them without sending a real message, so their result is
# a copy of the sentinel row's, not an independent measurement.
_GATE_TOOLS = (
    "chat",
    "agent",
    "gpt_chat",
    "deep_research",
    "deep_research_heavy",
    "generate_image",
    "code_interpreter",
    "canvas_execute",
    "memory_create_via_chat",
)


@dataclass
class Row:
    tool: str
    result: str
    detail: str


def _short(text: str, limit: int = _DETAIL_W) -> str:
    """First line of *text*, truncated to one table row."""
    stripped = (text or "").strip()
    if not stripped:
        return "(no detail)"
    line = stripped.splitlines()[0]
    if len(line) > limit:
        return line[: limit - 1] + "…"
    return line


# ── read-only probes ─────────────────────────────────────────────────────────
#
# Each probe mirrors one MCP tool's GET and returns the detail cell. Probes run
# sequentially on purpose — a burst of parallel requests to the backend looks
# like exactly the abnormal traffic the sentinel gate exists to stop.


async def _p_list_models(client: BackendClient, ctx: dict[str, Any]) -> str:
    data = await async_get(
        client,
        "/backend-api/models?history_and_training_disabled=false",
        target_path="/backend-api/models",
    )
    return f"{len((data or {}).get('models') or [])} models"


async def _p_account_status(client: BackendClient, ctx: dict[str, Any]) -> str:
    me = await async_get(client, "/backend-api/me", target_path="/backend-api/me") or {}
    check = await async_get(
        client,
        "/backend-api/accounts/check/v4-2023-04-27",
        target_path="/backend-api/accounts/check/v4-2023-04-27",
    )
    accounts = (check or {}).get("accounts") or {}
    first = next(iter(accounts.values()), {})
    plan = (first.get("entitlement") or {}).get("subscription_plan")
    return f"{plan or 'unknown'} plan, {me.get('country') or 'unknown country'}"


async def _p_list_conversations(client: BackendClient, ctx: dict[str, Any]) -> str:
    data = await async_get(
        client,
        "/backend-api/conversations?offset=0&limit=5&order=updated",
        target_path="/backend-api/conversations",
    )
    items = (data or {}).get("items") or []
    # Hand the newest conversation id to the get_conversation probe.
    ctx["conversation_ids"] = [c.get("id") for c in items if c.get("id")]
    return f"{len(items)} recent conversations"


class _NotProbed(Exception):
    """Raised by a probe that cannot run because a probe it depends on failed."""


async def _p_get_conversation(client: BackendClient, ctx: dict[str, Any]) -> str:
    if "conversation_ids" not in ctx:
        # list_conversations failed, so its id list was never populated. Saying
        # OK here would report a tool as working that was never exercised.
        raise _NotProbed("depends on list_conversations, which failed above")
    ids = ctx["conversation_ids"]
    if not ids:
        return "no conversation to inspect (account has none)"
    data = await async_get(client, f"/backend-api/conversation/{ids[0]}") or {}
    return f"newest conversation readable ({len(data.get('mapping') or {})} nodes)"


async def _p_list_custom_gpts(client: BackendClient, ctx: dict[str, Any]) -> str:
    data = await async_get(
        client,
        "/backend-api/gizmos/snorlax/sidebar",
        target_path="/backend-api/gizmos/snorlax/sidebar",
    )
    return f"{len((data or {}).get('items') or [])} custom GPTs"


async def _p_custom_instructions_get(
    client: BackendClient, ctx: dict[str, Any]
) -> str:
    ci = await async_get(
        client,
        "/backend-api/user_system_messages",
        target_path="/backend-api/user_system_messages",
    )
    return f"enabled={(ci or {}).get('enabled')!r}"


async def _p_memory_list(client: BackendClient, ctx: dict[str, Any]) -> str:
    data = await async_get(
        client, "/backend-api/memories", target_path="/backend-api/memories"
    )
    return f"{len((data or {}).get('memories') or [])} memories"


async def _p_memory_search(client: BackendClient, ctx: dict[str, Any]) -> str:
    # Same GET as memory_list plus a local filter (tools/memory.py), so it is a
    # real probe of the tool's only network dependency, not an inference.
    data = await async_get(
        client, "/backend-api/memories", target_path="/backend-api/memories"
    )
    return f"searchable over {len((data or {}).get('memories') or [])} memories"


async def _p_list_tasks(client: BackendClient, ctx: dict[str, Any]) -> str:
    data = await async_get(
        client, "/backend-api/tasks?limit=1", target_path="/backend-api/tasks"
    )
    return f"{len((data or {}).get('tasks') or [])} tasks"


async def _p_list_codex_envs(client: BackendClient, ctx: dict[str, Any]) -> str:
    data = await async_get(
        client,
        "/backend-api/codex/environments",
        target_path="/backend-api/codex/environments",
    )
    envs = data if isinstance(data, list) else ((data or {}).get("environments") or [])
    return f"{len(envs)} environments"


async def _p_list_codex_tasks(client: BackendClient, ctx: dict[str, Any]) -> str:
    data = await async_get(
        client, "/backend-api/codex/tasks?limit=1", target_path="/backend-api/codex/tasks"
    )
    return f"{len((data or {}).get('items') or [])} tasks"


async def _p_list_apps(client: BackendClient, ctx: dict[str, Any]) -> str:
    data = await async_get(
        client, "/backend-api/apps/list", target_path="/backend-api/apps/list"
    )
    return f"{len((data or {}).get('apps') or [])} apps/connectors"


# (tool name, probe) in report order.
_PROBES: list[tuple[str, Callable[[BackendClient, dict[str, Any]], Awaitable[str]]]] = [
    ("list_models", _p_list_models),
    ("account_status", _p_account_status),
    ("list_conversations", _p_list_conversations),
    ("get_conversation", _p_get_conversation),
    ("list_custom_gpts", _p_list_custom_gpts),
    ("custom_instructions_get", _p_custom_instructions_get),
    ("memory_list", _p_memory_list),
    ("memory_search", _p_memory_search),
    ("list_tasks", _p_list_tasks),
    ("list_codex_envs", _p_list_codex_envs),
    ("list_codex_tasks", _p_list_codex_tasks),
    ("list_apps", _p_list_apps),
]


# ── sentinel probe ───────────────────────────────────────────────────────────


async def _classify_gate(client: BackendClient) -> tuple[str, str]:
    """Classify the sentinel gate with one real ``chat-requirements`` round trip.

    Reuses ``SentinelGate.get_tokens()`` so doctor reports whatever the actual
    conversation path would hit, rather than a re-implementation that could drift.
    """
    try:
        tokens = await SentinelGate(client).get_tokens()
    except UpstreamChallengeError as exc:
        # Compress the (deliberately verbose) exception into one table cell; the
        # full explanation stays in the exception for MCP client logs.
        first = str(exc).splitlines()[0]
        if "POW" in first:
            detail = "proof-of-work stage no longer solvable — challenge changed upstream"
        elif "no challenge payload" in first:
            detail = "turnstile demanded without a challenge payload — changed upstream"
        else:
            detail = "turnstile required, solver returns no token — changed upstream"
        return _BLOCKED, detail
    except RuntimeError as exc:
        # Gate failed for some other reason (auth, transport, response shape) —
        # that is a probe failure, not a classified upstream breakage.
        return _FAIL, _short(str(exc))
    if tokens.get("turnstile"):
        return _OK, "turnstile solved"
    return _OK, "no turnstile required"


# ── classification from code, not from probing ───────────────────────────────
#
# These tools POST to REST endpoints directly (tools/writes.py) and never touch
# the sentinel gate, so the challenge breakage does not reach them — but doctor
# will not verify that by writing to the account. Files tools need a file_id we
# have no way to discover read-only.


_UNVERIFIED_TOOLS: list[tuple[str, str]] = [
    (
        "custom_instructions_set",
        "plain POST, bypasses the sentinel gate — not probed (would overwrite "
        "your instructions)",
    ),
    (
        "codex_task_create",
        "plain POST, bypasses the sentinel gate — not probed (would spend quota)",
    ),
    ("get_file_info", "needs a file_id — not probed"),
    ("get_file_download_url", "needs a file_id — not probed"),
]


_GATE_DETAIL = {
    _OK: "gate open — conversation not probed (would send a message)",
    _BLOCKED: "upstream challenge — see the sentinel row above",
}


# ── report ───────────────────────────────────────────────────────────────────


async def collect(client: BackendClient) -> list[Row]:
    """Run every probe and return the report rows. One row per tool."""
    rows: list[Row] = []
    ctx: dict[str, Any] = {}

    for tool, probe in _PROBES:
        try:
            detail = await probe(client, ctx)
        except _NotProbed as exc:
            rows.append(Row(tool, _UNVERIFIED, str(exc)))
            continue
        except Exception as exc:  # one failed probe must not kill the whole report
            message = str(exc) or type(exc).__name__
            if "405" in message:
                # Method Not Allowed on a GET that used to be served: the
                # endpoint moved upstream. Retrying or re-logging in won't help.
                rows.append(Row(tool, _FAIL, "HTTP 405 — endpoint changed upstream"))
            else:
                rows.append(Row(tool, _FAIL, _short(message)))
            continue
        rows.append(Row(tool, _OK, detail))

    gate_result, gate_detail = await _classify_gate(client)
    rows.append(Row("sentinel (chat-requirements)", gate_result, gate_detail))
    for tool in _GATE_TOOLS:
        # Not an independent measurement: these all share the one gate probe
        # above, and exercising any of them would send a real message.
        detail = _GATE_DETAIL.get(gate_result, gate_detail)
        rows.append(Row(tool, gate_result, detail))

    rows.extend(Row(tool, _UNVERIFIED, detail) for tool, detail in _UNVERIFIED_TOOLS)
    return rows


def _format_table(rows: list[Row]) -> str:
    lines = [f"{'tool':<{_TOOL_W}} {'result':<{_RESULT_W}} detail"]
    for row in rows:
        lines.append(f"{row.tool:<{_TOOL_W}} {row.result:<{_RESULT_W}} {row.detail}")
    return "\n".join(lines)


def summarize(rows: list[Row]) -> str:
    """One-line summary of the table."""
    n: dict[str, int] = {}
    for row in rows:
        n[row.result] = n.get(row.result, 0) + 1
    parts = [
        f"{n.get(_OK, 0)} OK",
        f"{n.get(_FAIL, 0)} failed",
        f"{n.get(_BLOCKED, 0)} blocked upstream",
        f"{n.get(_UNVERIFIED, 0)} unverified",
    ]
    return "gpt2agent doctor: " + ", ".join(parts)


def _blocked_note(rows: list[Row]) -> str | None:
    """Context line for a blocked gate — the table cell is too short to carry it."""
    if not any(r.result == _BLOCKED for r in rows):
        return None
    return (
        "blocked tools need ChatGPT's sentinel challenge, which changed upstream — "
        "not a token or configuration problem. Read-only tools still work."
    )


def exit_code(rows: list[Row]) -> int:
    """0 when everything doctor could check is healthy, 1 otherwise.

    UNVERIFIED rows are not failures — doctor never claimed to check them.
    """
    return 0 if all(r.result not in (_FAIL, _BLOCKED) for r in rows) else 1


def run_doctor(stream=print) -> int:
    """Entry point for the ``doctor`` subcommand. Returns the process exit code."""
    from gpt2agent import __version__

    stream(f"gpt2agent doctor {__version__} — probing read-only surfaces")
    try:
        client = BackendClient()
    except TokenNotFoundError as exc:
        stream("")
        stream(f"no token: {exc}")
        stream("Nothing was probed. Log in, then run `gpt2agent doctor` again.")
        return 2

    rows = asyncio.run(collect(client))
    stream("")
    stream(_format_table(rows))
    stream("")
    note = _blocked_note(rows)
    if note:
        stream(note)
    stream(summarize(rows))
    return exit_code(rows)
