"""Offline regressions for v0.0.10 stream and Deep Research hardening."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import time
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from gpt2agent import sentinel as sentinel_mod
from gpt2agent import sse as sse_mod


class _FrameResponse:
    status_code = 200
    text = ""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Backend:
    class _Session:
        headers: dict[str, str] = {"User-Agent": "test-agent"}

    _session = _Session()

    def _reload_token_if_stale(self) -> None:
        pass

    def post(self, *_: Any, **__: Any) -> dict:
        return {
            "limits_progress": [
                {"feature_name": "deep_research", "remaining": 100}
            ]
        }


class _SentinelStub:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(self) -> dict[str, str]:
        return {"chat-requirements": "stub", "proof": "", "turnstile": ""}


def _patch_sse_frames(
    monkeypatch: pytest.MonkeyPatch, lines: list[str]
) -> None:
    class _FrameSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_FrameSession":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _FrameResponse:
            return _FrameResponse(lines)

    monkeypatch.setattr(sse_mod, "AsyncSession", _FrameSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _SentinelStub)


def _assistant_frame(text: str, status: str) -> str:
    return "data: " + json.dumps(
        {
            "message": {
                "id": "msg-1",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": [text]},
                "status": status,
            }
        }
    )


def test_raw_dump_creates_private_file_under_permissive_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = tmp_path / "raw.jsonl"
    monkeypatch.setenv("GPT2AGENT_RAW_DUMP", str(dump))

    previous_umask = os.umask(0o022)
    try:
        sse_mod._raw_dump({"secret": "value"}, phase="test")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(dump.stat().st_mode) == 0o600


def test_raw_dump_tightens_existing_permissive_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = tmp_path / "raw.jsonl"
    dump.write_text("old\n", encoding="utf-8")
    dump.chmod(0o644)
    monkeypatch.setenv("GPT2AGENT_RAW_DUMP", str(dump))

    sse_mod._raw_dump({"new": True}, phase="test")

    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert len(dump.read_text(encoding="utf-8").splitlines()) == 2


def test_complete_rejects_partial_text_at_raw_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sse_frames(monkeypatch, [_assistant_frame("partial answer", "in_progress")])
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="ended before completion"):
        asyncio.run(
            client.complete(
                "gpt-5-3", [{"role": "user", "content": "question"}]
            )
        )


def test_complete_accepts_explicit_success_at_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sse_frames(
        monkeypatch, [_assistant_frame("complete answer", "finished_successfully")]
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete("gpt-5-3", [{"role": "user", "content": "question"}])
    )

    assert result == "complete answer"


def test_complete_rejects_newer_partial_after_earlier_success_at_raw_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success = json.loads(
        _assistant_frame("earlier complete answer", "finished_successfully")[6:]
    )
    success["message"]["id"] = "msg-success"
    partial = json.loads(_assistant_frame("newer partial answer", "in_progress")[6:])
    partial["message"]["id"] = "msg-partial"
    _patch_sse_frames(
        monkeypatch,
        ["data: " + json.dumps(success), "data: " + json.dumps(partial)],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="ended before completion"):
        asyncio.run(
            client.complete(
                "gpt-5-3", [{"role": "user", "content": "question"}]
            )
        )


def test_light_research_marks_newer_partial_after_done_as_abnormal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    earlier = json.loads(
        _assistant_frame("earlier completed candidate", "finished_successfully")[6:]
    )
    earlier["conversation_id"] = "conv-stale"
    earlier["message"]["id"] = "msg-complete"
    newer = json.loads(_assistant_frame("newer partial report", "in_progress")[6:])
    newer["conversation_id"] = "conv-stale"
    newer["message"]["id"] = "msg-partial"
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps(earlier),
            "data: " + json.dumps(newer),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        events: list[dict] = []
        async for event in client.deep_research("query"):
            events.append(event)
        return events

    events = asyncio.run(_go())
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == "newer partial report"
    assert events[-1]["terminated_abnormally"] is True


def test_light_research_marks_tool_activity_after_done_as_abnormal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    earlier = json.loads(
        _assistant_frame("earlier completed candidate", "finished_successfully")[6:]
    )
    earlier["conversation_id"] = "conv-tool-stale"
    earlier["message"]["id"] = "msg-complete"
    tool = {
        "conversation_id": "conv-tool-stale",
        "message": {
            "id": "tool-response",
            "author": {"role": "tool"},
            "content": {"content_type": "text", "parts": ["tool result"]},
            "status": "finished_successfully",
        },
    }
    _patch_sse_frames(
        monkeypatch,
        [
            "data: " + json.dumps(earlier),
            "data: " + json.dumps(tool),
            "data: [DONE]",
        ],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        events: list[dict] = []
        async for event in client.deep_research("query"):
            events.append(event)
        return events

    events = asyncio.run(_go())
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == ""
    assert events[-1]["terminated_abnormally"] is True


def test_light_research_treats_text_addressed_to_tool_as_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch = {
        "conversation_id": "conv-dispatch",
        "message": {
            "id": "msg-dispatch",
            "author": {"role": "assistant"},
            "recipient": "api_tool.deep_research",
            "content": {
                "content_type": "text",
                "parts": ['{"query": "research topic"}'],
            },
            "status": "finished_successfully",
        },
    }
    _patch_sse_frames(
        monkeypatch,
        ["data: " + json.dumps(dispatch), "data: [DONE]"],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        events: list[dict] = []
        async for event in client.deep_research("query"):
            events.append(event)
        return events

    events = asyncio.run(_go())
    assert events[0] == {"type": "tool", "call": '{"query": "research topic"}'}
    assert events[-1]["type"] == "done"
    assert events[-1]["text"] == ""
    assert events[-1]["terminated_abnormally"] is True


@pytest.mark.parametrize(
    ("recipient", "content_type"),
    [("api_tool.example", "text"), ("all", "thoughts")],
)
def test_complete_rejects_nonterminal_finished_message_at_eof(
    monkeypatch: pytest.MonkeyPatch,
    recipient: str,
    content_type: str,
) -> None:
    frame = json.loads(_assistant_frame("not a final answer", "finished_successfully")[6:])
    frame["message"]["recipient"] = recipient
    frame["message"]["content"]["content_type"] = content_type
    _patch_sse_frames(monkeypatch, ["data: " + json.dumps(frame)])
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="ended before completion"):
        asyncio.run(
            client.complete(
                "gpt-5-3", [{"role": "user", "content": "question"}]
            )
        )


def test_complete_uses_only_verified_poll_recovery_after_raw_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = json.loads(_assistant_frame("partial answer", "in_progress")[6:])
    frame["conversation_id"] = "conv-recovery"
    _patch_sse_frames(monkeypatch, ["data: " + json.dumps(frame)])

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {
                "mapping": {
                    "final": {
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {
                                "content_type": "text",
                                "parts": ["verified final answer"],
                            },
                            "status": "finished_successfully",
                            "create_time": 1,
                        }
                    }
                }
            }

    async def _no_sleep(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(sse_mod.asyncio, "sleep", _no_sleep)
    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete(
            "gpt-5-3",
            [{"role": "user", "content": "question"}],
            poll_async=True,
        )
    )

    assert result == "verified final answer"


def test_tool_call_rejects_partial_text_at_raw_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sse_frames(monkeypatch, [_assistant_frame("partial tool answer", "in_progress")])
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="ended before completion"):
        asyncio.run(client.tool_call("run the tool"))


def test_tool_call_accepts_explicit_success_at_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sse_frames(
        monkeypatch,
        [_assistant_frame("complete tool answer", "finished_successfully")],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(client.tool_call("run the tool"))

    assert result["text"] == "complete tool answer"


def test_tool_call_rejects_partial_summary_after_finished_tool_at_raw_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_response = "data: " + json.dumps(
        {
            "message": {
                "id": "tool-response",
                "author": {"role": "tool"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["tool result"]},
                "status": "finished_successfully",
            }
        }
    )
    partial_summary = json.loads(
        _assistant_frame("partial assistant summary", "in_progress")[6:]
    )
    partial_summary["message"]["id"] = "assistant-summary"
    _patch_sse_frames(
        monkeypatch,
        [tool_response, "data: " + json.dumps(partial_summary)],
    )
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="ended before completion"):
        asyncio.run(client.tool_call("run the tool"))


def _widget_fixture() -> dict:
    return json.loads(
        Path("tests/fixtures/heavy_dr_widget_state.json").read_text(
            encoding="utf-8"
        )
    )["carrier_a_tool_text"]


def _dispatch_detail() -> dict:
    dispatch = (
        '{"path": "/Deep Research App/implicit_link::'
        'connector_openai_deep_research/start", "args": {"query": "test"}}'
    )
    return {
        "mapping": {
            "dispatch": {
                "message": {
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": [dispatch]},
                    "status": "finished_successfully",
                    "create_time": 1,
                    "metadata": {},
                }
            }
        }
    }


def test_phase_two_poll_skips_finished_connector_dispatch() -> None:
    responses = [_dispatch_detail(), _widget_fixture()]

    class _PollingBackend(_Backend):
        def __init__(self) -> None:
            self.get_calls = 0

        def get(self, *_: Any, **__: Any) -> dict:
            response = responses[self.get_calls]
            self.get_calls += 1
            return response

    backend = _PollingBackend()
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        events: list[dict] = []
        async for event in client._poll_dr_completion(
            "conv-1", interval=0, max_wait=1
        ):
            events.append(event)
        return events

    events = asyncio.run(_collect())
    done = [event for event in events if event.get("type") == "done"]

    assert backend.get_calls == 2
    assert len(done) == 1
    assert done[0]["text"].startswith("# Report A")


def test_phase_two_timeout_drops_connector_dispatch_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail = _dispatch_detail()
    dispatch = detail["mapping"]["dispatch"]["message"]["content"]["parts"][0]
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        events: list[dict] = []
        async for event in client._poll_dr_completion(
            "conv-dispatch", seed_text=dispatch, interval=0, max_wait=0
        ):
            events.append(event)
        return events

    events = asyncio.run(_collect())
    runner = _load_runner()
    _patch_runner_events(monkeypatch, runner, events)
    out_dir = tmp_path / "dispatch-timeout"

    status = asyncio.run(runner._run("query", "light", out_dir))
    report = (out_dir / "report.md").read_text(encoding="utf-8")

    assert len(events) == 1
    assert events[0]["type"] == "done"
    assert events[0]["text"] == ""
    assert dispatch not in events[0]["text"]
    assert events[0]["timeout"] is True
    assert events[0]["terminated_abnormally"] is True
    assert status != 0
    assert (out_dir / "status.txt").read_text(encoding="utf-8").startswith(
        "INCOMPLETE\t"
    )
    assert dispatch not in report


def test_phase_two_empty_timeout_reaches_runner_as_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        events: list[dict] = []
        async for event in client._poll_dr_completion(
            "conv-empty", interval=0, max_wait=0
        ):
            events.append(event)
        return events

    events = asyncio.run(_collect())
    runner = _load_runner()
    _patch_runner_events(monkeypatch, runner, events)
    out_dir = tmp_path / "empty-timeout"

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert len(events) == 1
    assert events[0]["type"] == "done"
    assert events[0]["text"] == ""
    assert events[0]["timeout"] is True
    assert events[0]["terminated_abnormally"] is True
    assert status != 0
    assert (out_dir / "status.txt").read_text(encoding="utf-8").startswith(
        "INCOMPLETE\t"
    )


def test_widget_report_rejects_assistant_carrier_and_keeps_tool_fixture() -> None:
    tool_detail = _widget_fixture()
    assistant_detail = deepcopy(tool_detail)
    only_node = next(iter(assistant_detail["mapping"].values()))
    only_node["message"]["author"]["role"] = "assistant"

    assert sse_mod._dr_report_from_widget_state(assistant_detail) == ("", [])

    text, references = sse_mod._dr_report_from_widget_state(tool_detail)
    assert text.startswith("# Report A")
    assert references


def test_phase_two_done_preserves_phase_one_connector_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = [
        "data: "
        + json.dumps(
            {
                "conversation_id": "conv-failed",
                "v": {
                    "message": {
                        "id": "tool-call",
                        "author": {"role": "assistant"},
                        "recipient": "api_tool_chatgpt_deep_research",
                        "content": {"content_type": "text", "parts": ["call"]},
                        "status": "in_progress",
                        "metadata": {},
                    }
                },
            }
        ),
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "tool-error",
                        "author": {"role": "tool"},
                        "recipient": "all",
                        "content": {
                            "content_type": "text",
                            "parts": ["Error: connector unavailable"],
                        },
                        "status": "finished_successfully",
                        "metadata": {},
                    }
                }
            }
        ),
        "data: [DONE]",
    ]
    _patch_sse_frames(monkeypatch, frames)

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return _widget_fixture()

    async def _no_sleep(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(sse_mod.asyncio, "sleep", _no_sleep)
    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    async def _collect() -> list[dict]:
        events: list[dict] = []
        async for event in client.deep_research_heavy("query"):
            events.append(event)
        return events

    events = asyncio.run(_collect())
    assert any(event.get("type") == "tool_error" for event in events)
    done = [event for event in events if event.get("type") == "done"][-1]
    assert done["connector_failed"] is True


class _SentinelBackend:
    class _Session:
        headers: dict[str, str] = {"User-Agent": "test-agent"}

    _session = _Session()


def _patch_sentinel_response(
    monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    class _Response:
        status_code = 200
        text = json.dumps(payload)

        def json(self) -> dict:
            return payload

    class _Session:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _Response:
            return _Response()

    monkeypatch.setattr(sentinel_mod, "AsyncSession", _Session)
    monkeypatch.setattr(sentinel_mod._pow, "get_requirements_token", lambda _: "p")


def test_pow_solver_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sentinel_response(
        monkeypatch,
        {
            "token": "chat-token",
            "proofofwork": {
                "required": True,
                "seed": "seed",
                "difficulty": "difficulty",
            },
        },
    )
    order: list[str] = []

    def _slow_solver(*_: Any) -> str:
        time.sleep(0.30)
        order.append("solver")
        return "proof-token"

    monkeypatch.setattr(sentinel_mod._pow, "solve_pow", _slow_solver)

    async def _heartbeat() -> None:
        await asyncio.sleep(0.05)
        order.append("heartbeat")

    async def _run() -> dict[str, str]:
        gate = sentinel_mod.SentinelGate(_SentinelBackend())  # type: ignore[arg-type]
        result, _ = await asyncio.gather(gate.get_tokens(), _heartbeat())
        return result

    result = asyncio.run(_run())
    assert order[0] == "heartbeat"
    assert result["proof"] == "proof-token"


def test_required_turnstile_without_dx_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sentinel_response(
        monkeypatch,
        {
            "token": "chat-token",
            "proofofwork": {"required": False},
            "turnstile": {"required": True},
        },
    )
    gate = sentinel_mod.SentinelGate(_SentinelBackend())  # type: ignore[arg-type]

    with pytest.raises(
        RuntimeError, match="required Turnstile challenge could not be solved"
    ):
        asyncio.run(gate.get_tokens())


def test_required_pow_unsolved_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sentinel_response(
        monkeypatch,
        {
            "token": "chat-token",
            "proofofwork": {
                "required": True,
                "seed": "seed",
                "difficulty": "difficulty",
            },
        },
    )
    monkeypatch.setattr(sentinel_mod._pow, "solve_pow", lambda *_: None)
    gate = sentinel_mod.SentinelGate(_SentinelBackend())  # type: ignore[arg-type]

    with pytest.raises(
        RuntimeError, match="required POW challenge could not be solved"
    ):
        asyncio.run(gate.get_tokens())


def test_required_turnstile_unsolved_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sentinel_response(
        monkeypatch,
        {
            "token": "chat-token",
            "proofofwork": {"required": False},
            "turnstile": {"required": True, "dx": "challenge"},
        },
    )
    monkeypatch.setattr(sentinel_mod._turn, "solve_turnstile", lambda *_: None)
    gate = sentinel_mod.SentinelGate(_SentinelBackend())  # type: ignore[arg-type]

    with pytest.raises(
        RuntimeError, match="required Turnstile challenge could not be solved"
    ):
        asyncio.run(gate.get_tokens())


def test_concurrent_turnstile_maps_keep_independent_start_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sentinel_mod._turn.time, "time_ns", lambda: 201_000_000_000)
    monkeypatch.setattr(sentinel_mod._turn.random, "random", lambda: 0.0)

    first = sentinel_mod._turn._build_func_map(100.0)
    second = sentinel_mod._turn._build_func_map(200.0)
    for func_map in (first, second):
        func_map[2](30, "window.performance.now")
        func_map[17](31, 30)

    assert first[31] == 101_000.0
    assert second[31] == 1_000.0


def _load_runner() -> ModuleType:
    path = Path("gpt2agent/skills/deep-research/bin/deep_research.py")
    spec = importlib.util.spec_from_file_location("_test_deep_research_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_runner_events(
    monkeypatch: pytest.MonkeyPatch, runner: ModuleType, events: list[dict]
) -> None:
    async def _events():
        for event in events:
            yield event

    class _Conversation:
        def __init__(self, _: object) -> None:
            pass

        def deep_research(self, _: str, **kw):
            return _events()

        def deep_research_heavy(self, _: str, **kw):
            return _events()

    monkeypatch.setattr(runner, "BackendClient", lambda: object())
    monkeypatch.setattr(runner, "ConversationClient", _Conversation)


@pytest.mark.parametrize("preexisting", [False, True], ids=["new", "preexisting"])
def test_bundled_runner_keeps_events_file_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preexisting: bool,
) -> None:
    runner = _load_runner()
    _patch_runner_events(
        monkeypatch,
        runner,
        [{"type": "done", "text": "complete report"}],
    )
    out_dir = tmp_path / "secure-events"
    events_path = out_dir / "events.jsonl"
    if preexisting:
        out_dir.mkdir()
        events_path.write_text("old unredacted data\n", encoding="utf-8")
        events_path.chmod(0o644)

    previous_umask = os.umask(0o022)
    try:
        status = asyncio.run(runner._run("query", "light", out_dir))
    finally:
        os.umask(previous_umask)

    assert status == 0
    assert stat.S_IMODE(events_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("preexisting", [False, True], ids=["new", "preexisting"])
def test_bundled_runner_keeps_all_output_artifacts_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preexisting: bool
) -> None:
    runner = _load_runner()
    _patch_runner_events(
        monkeypatch,
        runner,
        [
            {"type": "meta", "data": {"request_id": "synthetic"}},
            {"type": "done", "text": "complete report"},
        ],
    )
    out_dir = tmp_path / "private-output"
    if preexisting:
        out_dir.mkdir(mode=0o755)
        for name in ("events.jsonl", "report.md", "meta.json", "status.txt"):
            path = out_dir / name
            path.write_text("old data\n", encoding="utf-8")
            path.chmod(0o644)

    previous_umask = os.umask(0)
    try:
        status = asyncio.run(runner._run("query", "light", out_dir))
    finally:
        os.umask(previous_umask)

    assert status == 0
    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    for name in ("events.jsonl", "report.md", "meta.json", "status.txt"):
        assert stat.S_IMODE((out_dir / name).stat().st_mode) == 0o600


def test_bundled_runner_default_output_directories_are_unique_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    monkeypatch.chdir(tmp_path)

    first = runner._default_output_dir()
    second = runner._default_output_dir()

    assert first != second
    assert first.parent == tmp_path / "research"
    assert second.parent == tmp_path / "research"
    assert first.name.startswith("dr-")
    assert stat.S_IMODE(first.stat().st_mode) == 0o700
    assert stat.S_IMODE(second.stat().st_mode) == 0o700


def test_bundled_runner_records_backend_initialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()

    def _fail_backend() -> object:
        raise RuntimeError("invalid saved token")

    monkeypatch.setattr(runner, "BackendClient", _fail_backend)
    out_dir = tmp_path / "backend-error"
    out_dir.mkdir()
    for name in ("events.jsonl", "report.md", "meta.json", "status.txt"):
        path = out_dir / name
        path.write_text(f"stale {name}\n", encoding="utf-8")
        path.chmod(0o644)

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status != 0
    assert (out_dir / "status.txt").read_text(encoding="utf-8").startswith(
        "ERROR\tRuntimeError: invalid saved token"
    )
    assert (out_dir / "events.jsonl").read_text(encoding="utf-8") == ""
    assert (out_dir / "report.md").read_text(encoding="utf-8") == ""
    assert (out_dir / "meta.json").read_text(encoding="utf-8") == "[]\n"
    for name in ("events.jsonl", "report.md", "meta.json", "status.txt"):
        assert stat.S_IMODE((out_dir / name).stat().st_mode) == 0o600


def test_bundled_runner_clears_stale_meta_when_run_emits_no_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    _patch_runner_events(
        monkeypatch,
        runner,
        [{"type": "done", "text": "complete report"}],
    )
    out_dir = tmp_path / "no-meta"
    out_dir.mkdir()
    (out_dir / "meta.json").write_text('{"request_id": "stale"}\n')

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status == 0
    assert (out_dir / "meta.json").read_text(encoding="utf-8") == "[]\n"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW unavailable")
def test_bundled_runner_refuses_symlinked_output_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    _patch_runner_events(
        monkeypatch,
        runner,
        [{"type": "done", "text": "complete report"}],
    )
    out_dir = tmp_path / "symlink-output"
    out_dir.mkdir()
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("do not overwrite\n", encoding="utf-8")
    (out_dir / "report.md").symlink_to(sentinel)

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status == 2
    assert sentinel.read_text(encoding="utf-8") == "do not overwrite\n"
    assert (out_dir / "status.txt").read_text(encoding="utf-8").startswith(
        "ERROR\t"
    )


@pytest.mark.parametrize(
    ("terminal_event", "reason"),
    [
        (
            {
                "type": "done",
                "text": "timeout",
                "terminated_abnormally": True,
                "timeout": True,
            },
            "polling timed out",
        ),
        (
            {
                "type": "done",
                "text": "abnormal",
                "terminated_abnormally": True,
            },
            "stream terminated abnormally",
        ),
    ],
    ids=["timeout", "terminated-abnormally"],
)
def test_bundled_runner_uses_final_terminal_state_independent_of_report_length(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_event: dict,
    reason: str,
) -> None:
    runner = _load_runner()
    long_clean_report = "complete report " * 20
    _patch_runner_events(
        monkeypatch,
        runner,
        [
            {"type": "done", "text": long_clean_report},
            terminal_event,
        ],
    )
    out_dir = tmp_path / "terminal-state"

    status = asyncio.run(runner._run("query", "light", out_dir))
    status_text = (out_dir / "status.txt").read_text(encoding="utf-8")

    assert status != 0
    assert status_text.startswith("INCOMPLETE\t")
    assert f"reason={reason}" in status_text
    assert long_clean_report in (out_dir / "report.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("events", "case"),
    [
        (
            [
                {
                    "type": "done",
                    "text": "partial timeout",
                    "terminated_abnormally": True,
                    "timeout": True,
                }
            ],
            "timeout",
        ),
        (
            [
                {
                    "type": "done",
                    "text": "partial abnormal",
                    "terminated_abnormally": True,
                }
            ],
            "abnormal",
        ),
        ([], "empty"),
    ],
)
def test_bundled_runner_marks_incomplete_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    events: list[dict],
    case: str,
) -> None:
    runner = _load_runner()
    _patch_runner_events(monkeypatch, runner, events)
    out_dir = tmp_path / case

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status != 0
    assert (out_dir / "status.txt").read_text(encoding="utf-8").startswith(
        "INCOMPLETE\t"
    )
    assert "incomplete" in capsys.readouterr().out.lower()


def test_bundled_runner_keeps_clean_done_successful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    _patch_runner_events(
        monkeypatch,
        runner,
        [{"type": "done", "text": "complete report", "content_references": []}],
    )
    out_dir = tmp_path / "complete"

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status == 0
    assert (out_dir / "status.txt").read_text(encoding="utf-8").startswith(
        "DONE\t"
    )


def test_bundled_runner_keeps_longest_of_multiple_clean_done_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    expected = "complete research report " * 20
    _patch_runner_events(
        monkeypatch,
        runner,
        [
            {"type": "done", "text": "short dispatch candidate"},
            {"type": "done", "text": expected},
        ],
    )
    out_dir = tmp_path / "multiple-complete"

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status == 0
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert expected in report
    assert "short dispatch candidate" not in report


def test_bundled_runner_invalidates_clarification_before_followup_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    question = "Before I begin, could you confirm the desired scope?"
    _patch_runner_events(
        monkeypatch,
        runner,
        [
            {"type": "done", "text": question},
            {
                "type": "clarification_auto_reply",
                "round": 1,
                "question": question,
            },
        ],
    )
    out_dir = tmp_path / "clarification-eof"

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status != 0
    assert (out_dir / "status.txt").read_text(encoding="utf-8").startswith(
        "INCOMPLETE\t"
    )


def test_bundled_runner_discards_long_clarification_before_short_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    question = "Before I begin, could you confirm the desired scope? " * 20
    final = "# Final report\n\nShort but complete."
    _patch_runner_events(
        monkeypatch,
        runner,
        [
            {"type": "done", "text": question},
            {
                "type": "clarification_auto_reply",
                "round": 1,
                "question": question,
            },
            {"type": "done", "text": final, "content_references": []},
        ],
    )
    out_dir = tmp_path / "clarification-then-final"

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status == 0
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert final in report
    assert question not in report


def _v1_assistant_envelope(msg_id: str = "msg-a1") -> str:
    return "data: " + json.dumps(
        {
            "v": {
                "message": {
                    "id": msg_id,
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": [""]},
                }
            }
        }
    )


def test_batch_patch_without_p_key_emits_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f/conversation batch patches may omit the top-level `p` key entirely
    (observed live 2026-09-18 on account B) — those frames must not be
    silently dropped."""
    lines = [
        'data: "v1"',
        _v1_assistant_envelope(),
        "data: " + json.dumps(
            {
                "o": "patch",
                "v": [
                    {"p": "/message/content/parts/0", "o": "append", "v": "ACCT-B OK"},
                    {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
                    {"p": "/message/end_turn", "o": "replace", "v": True},
                ],
            }
        ),
        'data: {"type":"message_stream_complete"}',
        "data: [DONE]",
    ]
    _patch_sse_frames(monkeypatch, lines)
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete("gpt-5-6", [{"role": "user", "content": "hi"}])
    )

    assert "ACCT-B OK" in result


def test_account_sharing_degrade_banner_surfaces_in_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation_detail_metadata banner_info (e.g. OpenAI's
    account_sharing_degrade 'Suspicious activity detected') must reach the
    caller — a flagged account degrading to mini models must not be silent."""
    lines = [
        'data: "v1"',
        _v1_assistant_envelope(),
        "data: " + json.dumps(
            {"p": "/message/content/parts/0", "o": "append", "v": "hi"}
        ),
        "data: " + json.dumps(
            {
                "type": "conversation_detail_metadata",
                "banner_info": {
                    "name": "account_sharing_degrade",
                    "title": "Suspicious activity detected",
                    "resets_after": "2026-09-19T21:41:52+00:00",
                },
            }
        ),
        'data: {"type":"message_stream_complete"}',
        "data: [DONE]",
    ]
    _patch_sse_frames(monkeypatch, lines)
    client = sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]

    result = asyncio.run(
        client.complete("gpt-5-6", [{"role": "user", "content": "hi"}])
    )

    assert "hi" in result
    assert "Account note" in result
    assert "Suspicious activity detected" in result
    assert "2026-09-19T21:41:52" in result
