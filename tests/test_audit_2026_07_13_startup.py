"""Regression tests: a missing ChatGPT token must fail the server start with a
clean, actionable message — not a raw backend traceback.

The most common first-run state is "installed + registered with the MCP client,
but `codex login` / `gpt2agent setup` not run yet". Before this fix, spawning
`gpt2agent run --stdio` in that state dumped a backend.py traceback into the
client's MCP logs. It now exits cleanly like the HTTP-bind refusal does.
"""

from __future__ import annotations

import pytest


def test_missing_token_raises_dedicated_subclass(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)

    from gpt2agent import backend as be

    # Subclass of RuntimeError so existing `except RuntimeError` handlers and
    # `pytest.raises(RuntimeError)` assertions keep working.
    assert issubclass(be.TokenNotFoundError, RuntimeError)
    with pytest.raises(be.TokenNotFoundError, match="No ChatGPT token found"):
        be._load_token_with_source()


def test_run_without_token_exits_cleanly_no_traceback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr("sys.argv", ["gpt2agent", "run", "--stdio"])

    from gpt2agent import server

    with pytest.raises(SystemExit) as exc_info:
        server.main()

    # SystemExit carries the actionable message as its code (a string), so the
    # interpreter prints it to stderr and exits 1 — no traceback.
    assert "No ChatGPT token found" in str(exc_info.value.code)
