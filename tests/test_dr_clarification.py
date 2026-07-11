"""Multi-turn clarification handling for light Deep Research.

ChatGPT's research mode often opens with a clarifying question ("Could you
confirm...?") instead of starting research immediately. Before this fix, the
wrapper exited on that first ``done`` and the caller never saw real research.

The wrapper now detects clarification-shaped ``done`` events, captures the
conversation_id + assistant message id, and auto-replies "Proceed with your
best interpretation" in the same conversation thread.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


_CLARIFICATION_TEXT = (
    "Before I begin the deep research, could you confirm whether you'd "
    "like the report to focus on the last 6 months or the last 12 months?"
)
_REAL_REPORT = (
    "# Research Report\n\n"
    + "## Section 1\n\n"
    + "Detailed findings on the requested topic.\n\n" * 20
    + "## Section 2\n\n"
    + "More detailed analysis with multiple paragraphs of content " * 30
)


def _frame_clarification(conv_id: str, msg_id: str) -> list[str]:
    """SSE frames for a clarification turn: short Q ending in '?'."""
    return [
        "data: "
        + json.dumps(
            {
                "conversation_id": conv_id,
                "message": {
                    "id": msg_id,
                    "author": {"role": "assistant"},
                    "content": {
                        "content_type": "text",
                        "parts": [_CLARIFICATION_TEXT],
                    },
                    "status": "finished_successfully",
                    "metadata": {},
                },
            }
        ),
        "data: [DONE]",
    ]


def _frame_real(conv_id: str, msg_id: str) -> list[str]:
    """SSE frames for a real research turn — long content, finished_successfully."""
    return [
        "data: "
        + json.dumps(
            {
                "conversation_id": conv_id,
                "message": {
                    "id": msg_id,
                    "author": {"role": "assistant"},
                    "content": {
                        "content_type": "text",
                        "parts": [_REAL_REPORT],
                    },
                    "status": "finished_successfully",
                    "metadata": {
                        "content_references": [
                            {
                                "items": [
                                    {"url": "https://example.org", "title": "Source"}
                                ]
                            }
                        ],
                    },
                },
            }
        ),
        "data: [DONE]",
    ]


def _frame_finished_text(
    conv_id: str,
    msg_id: str,
    text: str,
    *,
    content_references: list[dict] | None = None,
) -> list[str]:
    """SSE frames for a customizable completed assistant-text message."""
    return [
        "data: "
        + json.dumps(
            {
                "conversation_id": conv_id,
                "message": {
                    "id": msg_id,
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": [text]},
                    "status": "finished_successfully",
                    "metadata": {
                        "content_references": content_references or [],
                    },
                },
            }
        ),
        "data: [DONE]",
    ]


class _FakeResp:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_content(self):
        for ln in self._lines:
            yield (ln + "\n").encode()


class _ScriptedSession:
    """Returns a different SSE frame list on each post() call."""

    _next: list[list[str]] = []

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, *args: Any, **kwargs: Any) -> _FakeResp:
        if not _ScriptedSession._next:
            raise RuntimeError("Test bug: no scripted SSE frames left")
        return _FakeResp(_ScriptedSession._next.pop(0))


class _FakeBackend:
    class _Sess:
        headers: dict[str, str] = {"User-Agent": "test"}

    _session = _Sess()

    def _reload_token_if_stale(self) -> None:  # mirrors BackendClient
        pass

    def request_headers(self) -> dict[str, str]:
        return dict(self._session.headers)


class _FakeSentinel:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(
        self, _operation_headers: dict[str, str] | None = None
    ) -> dict[str, str]:
        return {"chat-requirements": "stub", "proof": "", "turnstile": ""}


def _load_bundled_runner() -> Any:
    path = (
        Path(__file__).resolve().parents[1]
        / "gpt2agent"
        / "skills"
        / "deep-research"
        / "bin"
        / "deep_research.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_dr_clarification_runner", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_bundled_runner(
    monkeypatch: pytest.MonkeyPatch,
    rounds: list[list[str]],
    out_dir: Path,
) -> int:
    """Run the bundled runner through the real light-DR event producer."""
    from gpt2agent import sse as sse_mod

    _ScriptedSession._next = rounds
    monkeypatch.setattr(sse_mod, "AsyncSession", _ScriptedSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _FakeSentinel)

    runner = _load_bundled_runner()
    monkeypatch.setattr(runner, "BackendClient", _FakeBackend)
    return asyncio.run(runner._run("research topic X", "light", out_dir))


def test_clarification_then_real_research(monkeypatch: pytest.MonkeyPatch) -> None:
    """First round = clarification Q; second round = real report. The wrapper
    must auto-reply between them and yield both the clarification meta event
    and the real done event."""
    from gpt2agent import sse as sse_mod

    _ScriptedSession._next = [
        _frame_clarification("conv-1", "msg-clar"),
        _frame_real("conv-1", "msg-real"),
    ]

    monkeypatch.setattr(sse_mod, "AsyncSession", _ScriptedSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _FakeSentinel)

    client = sse_mod.ConversationClient(_FakeBackend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        out: list[dict] = []
        async for ev in client.deep_research("research topic X"):
            out.append(ev)
        return out

    events = asyncio.run(_go())

    # The wrapper should have emitted: done (clar) → clarification_auto_reply → done (real)
    dones = [e for e in events if e.get("type") == "done"]
    autos = [e for e in events if e.get("type") == "clarification_auto_reply"]

    assert len(autos) == 1, f"expected 1 auto-reply event, got: {autos}"
    assert autos[0]["round"] == 1
    assert "could you confirm" in autos[0]["question"].lower()

    assert len(dones) == 2, f"expected 2 done events (clar + real), got {len(dones)}"
    # Last done should be the real report, not the clarification.
    assert dones[-1]["text"] == _REAL_REPORT
    # And it carries citations from metadata.
    assert dones[-1]["content_references"]


def test_clarification_followup_eof_emits_abnormal_terminal_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation that closes without a response must not end silently."""
    from gpt2agent import sse as sse_mod

    _ScriptedSession._next = [
        _frame_clarification("conv-eof", "msg-clar"),
        ["data: [DONE]"],
    ]
    monkeypatch.setattr(sse_mod, "AsyncSession", _ScriptedSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _FakeSentinel)
    client = sse_mod.ConversationClient(_FakeBackend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        events: list[dict] = []
        async for event in client.deep_research("research topic X"):
            events.append(event)
        return events

    events = asyncio.run(_go())
    assert events[-1]["type"] == "done"
    assert events[-1]["terminated_abnormally"] is True
    assert events[-1]["text"] == ""
    assert events[-1]["clarification_followup_incomplete"] is True


def test_unresolved_clarification_at_round_limit_is_abnormal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpt2agent import sse as sse_mod

    _ScriptedSession._next = [_frame_clarification("conv-limit", "msg-clar")]
    monkeypatch.setattr(sse_mod, "AsyncSession", _ScriptedSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _FakeSentinel)
    client = sse_mod.ConversationClient(_FakeBackend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        events: list[dict] = []
        async for event in client.deep_research(
            "research topic X", max_clarification_rounds=0
        ):
            events.append(event)
        return events

    events = asyncio.run(_go())
    assert events[-1]["type"] == "done"
    assert events[-1]["terminated_abnormally"] is True
    assert events[-1]["clarification_unresolved"] is True


def test_long_done_text_is_not_clarification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real research reports must not be auto-retried as if they were clarifications,
    even if they happen to contain a '?' somewhere."""
    from gpt2agent import sse as sse_mod

    # Only one round of frames — if the wrapper wrongly retries, it'll
    # RuntimeError out of scripted frames.
    _ScriptedSession._next = [_frame_real("conv-2", "msg-real-only")]

    monkeypatch.setattr(sse_mod, "AsyncSession", _ScriptedSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _FakeSentinel)

    client = sse_mod.ConversationClient(_FakeBackend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        out: list[dict] = []
        async for ev in client.deep_research("real topic"):
            out.append(ev)
        return out

    events = asyncio.run(_go())
    dones = [e for e in events if e.get("type") == "done"]
    autos = [e for e in events if e.get("type") == "clarification_auto_reply"]

    assert len(dones) == 1
    assert len(autos) == 0
    assert dones[0]["text"] == _REAL_REPORT


def test_clarification_detection_unit() -> None:
    from gpt2agent.sse import _looks_like_clarification

    # Positive — phrase matches
    assert _looks_like_clarification("Could you confirm whether to use 6 or 12 months?")
    assert _looks_like_clarification("Before I begin, please clarify the scope.")
    assert _looks_like_clarification("Just one key clarification on Q4: which year?")

    # Negative — short bare question NOT matching phrase list (was previously
    # caught by the length+"?" heuristic; that branch was dropped because it
    # false-positived on real reports ending with rhetorical questions).
    assert not _looks_like_clarification("Which timeframe?")

    # Negative — long real report ending with a rhetorical question (the
    # exact false-positive class the conservative heuristic now avoids).
    rhetorical = ("Based on the evidence, the answer appears to be X. "
                  * 30 + "But is the timeline really that short?")
    assert not _looks_like_clarification(rhetorical)

    # Negative — long real report
    assert not _looks_like_clarification("Detailed report. " * 200)

    # Negative — long real report whose PROSE contains a hint phrase. Without
    # the length ceiling this false-positived, burning a DR round and letting
    # the auto-proceed follow-up overwrite the real report.
    report_with_phrase = (
        "## Findings\n\nThe data shows X. To make sure the comparison is fair, "
        "both series were normalized to 2020 baselines.\n\n" + "More analysis. " * 120
    )
    assert not _looks_like_clarification(report_with_phrase)
    assert not _looks_like_clarification(
        "Methodology note: before I begin the appendix, caveats apply. " * 40
    )

    # Positive — a genuine multi-question clarification stays under the ceiling.
    multi_q = (
        "Before I begin, could you confirm: 1) which regions to cover, "
        "2) whether to include 2025 data, and 3) the preferred output format?"
    )
    assert _looks_like_clarification(multi_q)

    # Negative — empty
    assert not _looks_like_clarification("")
    assert not _looks_like_clarification("   ")


def test_build_dr_payload_continuation_fields() -> None:
    """conversation_id + parent_message_id propagate into the payload."""
    from gpt2agent.sse import _build_dr_payload

    p = _build_dr_payload("q")
    assert "conversation_id" not in p

    p2 = _build_dr_payload(
        "follow-up", conversation_id="conv-123", parent_message_id="msg-abc"
    )
    assert p2["conversation_id"] == "conv-123"
    assert p2["parent_message_id"] == "msg-abc"


def test_bundled_runner_marks_empty_clarification_followup_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "clarification-followup-eof"

    status = _run_bundled_runner(
        monkeypatch,
        [
            _frame_clarification("conv-runner-eof", "msg-clarification"),
            ["data: [DONE]"],
        ],
        out_dir,
    )

    assert status == 3
    assert (out_dir / "status.txt").read_text(encoding="utf-8").startswith(
        "INCOMPLETE\t"
    )
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert _CLARIFICATION_TEXT not in report

    status_text = (out_dir / "status.txt").read_text(encoding="utf-8")
    assert "events=3" in status_text
    assert "reason=stream terminated abnormally" in status_text
    assert not (out_dir / "events.jsonl").exists()
    assert not (out_dir / "meta.json").exists()


def test_bundled_runner_uses_short_cited_final_after_long_clarification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpt2agent.sse import _looks_like_clarification

    long_question = (
        "Before I begin, could you confirm whether the report should cover "
        "North America, Europe, or both? "
        + "Please specify the regional scope, time horizon, and exclusions. " * 8
    )
    short_final = "# Final report\n\nEvidence supports the concise conclusion."
    stale_url = "https://clarification.invalid/stale"
    final_url = "https://example.org/final-evidence"
    stale_refs = [{"items": [{"url": stale_url, "title": "Stale source"}]}]
    final_refs = [{"items": [{"url": final_url, "title": "Final evidence"}]}]

    assert len(long_question) > len(short_final)
    assert _looks_like_clarification(long_question)
    assert not _looks_like_clarification(short_final)

    out_dir = tmp_path / "clarification-short-final"
    status = _run_bundled_runner(
        monkeypatch,
        [
            _frame_finished_text(
                "conv-runner-final",
                "msg-long-clarification",
                long_question,
                content_references=stale_refs,
            ),
            _frame_finished_text(
                "conv-runner-final",
                "msg-short-final",
                short_final,
                content_references=final_refs,
            ),
        ],
        out_dir,
    )

    assert status == 0
    assert (out_dir / "status.txt").read_text(encoding="utf-8").startswith(
        "DONE\t"
    )
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert short_final in report
    assert long_question not in report
    assert final_url in report
    assert stale_url not in report
