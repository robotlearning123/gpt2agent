"""Tests for the ``gpt2agent install`` subcommand.

Verifies the Claude Code and Codex registration logic against synthetic
config files in ``tmp_path``. Never touches the real ``~/.claude.json`` or
``~/.codex/config.toml``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpt2agent.install import (
    SUPPORTED_CLIENTS,
    _remove_toml_section,
    _replace_or_append_toml_section,
    detect_clients,
    install_claude_code,
    install_claude_skill,
    install_codex,
    install_cursor,
    install_windsurf,
    install_zed,
)


# ── Claude Code ────────────────────────────────────────────────────────────


def test_claude_new_config(tmp_path: Path) -> None:
    cfg = tmp_path / "claude.json"
    result = install_claude_code(config_path=cfg)
    assert result["changed"] is True
    assert result["backup"] is None  # no prior file → no backup

    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["gpt2agent"]["type"] == "stdio"
    assert data["mcpServers"]["gpt2agent"]["command"] == "gpt2agent"
    assert data["mcpServers"]["gpt2agent"]["args"] == ["run", "--stdio"]


def test_claude_preserves_other_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "claude.json"
    cfg.write_text(
        json.dumps(
            {
                "numStartups": 42,
                "tipsHistory": {"x": 1},
                "mcpServers": {"existing": {"type": "stdio", "command": "other"}},
            }
        )
    )
    install_claude_code(config_path=cfg)

    data = json.loads(cfg.read_text())
    assert data["numStartups"] == 42
    assert data["tipsHistory"] == {"x": 1}
    assert data["mcpServers"]["existing"]["command"] == "other"
    assert data["mcpServers"]["gpt2agent"]["command"] == "gpt2agent"


def test_claude_writes_backup(tmp_path: Path) -> None:
    cfg = tmp_path / "claude.json"
    cfg.write_text('{"original": true, "mcpServers": {}}')
    result = install_claude_code(config_path=cfg)

    assert result["backup"] is not None
    assert result["backup"].exists()
    assert '"original": true' in result["backup"].read_text()


def test_claude_idempotent(tmp_path: Path) -> None:
    cfg = tmp_path / "claude.json"
    install_claude_code(config_path=cfg)
    snap = cfg.read_text()
    result = install_claude_code(config_path=cfg)
    assert result["changed"] is False
    assert cfg.read_text() == snap


def test_claude_http_transport(tmp_path: Path) -> None:
    cfg = tmp_path / "claude.json"
    install_claude_code(config_path=cfg, transport="http", http_port=9001)
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["gpt2agent"]["type"] == "url"
    assert data["mcpServers"]["gpt2agent"]["url"] == "http://localhost:9001/mcp"


def test_claude_rejects_broken_json(tmp_path: Path) -> None:
    cfg = tmp_path / "claude.json"
    cfg.write_text("{not valid json")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        install_claude_code(config_path=cfg)


def test_claude_migrates_legacy_openai_entry(tmp_path: Path) -> None:
    """Stale 0.0.1 mcpServers.openai pointing at openai-mcp binary is removed."""
    cfg = tmp_path / "claude.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "openai": {
                        "type": "stdio",
                        "command": "openai-mcp",
                        "args": ["run", "--stdio"],
                    },
                    "other": {"type": "stdio", "command": "other-tool"},
                }
            }
        )
    )
    install_claude_code(config_path=cfg)
    data = json.loads(cfg.read_text())
    assert "openai" not in data["mcpServers"], "legacy entry must be removed"
    assert "gpt2agent" in data["mcpServers"]
    assert "other" in data["mcpServers"], "unrelated entries preserved"


def test_claude_keeps_unrelated_openai_entry(tmp_path: Path) -> None:
    """A user-installed mcpServers.openai pointing at something OTHER than the
    old openai-mcp binary is preserved (not our place to mass-clean)."""
    cfg = tmp_path / "claude.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "openai": {
                        "type": "stdio",
                        "command": "some-other-openai-bridge",
                    }
                }
            }
        )
    )
    install_claude_code(config_path=cfg)
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["openai"]["command"] == "some-other-openai-bridge"
    assert "gpt2agent" in data["mcpServers"]


def test_claude_dry_run_no_write(tmp_path: Path) -> None:
    cfg = tmp_path / "claude.json"
    cfg.write_text('{"existing": true}')
    install_claude_code(config_path=cfg, dry_run=True)
    assert cfg.read_text() == '{"existing": true}'


# ── Codex ──────────────────────────────────────────────────────────────────


def test_codex_new_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    install_codex(config_path=cfg)
    text = cfg.read_text()
    assert "[mcp_servers.gpt2agent]" in text
    assert 'command = "gpt2agent"' in text
    assert 'args = ["run", "--stdio"]' in text


def test_codex_preserves_existing_sections(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'approval_policy = "never"\n'
        'model = "gpt-5.5"\n'
        "\n"
        "[agents]\n"
        "max_depth = 2\n"
        "\n"
        "[agents.explorer]\n"
        'config_file = "agents/explorer.toml"\n'
    )
    install_codex(config_path=cfg)
    text = cfg.read_text()
    assert 'approval_policy = "never"' in text
    assert 'model = "gpt-5.5"' in text
    assert "[agents]" in text
    assert "max_depth = 2" in text
    assert "[agents.explorer]" in text
    assert 'config_file = "agents/explorer.toml"' in text
    assert "[mcp_servers.gpt2agent]" in text


def test_codex_replaces_existing_mcp_section(tmp_path: Path) -> None:
    """Re-installing must replace the stale [mcp_servers.gpt2agent] body, not duplicate."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.gpt2agent]\n"
        'command = "OLD_COMMAND"\n'
        'args = ["old", "args"]\n'
        "\n"
        "[other]\n"
        "key = 1\n"
    )
    install_codex(config_path=cfg)
    text = cfg.read_text()
    assert "OLD_COMMAND" not in text
    assert text.count("[mcp_servers.gpt2agent]") == 1
    assert "[other]" in text
    assert "key = 1" in text


def test_codex_idempotent(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    install_codex(config_path=cfg)
    snap = cfg.read_text()
    result = install_codex(config_path=cfg)
    assert result["changed"] is False
    assert cfg.read_text() == snap


def test_codex_migrates_legacy_openai_section(tmp_path: Path) -> None:
    """Stale [mcp_servers.openai] block whose command is the old openai-mcp
    binary gets dropped, since the binary is gone post-rename."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "approval_policy = \"never\"\n"
        "\n"
        "[mcp_servers.openai]\n"
        'command = "openai-mcp"\n'
        'args = ["run", "--stdio"]\n'
        "\n"
        "[other]\n"
        "k = 1\n"
    )
    install_codex(config_path=cfg)
    text = cfg.read_text()
    assert "[mcp_servers.openai]" not in text, "legacy section must be removed"
    assert "[mcp_servers.gpt2agent]" in text
    assert 'approval_policy = "never"' in text
    assert "[other]" in text


def test_codex_keeps_unrelated_openai_section(tmp_path: Path) -> None:
    """An [mcp_servers.openai] block with a command OTHER than openai-mcp
    is left alone (someone wired up a different bridge under that key)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.openai]\n"
        'command = "some-other-bridge"\n'
        'args = []\n'
    )
    install_codex(config_path=cfg)
    text = cfg.read_text()
    assert "[mcp_servers.openai]" in text
    assert 'command = "some-other-bridge"' in text
    assert "[mcp_servers.gpt2agent]" in text


def test_codex_dry_run_no_write(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('approval_policy = "never"\n')
    install_codex(config_path=cfg, dry_run=True)
    assert cfg.read_text() == 'approval_policy = "never"\n'


# ── Skill bundle ───────────────────────────────────────────────────────────


def test_skill_install(tmp_path: Path) -> None:
    """Bundled skill copies into a Claude Code skills directory."""
    skills_root = tmp_path / "claude" / "skills"
    result = install_claude_skill(dst_dir=skills_root)
    # If skill bundle missing from package, test is informational only.
    if result.get("skipped"):
        pytest.skip("skill bundle not present in package")
    # deep-research skill
    dr = skills_root / "deep-research"
    assert dr.exists()
    assert (dr / "SKILL.md").exists()
    assert (dr / "bin" / "run.sh").exists()
    # gpt2agent skill
    ga = skills_root / "gpt2agent"
    assert ga.exists()
    assert (ga / "SKILL.md").exists()
    assert (ga / "tools-reference.md").exists()
    skill = (ga / "SKILL.md").read_text()
    reference = (ga / "tools-reference.md").read_text()
    allowed = [
        line.strip()
        for line in skill.splitlines()
        if line.strip().startswith("- mcp__gpt2agent__")
    ]
    assert len(allowed) == 26
    assert allowed.count("- mcp__gpt2agent__list_voices") == 1
    assert "Complete parameter reference for all 26 MCP tools" in reference
    assert reference.count("### list_voices") == 1
    assert "GPT-Live" in reference


def test_skill_backup_on_overwrite(tmp_path: Path) -> None:
    skills_root = tmp_path / "claude" / "skills"
    first = install_claude_skill(dst_dir=skills_root)
    if first.get("skipped"):
        pytest.skip("skill bundle not present in package")
    # Touch a sentinel file to verify it lands in the backup.
    sentinel = skills_root / "deep-research" / "USER_OVERRIDE.txt"
    sentinel.write_text("user-edited")
    install_claude_skill(dst_dir=skills_root)
    dr_backup = skills_root / "deep-research.bak-gpt2agent"
    assert dr_backup.exists()
    assert (dr_backup / "USER_OVERRIDE.txt").exists()


# ── TOML section editor ────────────────────────────────────────────────────


def test_replace_toml_section_append_when_absent() -> None:
    out = _replace_or_append_toml_section(
        "key = 1\n",
        "mcp_servers.x",
        ["command = \"foo\""],
    )
    assert "key = 1" in out
    assert "[mcp_servers.x]" in out
    assert 'command = "foo"' in out


def test_replace_toml_section_replaces_body() -> None:
    src = (
        "[mcp_servers.x]\n"
        'command = "old"\n'
        'args = ["a"]\n'
        "\n"
        "[other]\n"
        "k = 2\n"
    )
    out = _replace_or_append_toml_section(
        src, "mcp_servers.x", ['command = "new"', 'args = ["b"]']
    )
    assert "old" not in out
    assert '[other]' in out
    assert 'k = 2' in out
    assert out.count("[mcp_servers.x]") == 1


def test_toml_section_editors_preserve_commented_headers() -> None:
    src = (
        "[mcp_servers.x]\n"
        'command = "old"\n'
        "\n"
        "[model] # keep this section\n"
        'name = "gpt"\n'
        "\n"
        "[[profiles]] # keep this array table\n"
        'name = "work"\n'
    )
    replaced = _replace_or_append_toml_section(src, "mcp_servers.x", ['command = "new"'])
    assert 'command = "old"' not in replaced
    assert "[model] # keep this section" in replaced
    assert 'name = "gpt"' in replaced
    assert "[[profiles]] # keep this array table" in replaced
    assert 'name = "work"' in replaced

    removed = _remove_toml_section(src, "mcp_servers.x")
    assert "[mcp_servers.x]" not in removed
    assert "[model] # keep this section" in removed
    assert "[[profiles]] # keep this array table" in removed


def test_toml_section_editors_remove_dotted_child_subtables() -> None:
    """A section's dotted children ([x.env]) are part of the section: removing
    or replacing [mcp_servers.openai] must not orphan [mcp_servers.openai.env]
    — that leaves a command-less half-entry Codex spawn-and-fails on. Siblings
    with a shared name prefix ([mcp_servers.openai2]) are NOT children."""
    src = (
        "[mcp_servers.openai]\n"
        'command = "openai-mcp"\n'
        "\n"
        "[mcp_servers.openai.env]\n"
        'FOO = "bar"\n'
        "\n"
        "[mcp_servers.openai2]\n"
        'command = "keep-me"\n'
    )
    removed = _remove_toml_section(src, "mcp_servers.openai")
    assert "[mcp_servers.openai]" not in removed
    assert "[mcp_servers.openai.env]" not in removed
    assert 'FOO = "bar"' not in removed
    assert "[mcp_servers.openai2]" in removed
    assert 'command = "keep-me"' in removed

    replaced = _replace_or_append_toml_section(
        src, "mcp_servers.openai", ['command = "new"']
    )
    assert 'command = "new"' in replaced
    assert "[mcp_servers.openai.env]" not in replaced  # stale env must not survive
    assert 'FOO = "bar"' not in replaced
    assert "[mcp_servers.openai2]" in replaced
    assert 'command = "keep-me"' in replaced


def test_toml_section_editors_commented_header_with_commented_child() -> None:
    """Commented target header AND commented child subtable together
    ([x] # managed + [x.env] # stale) — replace/remove must drop both
    (cx review 2026-07-02 P2: the two cases were only covered separately)."""
    src = (
        "[mcp_servers.gpt2agent] # managed\n"
        'command = "old"\n'
        "\n"
        "[mcp_servers.gpt2agent.env] # stale\n"
        'KEY = "v"\n'
        "\n"
        "[other]\n"
        "k = 1\n"
    )
    replaced = _replace_or_append_toml_section(
        src, "mcp_servers.gpt2agent", ['command = "new"']
    )
    assert 'command = "old"' not in replaced
    assert ".env" not in replaced
    assert replaced.count("[mcp_servers.gpt2agent]") == 1
    assert "[other]" in replaced

    removed = _remove_toml_section(src, "mcp_servers.gpt2agent")
    assert "mcp_servers.gpt2agent" not in removed
    assert "[other]" in removed


def test_codex_legacy_removal_covers_env_subtable(tmp_path: Path) -> None:
    """End-to-end: install_codex's legacy-openai cleanup must remove the whole
    stale entry even when it carries a [mcp_servers.openai.env] subtable,
    leaving a config that parses with no 'openai' server at all."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.openai]\n"
        'command = "openai-mcp"\n'
        "\n"
        "[mcp_servers.openai.env]\n"
        'FOO = "bar"\n'
    )
    result = install_codex(config_path=cfg)
    assert result["changed"] is True
    parsed = tomllib.loads(cfg.read_text())
    assert "openai" not in parsed.get("mcp_servers", {})
    assert parsed["mcp_servers"]["gpt2agent"]["command"] == "gpt2agent"


def test_toml_section_editors_recognize_commented_headers() -> None:
    """The body-skip boundary regex tolerates '[x] # comment' headers, but the
    TARGET-section find used an exact string match — a pre-existing
    '[mcp_servers.gpt2agent] # comment' was not recognized, so replace APPENDED
    a duplicate table (whole config becomes invalid TOML: 'Cannot declare ...
    twice') and remove was a silent no-op."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore

    src = (
        "[mcp_servers.gpt2agent] # managed by gpt2agent\n"
        'command = "old"\n'
        "\n"
        "[other]\n"
        "x = 1\n"
    )
    replaced = _replace_or_append_toml_section(
        src, "mcp_servers.gpt2agent", ['command = "new"']
    )
    parsed = tomllib.loads(replaced)  # must stay valid TOML — no duplicate table
    assert parsed["mcp_servers"]["gpt2agent"]["command"] == "new"
    assert parsed["other"]["x"] == 1

    removed = _remove_toml_section(src, "mcp_servers.gpt2agent")
    assert "gpt2agent" not in removed
    assert 'command = "old"' not in removed
    assert "[other]" in removed


def test_install_codex_replaces_equivalent_quoted_toml_table(tmp_path: Path) -> None:
    """Quoted TOML key segments are equivalent to their bare-key spelling."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[mcp_servers."gpt2agent"]\n'
        'command = "stale"\n'
        'args = ["old"]\n'
        '\n[mcp_servers."gpt2agent".env]\n'
        'STALE = "1"\n'
        "\n[unrelated]\n"
        "keep = true\n"
    )

    result = install_codex(config_path=cfg)

    assert result["changed"] is True
    parsed = tomllib.loads(cfg.read_text())
    assert parsed["mcp_servers"]["gpt2agent"] == {
        "command": "gpt2agent",
        "args": ["run", "--stdio"],
    }
    assert parsed["unrelated"]["keep"] is True


def test_toml_section_editor_keeps_fully_quoted_dotted_key_distinct() -> None:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore

    src = '["mcp_servers.gpt2agent"]\nkeep = true\n'
    replaced = _replace_or_append_toml_section(
        src,
        "mcp_servers.gpt2agent",
        ['command = "gpt2agent"'],
    )

    parsed = tomllib.loads(replaced)
    assert parsed["mcp_servers.gpt2agent"]["keep"] is True
    assert parsed["mcp_servers"]["gpt2agent"]["command"] == "gpt2agent"


def test_install_codex_removes_quoted_legacy_openai_table(tmp_path: Path) -> None:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[mcp_servers."openai"]\n'
        'command = "openai-mcp"\n'
        '\n[mcp_servers."openai".env]\n'
        'STALE = "1"\n'
    )

    result = install_codex(config_path=cfg)

    assert result["changed"] is True
    parsed = tomllib.loads(cfg.read_text())
    assert "openai" not in parsed.get("mcp_servers", {})
    assert parsed["mcp_servers"]["gpt2agent"]["command"] == "gpt2agent"


def test_install_codex_refuses_invalid_existing_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    original = "[broken\nvalue = 1\n"
    cfg.write_text(original)

    with pytest.raises(RuntimeError, match="not valid TOML.*Refusing to overwrite"):
        install_codex(config_path=cfg)

    assert cfg.read_text() == original
    assert not cfg.with_name("config.toml.bak-gpt2agent").exists()


def test_codex_legacy_removal_covers_commented_header(tmp_path: Path) -> None:
    """_legacy_codex_openai_present detects via tomllib (comment-tolerant), so
    removal must handle '[mcp_servers.openai] # legacy' too — previously the
    installer PRINTED 'removed stale legacy' while the broken entry survived."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.openai] # legacy openai-mcp entry\n"
        'command = "openai-mcp"\n'
    )
    result = install_codex(config_path=cfg)
    assert result["changed"] is True
    parsed = tomllib.loads(cfg.read_text())
    assert "openai" not in parsed.get("mcp_servers", {})
    assert parsed["mcp_servers"]["gpt2agent"]["command"] == "gpt2agent"


def test_install_codex_defaults_to_selected_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_home = tmp_path / "home"
    selected_home = tmp_path / "alternate-codex"
    monkeypatch.setenv("HOME", str(default_home))
    monkeypatch.setenv("CODEX_HOME", str(selected_home))

    result = install_codex()

    assert result["path"] == selected_home / "config.toml"
    assert (selected_home / "config.toml").is_file()
    assert not (default_home / ".codex" / "config.toml").exists()


# ── detect ─────────────────────────────────────────────────────────────────


def test_detect_clients_with_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert detect_clients() == []

    (tmp_path / ".claude").mkdir()
    assert "claude-code" in detect_clients()

    (tmp_path / ".codex").mkdir()
    detected = detect_clients()
    assert "claude-code" in detected
    assert "codex" in detected


def test_detect_clients_uses_selected_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_home = tmp_path / "home"
    selected_home = tmp_path / "alternate-codex"
    default_home.mkdir()
    selected_home.mkdir()
    monkeypatch.setenv("HOME", str(default_home))
    monkeypatch.setenv("CODEX_HOME", str(selected_home))

    assert "codex" in detect_clients()


def test_detect_clients_does_not_fall_back_when_codex_home_is_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_home = tmp_path / "home"
    (default_home / ".codex").mkdir(parents=True)
    selected_home = tmp_path / "missing-alternate-codex"
    monkeypatch.setenv("HOME", str(default_home))
    monkeypatch.setenv("CODEX_HOME", str(selected_home))

    assert "codex" not in detect_clients()


# ── Other MCP hosts (Cursor / Windsurf / Zed) ───────────────────────────────


def test_cursor_new_config(tmp_path: Path) -> None:
    cfg = tmp_path / "cursor.json"
    install_cursor(config_path=cfg)
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["gpt2agent"] == {
        "command": "gpt2agent",
        "args": ["run", "--stdio"],
    }


def test_cursor_preserves_other_servers(tmp_path: Path) -> None:
    cfg = tmp_path / "cursor.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "misc": 1}))
    install_cursor(config_path=cfg)
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["misc"] == 1
    assert "gpt2agent" in data["mcpServers"]


def test_cursor_idempotent_and_backup(tmp_path: Path) -> None:
    cfg = tmp_path / "cursor.json"
    install_cursor(config_path=cfg)
    r2 = install_cursor(config_path=cfg)
    assert r2["changed"] is False
    # second run over an existing file would have produced a backup on change;
    # here it's a no-op so no backup is created
    assert r2["backup"] is None


def test_windsurf_new_config(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp_config.json"
    install_windsurf(config_path=cfg)
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["gpt2agent"]["args"] == ["run", "--stdio"]


def test_zed_uses_context_servers_with_nested_command(tmp_path: Path) -> None:
    cfg = tmp_path / "settings.json"
    install_zed(config_path=cfg)
    data = json.loads(cfg.read_text())
    entry = data["context_servers"]["gpt2agent"]
    assert entry["command"] == {"path": "gpt2agent", "args": ["run", "--stdio"]}
    assert entry["settings"] == {}
    assert "mcpServers" not in data


def test_json_host_rejects_broken_json(tmp_path: Path) -> None:
    cfg = tmp_path / "cursor.json"
    cfg.write_text("{not valid json")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        install_cursor(config_path=cfg)


def test_json_host_dry_run_no_write(tmp_path: Path) -> None:
    cfg = tmp_path / "cursor.json"
    install_cursor(config_path=cfg, dry_run=True)
    assert not cfg.exists()


def test_supported_clients_lists_all_hosts() -> None:
    for name in ("claude-code", "codex", "cursor", "windsurf", "claude-desktop", "zed"):
        assert name in SUPPORTED_CLIENTS
