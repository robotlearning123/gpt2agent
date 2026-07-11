from __future__ import annotations

import asyncio
import importlib.util
import re
import stat
from pathlib import Path
from types import ModuleType

import pytest


_TOKEN_MARKER = "synthetic-token-marker"
_PROMPT_MARKER = "synthetic-prompt-marker"
_RESUME_MARKER = "synthetic-resume-marker"
_ERROR_MARKER = "synthetic-error-marker"
_RESPONSE_MARKER = "synthetic-response-marker"


def _load_runner() -> ModuleType:
    path = Path("gpt2agent/skills/deep-research/bin/deep_research.py")
    spec = importlib.util.spec_from_file_location("_test_safe_deep_research_runner", path)
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

        def deep_research(self, _: str):
            return _events()

        def deep_research_heavy(self, _: str):
            return _events()

    monkeypatch.setattr(runner, "BackendClient", lambda: object())
    monkeypatch.setattr(runner, "ConversationClient", _Conversation)


def test_runner_persists_only_requested_report_and_shape_only_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    _patch_runner_events(
        monkeypatch,
        runner,
        [
            {"type": "meta", "data": {"request_id": _TOKEN_MARKER}},
            {"type": "tool", "call": _PROMPT_MARKER},
            {"type": "tool_error", "message": _ERROR_MARKER},
            {"type": "internal", "resume_token": _RESUME_MARKER},
            {"type": "done", "text": _RESPONSE_MARKER},
        ],
    )
    out_dir = tmp_path / "safe-output"

    status = asyncio.run(runner._run(_PROMPT_MARKER, "light", out_dir))

    assert status == 0
    assert {path.name for path in out_dir.iterdir()} == {"report.md", "status.txt"}
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert _RESPONSE_MARKER in report
    for forbidden in (_TOKEN_MARKER, _PROMPT_MARKER, _RESUME_MARKER, _ERROR_MARKER):
        assert forbidden not in report

    status_text = (out_dir / "status.txt").read_text(encoding="utf-8")
    assert re.fullmatch(
        r"DONE\t\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\tmode=light\t"
        r"elapsed=\d+s\tevents=5\tbody_chars=\d+\trefs=0\ttool_calls=1\t"
        r"meta_events=1\ttool_errors=1\tconnector_failed=False\n",
        status_text,
    )
    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((out_dir / "report.md").stat().st_mode) == 0o600
    assert stat.S_IMODE((out_dir / "status.txt").stat().st_mode) == 0o600


def test_runner_projects_untrusted_citation_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    _patch_runner_events(
        monkeypatch,
        runner,
        [
            {
                "type": "done",
                "text": _RESPONSE_MARKER,
                "content_references": [
                    {
                        "items": [
                            {"title": "script", "url": "javascript:alert(1)"},
                            {
                                "title": "credential",
                                "url": "https://user:secret@example.com/private",
                            },
                            {
                                "title": "metadata",
                                "url": "http://169.254.169.254/latest/meta-data",
                            },
                            {
                                "title": "Safe [document]",
                                "url": (
                                    "https://Example.COM:443/doc?utm_source=private&"
                                    "section=1&token=secret#fragment"
                                ),
                            },
                        ]
                    }
                ],
            }
        ],
    )
    out_dir = tmp_path / "citation-output"

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status == 0
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "- [Safe \\[document\\]](<https://example.com/doc?section=1>)" in report
    for forbidden in (
        "javascript:",
        "user:secret",
        "169.254.169.254",
        "utm_source",
        "token=secret",
        "#fragment",
    ):
        assert forbidden not in report
    assert "\trefs=1\t" in (out_dir / "status.txt").read_text(encoding="utf-8")


def test_runner_error_status_never_persists_exception_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()

    def _fail_backend() -> object:
        raise RuntimeError(_TOKEN_MARKER)

    monkeypatch.setattr(runner, "BackendClient", _fail_backend)
    out_dir = tmp_path / "safe-error"

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status == 2
    assert {path.name for path in out_dir.iterdir()} == {"report.md", "status.txt"}
    for artifact in out_dir.iterdir():
        assert _TOKEN_MARKER not in artifact.read_text(encoding="utf-8")
    assert re.fullmatch(
        r"ERROR\terror_type=RuntimeError\telapsed=\d+s\n",
        (out_dir / "status.txt").read_text(encoding="utf-8"),
    )


def test_runner_incomplete_message_does_not_reference_removed_event_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _load_runner()
    _patch_runner_events(monkeypatch, runner, [])
    out_dir = tmp_path / "incomplete"

    status = asyncio.run(runner._run("query", "light", out_dir))

    assert status == 3
    output = capsys.readouterr().out
    assert "events.jsonl" not in output
    assert str(out_dir / "status.txt") in output
    assert not (out_dir / "events.jsonl").exists()
    assert not (out_dir / "meta.json").exists()
