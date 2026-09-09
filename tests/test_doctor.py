"""Tests for ``gpt2agent doctor`` — all offline, against a fake backend.

Doctor's whole point is honesty: it reports what it actually probed, and says
UNVERIFIED for the tools it deliberately did not exercise. These tests pin that
contract — including "doctor never POSTs", which is what makes it safe to run
against a live account.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from gpt2agent import doctor as doctor_mod
from gpt2agent.backend import UpstreamChallengeError


class FakeClient:
    """Exact-match canned GET responses. Any POST is a test failure."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.gets: list[str] = []
        self.posts: list[str] = []

    def get(self, path: str, target_path: str | None = None, **_: Any) -> Any:
        self.gets.append(path)
        if path not in self.routes:
            raise RuntimeError(f"HTTP 404 for {path}")
        value = self.routes[path]
        if isinstance(value, Exception):
            raise value
        return value

    def post(self, path: str, *_: Any, **__: Any) -> Any:
        self.posts.append(path)
        raise AssertionError("doctor must never POST")


_ROUTES: dict[str, Any] = {
    "/backend-api/models?history_and_training_disabled=false": {"models": [{"slug": "gpt-5-6"}] * 3},
    "/backend-api/me": {"country": "US"},
    "/backend-api/accounts/check/v4-2023-04-27": {
        "accounts": {"acc_1": {"entitlement": {"subscription_plan": "plus"}}}
    },
    "/backend-api/conversations?offset=0&limit=5&order=updated": {
        "items": [{"id": "conv_1"}, {"id": "conv_2"}]
    },
    "/backend-api/conversation/conv_1": {"mapping": {"a": {}, "b": {}, "c": {}}},
    "/backend-api/gizmos/snorlax/sidebar": {"items": [{"gizmo": {"name": "G"}}]},
    "/backend-api/user_system_messages": {"enabled": True},
    "/backend-api/memories": {"memories": [{"id": "m"}, {"id": "m"}]},
    "/backend-api/tasks?limit=1": {"tasks": [{"task_id": "t"}]},
    "/backend-api/codex/environments": {"environments": [{"id": "e"}]},
    "/backend-api/codex/tasks?limit=1": {"items": [{"task": {"id": "t"}}]},
    "/backend-api/apps/list": {"apps": ["connector_x"]},
}


class _GateOk:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(self) -> dict[str, str]:
        return {"chat-requirements": "t", "proof": "p", "turnstile": "tok"}


class _GateBlocked:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(self) -> dict[str, str]:
        raise UpstreamChallengeError(
            "required Turnstile challenge could not be solved.\n"
            "This is a change on ChatGPT's side, not a problem with your token."
        )


def _collect(client: FakeClient) -> list[doctor_mod.Row]:
    return asyncio.run(doctor_mod.collect(client))


def _patch_gate(monkeypatch: pytest.MonkeyPatch, gate) -> None:
    monkeypatch.setattr(doctor_mod, "SentinelGate", gate)


def _row(rows: list[doctor_mod.Row], tool: str) -> doctor_mod.Row:
    matches = [r for r in rows if r.tool == tool]
    assert matches, f"{tool} missing from the doctor report"
    return matches[0]


# ── happy path ───────────────────────────────────────────────────────────────


def test_read_only_probes_report_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, _GateOk)
    rows = _collect(FakeClient(dict(_ROUTES)))

    assert _row(rows, "list_models").result == "OK"
    assert "3 models" in _row(rows, "list_models").detail
    assert _row(rows, "account_status").result == "OK"
    assert _row(rows, "get_conversation").result == "OK"
    assert _row(rows, "list_apps").result == "OK"


def test_doctor_never_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor is read-only by construction — a POST would write or spend quota."""
    _patch_gate(monkeypatch, _GateOk)
    client = FakeClient(dict(_ROUTES))
    asyncio.run(doctor_mod.collect(client))
    assert client.posts == []


def test_probes_use_the_real_tool_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor reports the paths the MCP tools actually call, not approximations."""
    _patch_gate(monkeypatch, _GateOk)
    client = FakeClient(dict(_ROUTES))
    asyncio.run(doctor_mod.collect(client))
    for path in _ROUTES:
        assert path in client.gets, f"doctor never probed {path}"


def test_get_conversation_without_history_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gate(monkeypatch, _GateOk)
    routes = dict(_ROUTES)
    routes["/backend-api/conversations?offset=0&limit=5&order=updated"] = {"items": []}
    rows = _collect(FakeClient(routes))

    row = _row(rows, "get_conversation")
    assert row.result == "OK"
    assert "no conversation" in row.detail


# ── failures ─────────────────────────────────────────────────────────────────


def test_probe_failure_does_not_abort_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gate(monkeypatch, _GateOk)
    routes = dict(_ROUTES)
    routes["/backend-api/memories"] = RuntimeError("HTTP 500 for /backend-api/memories")
    rows = _collect(FakeClient(routes))

    assert _row(rows, "memory_list").result == "FAIL"
    assert _row(rows, "list_models").result == "OK"
    assert _row(rows, "list_apps").result == "OK"


def test_405_is_reported_as_an_upstream_endpoint_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gate(monkeypatch, _GateOk)
    routes = dict(_ROUTES)
    routes["/backend-api/apps/list"] = RuntimeError("HTTP 405 for /backend-api/apps/list")
    rows = _collect(FakeClient(routes))

    row = _row(rows, "list_apps")
    assert row.result == "FAIL"
    assert "405" in row.detail
    assert "endpoint changed upstream" in row.detail


# ── the sentinel gate ────────────────────────────────────────────────────────


def test_blocked_gate_blocks_the_chat_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gate(monkeypatch, _GateBlocked)
    rows = _collect(FakeClient(dict(_ROUTES)))

    assert _row(rows, "sentinel (chat-requirements)").result == "BLOCKED"
    for tool in doctor_mod._GATE_TOOLS:
        assert _row(rows, tool).result == "BLOCKED", f"{tool} should be blocked"
    assert doctor_mod.exit_code(rows) == 1


def test_open_gate_leaves_chat_reported_as_not_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passing gate is not a passing chat tool — doctor must not overclaim."""
    _patch_gate(monkeypatch, _GateOk)
    rows = _collect(FakeClient(dict(_ROUTES)))

    row = _row(rows, "chat")
    assert row.result == "OK"
    assert "not probed" in row.detail
    assert doctor_mod.exit_code(rows) == 0


def test_gate_failure_for_another_reason_is_fail_not_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Gate403:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def get_tokens(self) -> dict[str, str]:
            raise RuntimeError("sentinel/chat-requirements HTTP 403: denied")

    _patch_gate(monkeypatch, _Gate403)
    rows = _collect(FakeClient(dict(_ROUTES)))

    assert _row(rows, "sentinel (chat-requirements)").result == "FAIL"
    assert _row(rows, "chat").result == "FAIL"
    assert doctor_mod.exit_code(rows) == 1


# ── unverified tools ─────────────────────────────────────────────────────────


def test_write_tools_are_reported_unverified_not_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """custom_instructions_set / codex_task_create bypass the gate; doctor
    classifies them from the code instead of mutating the account to test them."""
    _patch_gate(monkeypatch, _GateOk)
    client = FakeClient(dict(_ROUTES))
    rows = asyncio.run(doctor_mod.collect(client))

    instructions = _row(rows, "custom_instructions_set")
    codex = _row(rows, "codex_task_create")
    for row in (instructions, codex):
        assert row.result == "UNVERIFIED"
        assert "not probed" in row.detail
        assert "sentinel gate" in row.detail  # the code-read classification
    # Nothing was written while classifying them.
    assert client.posts == []


def test_unverified_rows_do_not_fail_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, _GateOk)
    rows = _collect(FakeClient(dict(_ROUTES)))

    assert any(r.result == "UNVERIFIED" for r in rows)
    assert doctor_mod.exit_code(rows) == 0


# ── the report itself ────────────────────────────────────────────────────────


def test_table_has_a_row_per_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, _GateBlocked)
    rows = _collect(FakeClient(dict(_ROUTES)))

    table = doctor_mod._format_table(rows)
    assert table.splitlines()[0].split() == ["tool", "result", "detail"]
    for row in rows:
        assert f"{row.tool:<25}" in table


def test_summary_line_counts_every_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, _GateBlocked)
    rows = _collect(FakeClient(dict(_ROUTES)))

    summary = doctor_mod.summarize(rows)
    blocked = sum(1 for r in rows if r.result == "BLOCKED")
    assert summary.startswith("gpt2agent doctor:")
    assert f"{blocked} blocked upstream" in summary
    assert "0 failed" in summary
    assert "unverified" in summary


def test_blocked_note_only_appears_when_something_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gate(monkeypatch, _GateOk)
    ok_rows = _collect(FakeClient(dict(_ROUTES)))
    assert doctor_mod._blocked_note(ok_rows) is None

    _patch_gate(monkeypatch, _GateBlocked)
    blocked_rows = _collect(FakeClient(dict(_ROUTES)))
    note = doctor_mod._blocked_note(blocked_rows)
    assert note is not None
    assert "not a token or configuration problem" in note


# ── entry point ──────────────────────────────────────────────────────────────


def test_run_doctor_without_token_says_so_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpt2agent.backend import TokenNotFoundError

    def _no_token() -> Any:
        raise TokenNotFoundError("No ChatGPT token found — run `codex login`")

    monkeypatch.setattr(doctor_mod, "BackendClient", _no_token)

    lines: list[str] = []
    assert doctor_mod.run_doctor(stream=lines.append) == 2
    text = "\n".join(lines)
    assert "no token" in text
    assert "Nothing was probed" in text
    # No status table: a report with zero probes would read as "all broken".
    assert "list_models" not in text


def test_run_doctor_prints_the_table_and_returns_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_mod, "BackendClient", lambda: FakeClient(dict(_ROUTES)))
    monkeypatch.setattr(doctor_mod, "SentinelGate", _GateOk)

    lines: list[str] = []
    assert doctor_mod.run_doctor(stream=lines.append) == 0

    text = "\n".join(lines)
    assert text.splitlines()[0].startswith("gpt2agent doctor")
    assert "list_models" in text
    assert text.rstrip().splitlines()[-1] == (
        "gpt2agent doctor: 22 OK, 0 failed, 0 blocked upstream, 4 unverified"
    )


def test_cli_doctor_subcommand_wires_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpt2agent import server as server_mod

    monkeypatch.setattr(doctor_mod, "run_doctor", lambda: 3)
    monkeypatch.setattr(sys, "argv", ["gpt2agent", "doctor"])

    with pytest.raises(SystemExit) as excinfo:
        server_mod.main()

    assert excinfo.value.code == 3


def test_get_conversation_is_unverified_when_the_listing_probe_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe whose dependency failed must not report OK.

    ``_p_get_conversation`` reuses the id list that ``_p_list_conversations``
    puts in ``ctx``. When the listing probe raises, ``collect`` records its
    failure and moves on, leaving ``ctx`` untouched — so an "empty means fine"
    reading of the missing key reported a tool as working that was never
    exercised. It has to come back UNVERIFIED instead.
    """
    routes = dict(_ROUTES)
    routes["/backend-api/conversations?offset=0&limit=5&order=updated"] = RuntimeError(
        "HTTP 500 for /backend-api/conversations"
    )
    monkeypatch.setattr(doctor_mod, "SentinelGate", _GateOk)

    rows = asyncio.run(doctor_mod.collect(FakeClient(routes)))
    by_tool = {row.tool: row for row in rows}

    assert by_tool["list_conversations"].result == doctor_mod._FAIL
    assert by_tool["get_conversation"].result == doctor_mod._UNVERIFIED
    assert "list_conversations" in by_tool["get_conversation"].detail
