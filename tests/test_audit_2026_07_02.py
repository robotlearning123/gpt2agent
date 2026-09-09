"""Regressions for the 2026-07-02 audit fixes.

Covers:
  * `tools/_redact` — _PHONE_RE no longer eats calendar dates ("2026-05-26"
    → "<PHONE>" corrupted every dated memory/task/conversation title).
  * server.py DR tools — `terminated_abnormally`/`timeout` done-flags now
    surface as an explicit "report may be incomplete" note instead of being
    dropped (a truncated report was indistinguishable from a complete one).
  * install._atomic_write — writing through a symlinked config no longer
    replaces the symlink with a plain file (dotfile-repo setups).
  * setup.write_mcp_config — existing ~/.gpt2agent/config.toml is backed up
    before overwrite and the write is atomic; identical content is a no-op.
  * auth._from_browser_use — the session-cookie fallback is gone: a NextAuth
    session cookie saved as access_token 401s on every call.
"""

from __future__ import annotations

import asyncio

from gpt2agent.install import _atomic_write
from gpt2agent.tools._redact import redact

from tests.test_tools import _RecordConv, _build_with_conv  # noqa: F401 (harness)


# ── redact: dates survive, phones still masked ───────────────────────────────


def test_redact_preserves_iso_date() -> None:
    assert redact("released 2026-05-26 as planned") == "released 2026-05-26 as planned"


def test_redact_preserves_iso_datetime() -> None:
    s = "run at 2026-05-26 12:34:56 UTC"
    assert redact(s) == s


def test_redact_preserves_date_range() -> None:
    s = "window 2026-05-26 - 2026-06-27"
    assert redact(s) == s


def test_redact_preserves_dmy_date() -> None:
    s = "due 26-05-2026 latest"
    assert redact(s) == s


def test_redact_still_masks_phone_numbers() -> None:
    assert redact("call +1 (617) 555-0123 now") == "call <PHONE> now"
    assert redact("fax 617-555-0123 ok") == "fax <PHONE> ok"
    assert redact("intl +44 20 7946 0958") == "intl <PHONE>"


def test_redact_masks_phone_after_iso_date_in_same_match() -> None:
    # _PHONE_RE greedily spans "2026-05-26 617-555-0123" as ONE match; the date
    # prefix must survive but the trailing phone must still be masked (cx P1).
    assert redact("appt 2026-05-26 617-555-0123") == "appt 2026-05-26 <PHONE>"


def test_redact_masks_phone_after_dmy_date_in_same_match() -> None:
    assert redact("appt 26-05-2026 617-555-0123") == "appt 26-05-2026 <PHONE>"


def test_redact_preserves_consecutive_dates() -> None:
    s = "between 2026-05-26 2026-06-27 only"
    assert redact(s) == s


# ── DR tools: truncated / timed-out reports are labeled ──────────────────────


def test_deep_research_flags_abnormal_termination(monkeypatch) -> None:
    conv = _RecordConv()
    conv.dr_events = [{"type": "done", "text": "Partial report",
                       "content_references": [], "terminated_abnormally": True}]
    tools = _build_with_conv(monkeypatch, conv)
    out = asyncio.run(tools["deep_research"].fn("topic"))
    assert out.startswith("Partial report")
    assert "Report may be incomplete" in out


def test_deep_research_clean_done_has_no_incomplete_note(monkeypatch) -> None:
    conv = _RecordConv()
    conv.dr_events = [{"type": "done", "text": "Full report", "content_references": []}]
    tools = _build_with_conv(monkeypatch, conv)
    out = asyncio.run(tools["deep_research"].fn("topic"))
    assert "incomplete" not in out


def test_deep_research_heavy_flags_poll_timeout(monkeypatch) -> None:
    conv = _RecordConv()
    conv.heavy_events = [{"type": "done", "text": "Partial report",
                          "content_references": [],
                          "terminated_abnormally": True, "timeout": True}]
    tools = _build_with_conv(monkeypatch, conv)
    out = asyncio.run(tools["deep_research_heavy"].fn("topic"))
    assert "Report may be incomplete" in out
    assert "polling timed out" in out


# ── _atomic_write: symlinked configs write through, not sever ────────────────


def test_atomic_write_preserves_symlink(tmp_path) -> None:
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / ".claude.json"
    link.symlink_to(real)

    _atomic_write(link, '{"a": 1}')

    assert link.is_symlink(), "symlink was replaced by a plain file"
    assert real.read_text() == '{"a": 1}'
    assert link.read_text() == '{"a": 1}'


def test_atomic_write_plain_file_unchanged_behavior(tmp_path) -> None:
    p = tmp_path / "cfg.toml"
    _atomic_write(p, "x = 1\n")
    assert p.read_text() == "x = 1\n"
    assert (p.stat().st_mode & 0o777) == 0o600


# ── setup.write_mcp_config: backup + atomic + idempotent ─────────────────────


def _patched_setup(monkeypatch, tmp_path):
    import gpt2agent.setup as setup_mod

    cfg_path = tmp_path / ".gpt2agent" / "config.toml"
    monkeypatch.setattr(setup_mod, "MCP_CONFIG_PATH", cfg_path)
    return setup_mod, cfg_path


def test_write_mcp_config_creates_file_and_parent(monkeypatch, tmp_path) -> None:
    setup_mod, cfg_path = _patched_setup(monkeypatch, tmp_path)
    setup_mod.write_mcp_config("pro")
    assert 'chat = "gpt-6-pro"' in cfg_path.read_text()


def test_write_mcp_config_backs_up_existing(monkeypatch, tmp_path) -> None:
    setup_mod, cfg_path = _patched_setup(monkeypatch, tmp_path)
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("# user-edited config\n")
    setup_mod.write_mcp_config("plus")
    bak = cfg_path.with_name(cfg_path.name + ".bak-gpt2agent")
    assert bak.read_text() == "# user-edited config\n"
    assert 'chat = "gpt-5-6"' in cfg_path.read_text()


def test_write_mcp_config_same_content_is_noop(monkeypatch, tmp_path) -> None:
    setup_mod, cfg_path = _patched_setup(monkeypatch, tmp_path)
    setup_mod.write_mcp_config("plus")
    before = cfg_path.stat().st_mtime_ns
    setup_mod.write_mcp_config("plus")
    bak = cfg_path.with_name(cfg_path.name + ".bak-gpt2agent")
    assert not bak.exists(), "identical rewrite must not create a backup"
    assert cfg_path.stat().st_mtime_ns == before


# ── auth: no session-cookie-as-access-token fallback ─────────────────────────


def test_browser_use_does_not_return_session_cookie(monkeypatch) -> None:
    import gpt2agent.auth as auth_mod

    class _Result:
        def __init__(self, stdout: str = "", returncode: int = 0) -> None:
            self.stdout = stdout
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
        if "eval" in cmd:
            return _Result(stdout="[]")  # no auth0 localStorage entries
        if "cookies" in cmd:
            raise AssertionError("cookie fallback must not be attempted")
        return _Result()

    monkeypatch.setattr(auth_mod.shutil, "which", lambda name: "/usr/bin/browser-use")
    monkeypatch.setattr(auth_mod.subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(auth_mod.time, "sleep", lambda *a: None)

    assert auth_mod._from_browser_use() is None


def _run_from_browser(monkeypatch, pasted: str):
    import gpt2agent.auth as auth_mod

    monkeypatch.setattr(auth_mod.webbrowser, "open", lambda *a, **k: True)
    monkeypatch.setattr(auth_mod.getpass, "getpass", lambda *a: pasted)
    return auth_mod._from_browser()


def test_from_browser_rejects_non_jwt_paste(monkeypatch) -> None:
    # A NextAuth session cookie is a 5-segment JWE, not a 3-segment JWS access
    # token; saving it just yields 401s later (cx P2) — must be refused.
    assert _run_from_browser(monkeypatch, "eyJa.eyJb.eyJc.eyJd.eyJe") is None
    assert _run_from_browser(monkeypatch, "some-random-cookie-value") is None
    assert _run_from_browser(monkeypatch, "") is None


def test_from_browser_accepts_jwt_shaped_token(monkeypatch) -> None:
    tok = "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"
    assert _run_from_browser(monkeypatch, tok) == {
        "access_token": tok,
        "source": "browser",
    }
