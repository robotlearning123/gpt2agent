"""gpt2agent setup wizard — one command, done."""

from __future__ import annotations

import getpass
import json
import os
import sys
import webbrowser
from pathlib import Path

# ── colours ────────────────────────────────────────────────────────────────
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg):
    print(f"  {GREEN}✓{RESET} {msg}")


def info(msg):
    print(f"  {YELLOW}→{RESET} {msg}")


def err(msg):
    print(f"  {RED}✗{RESET} {msg}")


def h1(msg):
    print(f"\n{BOLD}{msg}{RESET}")


# ── token acquisition ───────────────────────────────────────────────────────


def _codex_auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else Path.home() / ".codex") / "auth.json"


def _token_from_codex() -> str | None:
    p = _codex_auth_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        tokens = d.get("tokens") or {}
        return (
            tokens.get("access_token") or d.get("accessToken") or d.get("access_token")
        )
    except Exception:
        return None


def _token_from_saved() -> str | None:
    p = Path.home() / ".gpt2agent" / "token.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return (
            d.get("access_token")
            or d.get("token")
            or (d.get("tokens") or {}).get("access_token")
        )
    except Exception:
        return None


def _token_via_manual() -> str | None:
    """Open browser + ask user to paste token."""
    print()
    print("  Easiest path: install Codex CLI and run `codex login` — we'll")
    print(f"  pick up {_codex_auth_path()} automatically next time.")
    print()
    print("  Or paste a token manually:")
    print("    1. Press F12 → Console on chat.openai.com")
    print("    2. Paste + run:")
    print()
    print("       copy((Object.entries(localStorage).find(([k])=>")
    print(
        '         k.includes(\'auth0\'))||[])[1]?.match(/"access_token":"([^"]+)"/) ?.[1])'
    )
    print()
    webbrowser.open("https://chat.openai.com")
    token = getpass.getpass("  Paste token here (input hidden; empty to cancel): ").strip()
    if not token:
        return None
    from gpt2agent.auth import _is_access_token

    if not _is_access_token(token):
        err("That does not look like a JWT access_token; token was not saved")
        return None
    return token


def get_token() -> str:
    h1("Step 1 — Locate ChatGPT token")

    if t := _token_from_codex():
        ok(f"Found Codex CLI token ({_codex_auth_path()})")
        return t
    if t := _token_from_saved():
        ok("Using saved token")
        return t
    info("No Codex token — falling back to manual paste")
    if t := _token_via_manual():
        ok("Token received")
        return t
    raise SystemExit("Login cancelled. Re-run: gpt2agent setup")


def save_token(token: str) -> None:
    d = Path.home() / ".gpt2agent"
    d.mkdir(exist_ok=True)
    p = d / "token.json"
    # Open 0o600 up front so the bearer token is never briefly world-readable
    # between write and chmod under a permissive umask.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # O_CREAT's mode is ignored if the file already exists; tighten an
        # existing 0644 token.json explicitly (after O_TRUNC, before writing).
        os.fchmod(fd, 0o600)
    except (OSError, AttributeError):
        pass  # non-POSIX (e.g. Windows lacks fchmod)
    with os.fdopen(fd, "w") as f:
        json.dump({"access_token": token}, f)


# ── plan detection ──────────────────────────────────────────────────────────


def detect_plan(client=None) -> str:
    """Measure the authenticated account entitlement. Returns pro/plus/free."""
    owns_client = client is None
    try:
        from gpt2agent.backend import BackendClient

        bc = BackendClient() if client is None else client
        acct = bc.get("/backend-api/accounts/check/v4-2023-04-27")
    except Exception as exc:
        raise RuntimeError(f"Could not verify ChatGPT account: {exc}") from exc
    finally:
        if owns_client and "bc" in locals():
            close = getattr(getattr(bc, "_session", None), "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    accounts = (acct or {}).get("accounts") if isinstance(acct, dict) else None
    if not isinstance(accounts, dict) or not accounts:
        raise RuntimeError("Could not verify ChatGPT account: response contained no accounts")

    verified_plans: list[str] = []
    for a in accounts.values():
        if isinstance(a, dict):
            ent = a.get("entitlement")
            if not isinstance(ent, dict):
                continue
            plan = str(ent.get("subscription_plan") or "").strip().lower()
            active = ent.get("has_active_subscription")
            if active is False:
                verified_plans.append("free")
            elif active is True and plan in {"pro", "chatgptpro"}:
                verified_plans.append("pro")
            elif active is True and plan in {"plus", "chatgptplus"}:
                verified_plans.append("plus")

    for plan in ("pro", "plus", "free"):
        if plan in verified_plans:
            return plan
    raise RuntimeError("Could not verify ChatGPT account plan from entitlement response")


# ── MCP server ───────────────────────────────────────────────────────────────

MCP_CONFIG_PATH = Path.home() / ".gpt2agent" / "config.toml"
MCP_PORT = 9000


def write_mcp_config(plan: str) -> None:
    from gpt2agent.install import _atomic_write, _backup

    chat_model = "gpt-5-5-pro" if plan == "pro" else "gpt-5-3"
    cfg = f"""[server]
# Loopback only: the HTTP transport is unauthenticated and proxies your full
# ChatGPT account. Version 0.0.12 refuses every non-loopback host.
host = "127.0.0.1"
port = {MCP_PORT}

[models]
chat = "{chat_model}"
"""
    # Users hand-edit this file (models, host opt-ins); don't clobber their
    # copy silently — keep a .bak and write atomically.
    if MCP_CONFIG_PATH.exists():
        if MCP_CONFIG_PATH.read_text() == cfg:
            return
        _backup(MCP_CONFIG_PATH)
    _atomic_write(MCP_CONFIG_PATH, cfg)


# ── final summary ────────────────────────────────────────────────────────────


def print_summary(plan: str) -> None:
    print()
    print(f"{BOLD}{'─' * 50}{RESET}")
    print(f"{GREEN}{BOLD}  Done!{RESET}  ChatGPT {plan.capitalize()} is ready.")
    print(f"{'─' * 50}")
    print()
    print(f"  Plan:      ChatGPT {plan.capitalize()}")
    print("  Transport: stdio (local subprocess; not network-exposed)")
    print()
    print("  Restart your MCP client (Claude Code / Cursor / …) to load the tools.")
    print("  Then try the `account_status` or `chat` tool from your agent.")
    print()


# ── entry point ──────────────────────────────────────────────────────────────


def run_setup() -> None:
    print(f"\n{BOLD}gpt2agent setup{RESET}")
    print("Use your ChatGPT Plus/Pro in Claude Code and other AI tools.\n")

    try:
        token = get_token()
        save_token(token)

        h1("Detecting plan...")
        plan = detect_plan()
        ok(f"ChatGPT {plan.capitalize()} detected")

        # Persist a model default, then register over stdio — the same safe path
        # as `gpt2agent install`. (No background HTTP daemon / LaunchAgent / legacy
        # `openai` URL entry: the stdio transport is what every MCP client wants and
        # avoids exposing an unauthenticated account proxy.)
        write_mcp_config(plan)
        from gpt2agent.install import run_install

        rc = run_install(client="all", transport="stdio")
        if rc != 0:
            print()
            print(f"{RED}  Token saved, but client registration did not fully "
                  f"succeed (see messages above).{RESET}")
            print("  Fix the reported client(s) and re-run:  gpt2agent install")
            sys.exit(rc)
        print_summary(plan)

    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        sys.exit(1)
    except RuntimeError as exc:
        err(str(exc))
        sys.exit(1)
