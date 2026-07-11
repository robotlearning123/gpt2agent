"""One-command client registration: write gpt2agent into popular MCP-capable
agent config files (Claude Code, Codex, …) plus the Claude Code skill bundle.

Used by ``gpt2agent install --client {claude-code,codex,all}``. Each
register function is idempotent — running twice yields the same final
config — and writes a sibling ``.bak-gpt2agent`` backup of the prior
contents before any change.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

# ── ANSI ──────────────────────────────────────────────────────────────────
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _ok(msg: str) -> None:
    print(f"  {_GREEN}✓{_RESET} {msg}")


def _info(msg: str) -> None:
    print(f"  {_YELLOW}→{_RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"  {_YELLOW}!{_RESET} {msg}")


def _err(msg: str) -> None:
    print(f"  {_RED}✗{_RESET} {msg}", file=sys.stderr)


def _h1(msg: str) -> None:
    print(f"\n{_BOLD}{msg}{_RESET}")


# ── shared ─────────────────────────────────────────────────────────────────


def _codex_home() -> Path:
    """Return the authoritative Codex profile directory for this process."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _backup(path: Path) -> Path | None:
    """Snapshot ``path`` to ``path.bak-gpt2agent`` so the user can undo.

    Preserves the source file's mode. Agent configs (~/.claude.json,
    ~/.codex/config.toml) hold MCP server commands and can hold secrets; a
    plain ``write_bytes`` would create the backup with the process umask
    (often world-readable), exposing a ``0600`` config in its ``.bak`` copy.
    """
    if not path.exists():
        return None
    bak = path.with_name(path.name + ".bak-gpt2agent")
    data = path.read_bytes()
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    # Create at 0o600 (never wider than needed) then match the source mode.
    fd = os.open(str(bak), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            os.chmod(bak, mode)
        except OSError:
            pass  # non-POSIX filesystem
    except BaseException:
        bak.unlink(missing_ok=True)
        raise
    return bak


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a temp file in the same directory.

    Preserves the existing file's mode bits across the rename. For new files,
    uses 0o600 — agent-config files (~/.claude.json, ~/.codex/config.toml)
    contain MCP server commands that the agent will exec, so they should not
    be world-readable on shared systems.
    """
    if path.is_symlink():
        # Users symlink agent configs into dotfile repos; rename() over the
        # symlink would replace the link with a plain file and strand the real
        # target. Write through to the target instead.
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    # Open the temp file at 0o600 up front so a secret-bearing config is never
    # briefly world-readable under a permissive umask between write and chmod.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        try:
            # Preserve an existing file's mode; new files stay 0o600.
            if path.exists():
                os.chmod(tmp, path.stat().st_mode)
        except OSError:
            # Best-effort — Windows / non-POSIX filesystems may reject chmod.
            pass
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _stdio_entry() -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": "gpt2agent",
        "args": ["run", "--stdio"],
    }


def _http_entry(port: int) -> dict[str, Any]:
    return {"type": "url", "url": f"http://localhost:{port}/mcp"}


# ── Claude Code ────────────────────────────────────────────────────────────


def install_claude_code(
    *,
    transport: str = "stdio",
    http_port: int = 9000,
    server_name: str = "gpt2agent",
    dry_run: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Register gpt2agent in ``~/.claude.json`` under ``mcpServers.<server_name>``.

    Preserves all other fields of the existing config (the file is large and
    holds unrelated state — tips history, conversation tracking, etc.).
    """
    if transport not in {"stdio", "http"}:
        raise ValueError("transport must be 'stdio' or 'http'")

    cfg_path = config_path or Path.home() / ".claude.json"

    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text())
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{cfg_path} is not valid JSON ({exc}). Refusing to overwrite — "
                "open it in an editor and either repair or move it aside."
            )
    else:
        data = {}

    servers = data.setdefault("mcpServers", {})
    entry = _stdio_entry() if transport == "stdio" else _http_entry(http_port)
    prior = servers.get(server_name)
    servers[server_name] = entry

    # Migrate stale 0.0.1-era "openai" entry that points at the old binary.
    # The openai-mcp pipx app is gone post-rename, so a leftover entry is a
    # broken pointer Claude Code will spawn-and-fail on every restart.
    legacy = servers.get("openai")
    legacy_removed = False
    if (
        server_name != "openai"
        and isinstance(legacy, dict)
        and legacy.get("command") == "openai-mcp"
    ):
        del servers["openai"]
        legacy_removed = True

    if prior == entry and not legacy_removed:
        _info(f"claude-code: {cfg_path} already has {server_name!r} entry (no-op)")
        return {"path": cfg_path, "backup": None, "changed": False}

    if dry_run:
        _info(f"claude-code: would update {cfg_path} mcpServers.{server_name}")
        if legacy_removed:
            _info("claude-code: would also drop stale legacy 'openai' entry")
        return {"path": cfg_path, "backup": None, "changed": False}

    backup = _backup(cfg_path)
    _atomic_write(cfg_path, json.dumps(data, indent=2) + "\n")
    _ok(
        f"claude-code: wrote mcpServers.{server_name} to {cfg_path} "
        f"(backup: {backup.name if backup else 'none'})"
    )
    if legacy_removed:
        _ok("claude-code: removed stale legacy 'openai' entry (pointed at gone openai-mcp binary)")
    return {"path": cfg_path, "backup": backup, "changed": True}


# ── Codex CLI ──────────────────────────────────────────────────────────────


def _toml_quote(value: str) -> str:
    """Quote a string for TOML basic-string form."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# Match both standard table headers `[x]` and array-of-table headers `[[x]]`.
# Missing the `[[...]]` form made the body-skip loop swallow a following
# array-of-table section as if it were part of the replaced/removed block,
# deleting unrelated valid TOML.
_SECTION_HEADER_RE = re.compile(r"^\s*\[\[?([^\[\]\n]+)\]\]?\s*(?:#.*)?$")


def _toml_header_path(header_line: str) -> tuple[str, ...] | None:
    """Return the semantic key path declared by a TOML table header.

    TOML permits quoted key segments, so ``[a.b]`` and ``[a."b"]`` declare
    the same path while ``["a.b"]`` declares a different, single-segment key.
    Let the standard parser resolve those distinctions instead of comparing
    the raw header text.
    """
    match = _SECTION_HEADER_RE.match(header_line)
    if match is None:
        return None

    is_array = header_line.lstrip().startswith("[[")
    opening, closing = ("[[", "]]") if is_array else ("[", "]")
    marker = "__gpt2agent_header_marker__"
    synthetic = f"{opening}{match.group(1).strip()}{closing}\n{marker} = true\n"
    try:
        parsed = tomllib.loads(synthetic)
    except (TypeError, ValueError):
        return None

    def _find_marker(node: object, path: tuple[str, ...]) -> tuple[str, ...] | None:
        if isinstance(node, dict):
            if node.get(marker) is True:
                return path
            for key, value in node.items():
                found = _find_marker(value, (*path, str(key)))
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = _find_marker(value, path)
                if found is not None:
                    return found
        return None

    return _find_marker(parsed, ())


def _toml_section_path(section_name: str) -> tuple[str, ...]:
    path = _toml_header_path(f"[{section_name}]")
    if path is None:
        raise ValueError(f"invalid TOML section name: {section_name!r}")
    return path


def _belongs_to_section(header_line: str, section_name: str) -> bool:
    """True iff *header_line* is ``[section_name]`` itself or a dotted child
    (``[section_name.env]``, ``[[section_name.x]]``). Child subtables are part
    of the section for TOML semantics — removing/replacing the parent while
    leaving a child behind resurrects the section as a headless half-entry.
    """
    header_path = _toml_header_path(header_line)
    if header_path is None:
        return False
    section_path = _toml_section_path(section_name)
    return header_path[: len(section_path)] == section_path


def _is_target_header(line: str, section_name: str) -> bool:
    """True iff *line* is the ``[section_name]`` table header itself, tolerating
    the same whitespace / trailing-comment forms ``_SECTION_HEADER_RE`` accepts
    as section boundaries. An exact string compare here misses
    ``[section] # comment`` — replace then appends a DUPLICATE table (invalid
    TOML: "Cannot declare ... twice") and remove becomes a silent no-op.
    """
    if line.lstrip().startswith("[["):  # array-of-table is never the target
        return False
    return _toml_header_path(line) == _toml_section_path(section_name)


def _remove_toml_section(content: str, section_name: str) -> str:
    """Remove the ``[section_name]`` block from TOML content. No-op if absent.

    A section runs from its ``[header]`` line up to the next *unrelated*
    ``[header]`` or end-of-file; dotted child subtables are removed with it.
    Other sections are preserved verbatim.
    """
    out: list[str] = []
    skipping = False
    for line in content.splitlines():
        if _is_target_header(line, section_name):
            skipping = True
            continue
        if skipping:
            if _SECTION_HEADER_RE.match(line) and not _belongs_to_section(line, section_name):
                skipping = False
                # fall through to keep this header line
            else:
                continue
        out.append(line)
    return "\n".join(out).rstrip() + "\n" if out else ""


def _legacy_codex_openai_present(content: str) -> bool:
    """True iff content has a ``[mcp_servers.openai]`` section whose
    ``command`` is the renamed-away ``openai-mcp`` binary.
    """
    try:
        parsed = tomllib.loads(content)
    except Exception:
        return False
    legacy = (parsed.get("mcp_servers") or {}).get("openai") or {}
    return legacy.get("command") == "openai-mcp"


def _replace_or_append_toml_section(
    content: str,
    section_name: str,
    new_section_lines: list[str],
) -> str:
    """Replace ``[section_name]`` block in ``content`` or append it.

    A section runs from its ``[header]`` line up to the next *unrelated*
    ``[header]`` or end-of-file; dotted child subtables of the old block are
    replaced with it (stale ``[section.env]`` must not outlive a replace).
    Other sections are preserved verbatim. The new block is written as
    ``[section_name]`` followed by ``new_section_lines``.
    """
    header = f"[{section_name}]"
    new_block = "\n".join([header, *new_section_lines]).strip("\n")

    lines = content.splitlines()
    out: list[str] = []
    i = 0
    found = False

    while i < len(lines):
        line = lines[i]
        if _is_target_header(line, section_name):
            found = True
            out.append(new_block)
            i += 1
            # Skip prior body (incl. dotted child subtables) until the next
            # unrelated section header or EOF.
            while i < len(lines) and (
                not _SECTION_HEADER_RE.match(lines[i])
                or _belongs_to_section(lines[i], section_name)
            ):
                i += 1
            continue
        out.append(line)
        i += 1

    if not found:
        trailing_blank = bool(out) and out[-1].strip() == ""
        if not trailing_blank and out:
            out.append("")
        out.append(new_block)

    result = "\n".join(out).rstrip() + "\n"
    return result


def install_codex(
    *,
    server_name: str = "gpt2agent",
    dry_run: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Register gpt2agent in the selected Codex home's ``config.toml``.

    Preserves all other sections (codex agent definitions, model config, …).
    """
    cfg_path = config_path or _codex_home() / "config.toml"
    existing = cfg_path.read_text() if cfg_path.exists() else ""
    if existing:
        try:
            tomllib.loads(existing)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{cfg_path} is not valid TOML ({exc}). Refusing to overwrite — "
                "open it in an editor and either repair or move it aside."
            ) from exc

    section_name = f"mcp_servers.{server_name}"
    new_section = [
        f'command = {_toml_quote("gpt2agent")}',
        'args = ["run", "--stdio"]',
    ]
    new_content = _replace_or_append_toml_section(existing, section_name, new_section)

    # Drop a stale 0.0.1 [mcp_servers.openai] block whose command is the
    # gone openai-mcp binary — Codex would spawn-and-fail on every invocation.
    legacy_removed = False
    if section_name != "mcp_servers.openai" and _legacy_codex_openai_present(new_content):
        new_content = _remove_toml_section(new_content, "mcp_servers.openai")
        legacy_removed = True

    try:
        tomllib.loads(new_content)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Generated Codex config for {cfg_path} is not valid TOML ({exc}). "
            "Refusing to overwrite the existing file."
        ) from exc

    if new_content == existing:
        _info(f"codex: {cfg_path} already has [{section_name}] (no-op)")
        return {"path": cfg_path, "backup": None, "changed": False}

    if dry_run:
        _info(f"codex: would write [{section_name}] to {cfg_path}")
        if legacy_removed:
            _info("codex: would also drop stale legacy [mcp_servers.openai] block")
        return {"path": cfg_path, "backup": None, "changed": False}

    backup = _backup(cfg_path)
    _atomic_write(cfg_path, new_content)
    _ok(
        f"codex: wrote [{section_name}] to {cfg_path} "
        f"(backup: {backup.name if backup else 'none'})"
    )
    if legacy_removed:
        _ok("codex: removed stale legacy [mcp_servers.openai] block")
    return {"path": cfg_path, "backup": backup, "changed": True}


# ── Other MCP hosts (JSON, mcpServers-style) ───────────────────────────────
#
# Cursor, Windsurf, Claude Desktop and friends all use the same
# ``{"<top_key>": {"<server>": {command, args}}}`` JSON shape Claude Code uses —
# only the file path (and, for VS Code/Zed, the top-level key + entry shape)
# differ. ``_install_json_host`` is the shared, idempotent writer; the per-host
# functions just supply path + key + entry.


def _mcp_entry() -> dict[str, Any]:
    """stdio server entry for the mcpServers-style hosts (Cursor/Windsurf/…)."""
    return {"command": "gpt2agent", "args": ["run", "--stdio"]}


def _zed_entry() -> dict[str, Any]:
    """Zed nests the command and uses a ``context_servers`` top-level key."""
    return {"command": {"path": "gpt2agent", "args": ["run", "--stdio"]}, "settings": {}}


def _install_json_host(
    label: str,
    cfg_path: Path,
    *,
    top_key: str,
    entry: dict[str, Any],
    server_name: str = "gpt2agent",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Register gpt2agent in a JSON MCP config file under ``top_key.<server>``.

    Idempotent; preserves every other field; writes a ``.bak-gpt2agent`` backup.
    Used for Cursor, Windsurf, Claude Desktop, and Zed (see callers). VS Code and
    Cline use the same shape but are documented as manual setup (docs/clients.md).
    """
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{cfg_path} is not valid JSON ({exc}). Refusing to overwrite — "
                "repair it or move it aside, then re-run."
            )
        if not isinstance(data, dict):
            raise RuntimeError(f"{cfg_path} is not a JSON object; refusing to overwrite.")
    else:
        data = {}

    servers = data.setdefault(top_key, {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"{cfg_path}: '{top_key}' is not an object; refusing to overwrite.")
    prior = servers.get(server_name)
    servers[server_name] = entry

    if prior == entry:
        _info(f"{label}: {cfg_path} already has {top_key}.{server_name} (no-op)")
        return {"path": cfg_path, "backup": None, "changed": False}
    if dry_run:
        _info(f"{label}: would write {top_key}.{server_name} to {cfg_path}")
        return {"path": cfg_path, "backup": None, "changed": False}

    backup = _backup(cfg_path)
    _atomic_write(cfg_path, json.dumps(data, indent=2) + "\n")
    _ok(f"{label}: wrote {top_key}.{server_name} to {cfg_path} "
        f"(backup: {backup.name if backup else 'none'})")
    return {"path": cfg_path, "backup": backup, "changed": True}


def install_cursor(*, server_name: str = "gpt2agent", dry_run: bool = False,
                   config_path: Path | None = None) -> dict[str, Any]:
    """Register gpt2agent in Cursor's ``~/.cursor/mcp.json`` (``mcpServers``)."""
    cfg = config_path or Path.home() / ".cursor" / "mcp.json"
    return _install_json_host("cursor", cfg, top_key="mcpServers", entry=_mcp_entry(),
                              server_name=server_name, dry_run=dry_run)


def install_windsurf(*, server_name: str = "gpt2agent", dry_run: bool = False,
                     config_path: Path | None = None) -> dict[str, Any]:
    """Register gpt2agent in Windsurf's ``~/.codeium/windsurf/mcp_config.json``."""
    cfg = config_path or Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    return _install_json_host("windsurf", cfg, top_key="mcpServers", entry=_mcp_entry(),
                              server_name=server_name, dry_run=dry_run)


def install_claude_desktop(*, server_name: str = "gpt2agent", dry_run: bool = False,
                           config_path: Path | None = None) -> dict[str, Any]:
    """Register gpt2agent in the Claude Desktop config (``mcpServers``)."""
    cfg = config_path or _claude_desktop_config_path()
    return _install_json_host("claude-desktop", cfg, top_key="mcpServers", entry=_mcp_entry(),
                              server_name=server_name, dry_run=dry_run)


def install_zed(*, server_name: str = "gpt2agent", dry_run: bool = False,
                config_path: Path | None = None) -> dict[str, Any]:
    """Register gpt2agent in Zed's ``~/.config/zed/settings.json`` (``context_servers``)."""
    cfg = config_path or Path.home() / ".config" / "zed" / "settings.json"
    return _install_json_host("zed", cfg, top_key="context_servers", entry=_zed_entry(),
                              server_name=server_name, dry_run=dry_run)


def _claude_desktop_config_path() -> Path:
    """Platform-specific Claude Desktop config path."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


# Registry of the JSON hosts beyond claude-code/codex: name → (installer, detect path).
_EXTRA_HOSTS: dict[str, Any] = {
    "cursor": (install_cursor, lambda: Path.home() / ".cursor"),
    "windsurf": (install_windsurf, lambda: Path.home() / ".codeium" / "windsurf"),
    "claude-desktop": (install_claude_desktop, lambda: _claude_desktop_config_path().parent),
    "zed": (install_zed, lambda: Path.home() / ".config" / "zed"),
}


# ── Claude Code skill bundle ───────────────────────────────────────────────


_SKILL_REQUIRED_FILES = {
    "deep-research": (
        "SKILL.md",
        "bin/deep_research.py",
        "bin/quota.sh",
        "bin/run.sh",
    ),
    "gpt2agent": ("SKILL.md", "tools-reference.md"),
}


def _install_one_skill(
    name: str,
    src: Path,
    dst_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Transactionally copy one filtered skill directory into *dst_dir*."""
    dst = dst_dir / name

    if dry_run:
        _info(f"skill: would install {src} → {dst}")
        return {"path": dst, "changed": False}

    dst.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.staging-", dir=dst.parent))
    backup: Path | None = None
    try:
        shutil.copytree(
            src,
            staging,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        required = _SKILL_REQUIRED_FILES.get(name, ("SKILL.md",))
        missing = [relative for relative in required if not (staging / relative).is_file()]
        if missing:
            raise RuntimeError(
                f"skill bundle '{name}' missing required file(s): {', '.join(missing)}"
            )

        had_target = dst.exists()
        if had_target:
            backup = dst.with_name(dst.name + ".bak-gpt2agent")
            if backup.exists():
                shutil.rmtree(backup)

        try:
            if backup is not None:
                shutil.move(str(dst), str(backup))
            shutil.move(str(staging), str(dst))
            # Ensure executable bits on shell scripts (lost on some filesystems).
            for sh in dst.glob("bin/*.sh"):
                sh.chmod(0o755)
            _ok(
                f"skill: installed {name} → {dst} "
                f"(backup: {backup.name if backup else 'none'})"
            )
        except BaseException:
            if backup is not None and backup.exists():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.move(str(backup), str(dst))
            elif not had_target and dst.exists():
                shutil.rmtree(dst)
            raise

        return {"path": dst, "backup": backup, "changed": True}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def install_claude_skill(
    *,
    dst_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy bundled skills (deep-research + gpt2agent) into ``~/.claude/skills/``.

    The deep-research skill calls gpt2agent's ConversationClient directly
    (bypasses MCP) so it works even before restarting Claude Code.
    The gpt2agent skill provides full account access instructions and
    pre-approves all 32 MCP tools.
    """
    skills_src = Path(__file__).parent / "skills"
    target_dir = dst_dir or Path.home() / ".claude" / "skills"

    results = []
    for name in ("deep-research", "gpt2agent"):
        src = skills_src / name
        if not src.exists():
            _warn(f"skill bundle '{name}' not found in package — skipped")
            continue
        results.append(_install_one_skill(name, src, target_dir, dry_run=dry_run))

    if not results:
        return {"path": None, "skipped": True}

    return results[-1]


# ── auto-detect ────────────────────────────────────────────────────────────


def detect_clients() -> list[str]:
    """Return clients whose config locations are present on disk."""
    detected = []
    if (Path.home() / ".claude.json").exists() or (Path.home() / ".claude").exists():
        detected.append("claude-code")
    if _codex_home().exists():
        detected.append("codex")
    for name, (_installer, detect) in _EXTRA_HOSTS.items():
        if detect().exists():
            detected.append(name)
    return detected


# All client names install supports (for --client choices and docs).
SUPPORTED_CLIENTS = ["claude-code", "codex", *_EXTRA_HOSTS.keys()]


# ── top-level entry point ──────────────────────────────────────────────────


def run_install(
    *,
    client: str = "all",
    transport: str = "stdio",
    http_port: int = 9000,
    install_skill: bool = True,
    dry_run: bool = False,
) -> int:
    """Run the install flow for the chosen client(s)."""
    if transport not in {"stdio", "http"}:
        _err("transport must be 'stdio' or 'http'")
        _info("No configuration files were changed.")
        return 1

    _h1("gpt2agent install")

    if client == "all":
        targets = detect_clients()
        if not targets:
            _err("No supported clients detected on this machine.")
            _info(
                "Expected one of: ~/.claude.json, ~/.claude/, "
                f"selected Codex home ({_codex_home()}/)"
            )
            return 1
        _info(f"detected: {', '.join(targets)}")
    else:
        targets = [client]

    # URL registration is a Claude Code-only contract. Other supported hosts
    # are configured as spawned stdio subprocesses; accepting --transport http
    # for them used to silently write stdio entries. Preflight the complete
    # target set so an auto-detected mixed install cannot be partially applied.
    if transport == "http":
        unsupported = [target for target in targets if target != "claude-code"]
        if unsupported:
            _err(
                "HTTP registration is supported only for Claude Code; "
                f"unsupported detected target(s): {', '.join(unsupported)}."
            )
            _info("No configuration files were changed.")
            return 1

    failures = 0
    for target in targets:
        try:
            if target == "claude-code":
                install_claude_code(
                    transport=transport,
                    http_port=http_port,
                    dry_run=dry_run,
                )
            elif target == "codex":
                install_codex(dry_run=dry_run)
            elif target in _EXTRA_HOSTS:
                _EXTRA_HOSTS[target][0](dry_run=dry_run)
            else:
                _err(f"Unknown client: {target}")
                failures += 1
        except Exception as exc:  # noqa: BLE001
            _err(f"{target}: {exc}")
            failures += 1

    if install_skill and "claude-code" in targets:
        try:
            install_claude_skill(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            _err(f"skill install: {exc}")
            failures += 1

    if failures:
        _err(f"completed with {failures} failure(s)")
        return 1

    if not dry_run:
        _h1("Next steps")
        if transport == "http":
            print(
                "  • Start and keep the MCP server running separately: "
                f"gpt2agent run --http --port {http_port}"
            )
            print("  • Restart Claude Code after the HTTP server is available")
        else:
            if "claude-code" in targets:
                print("  • Restart Claude Code so it spawns the new MCP subprocess")
            if "codex" in targets:
                print("  • Codex will spawn gpt2agent on next run; nothing else to do")
            print("  • Try:  gpt2agent run --stdio  # (manual stdio smoke test)")
    return 0
