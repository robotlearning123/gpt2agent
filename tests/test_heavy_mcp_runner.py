"""Offline safety checks for the fresh stdio-MCP heavy-DR runner."""
from __future__ import annotations

import importlib.util
from pathlib import Path


RUNNER = Path(__file__).parents[1] / "tools" / "run_heavy_mcp_once.py"
SPEC = importlib.util.spec_from_file_location("heavy_mcp_runner", RUNNER)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_completed_report_is_accepted() -> None:
    assert module.is_complete_result(
        "# Findings\n\nDSec does X [paper](https://example.org).", False
    )


def test_nonfinal_or_wrapper_failure_is_rejected() -> None:
    for text in (
        "Deep Research has started working on this.",
        "Deep research is starting now.",
        "⚠ Report may be incomplete (completion polling timed out).",
        "⚠ DR connector unavailable — fallback response.",
        "(no response)",
    ):
        assert not module.is_complete_result(text, False), text
    assert module.is_complete_result(
        "# Report\n\nDeep Research has started as a new topic.", False
    )


def test_empty_and_tool_error_are_rejected() -> None:
    assert not module.is_complete_result("", False)
    assert not module.is_complete_result("some text", True)


def test_existing_output_is_detected_before_run(tmp_path: Path) -> None:
    output = tmp_path / "result.md"
    output.write_text("preserve", encoding="utf-8")
    try:
        module.ensure_output_unused(output)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing result path was not refused")
    assert output.read_text(encoding="utf-8") == "preserve"


def test_output_reservation_blocks_duplicate_and_survives_failure(tmp_path: Path) -> None:
    output = tmp_path / "result.md"
    marker = module.reserve_output(output)
    assert marker.exists()
    try:
        module.reserve_output(output)
    except FileExistsError:
        pass
    else:
        raise AssertionError("duplicate output reservation was not refused")
    assert marker.exists(), "ambiguous request marker must remain for inspection"
