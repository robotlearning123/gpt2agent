#!/usr/bin/env python3
"""fleet-account-monitor — periodic liveness check for the two local ChatGPT
accounts behind gpt2agent, plus the serial task runners that serve them.

Design constraints (learned 2026-09-18):
- Checks are READ-LIGHT: token expiry is decoded locally; per account we make
  exactly one GET /backend-api/me. No sentinel mint, no conversation POST —
  account B is under `account_sharing_degrade` and probing harder is itself
  a signal. Deeper probing belongs to `gpt2agent doctor`, run manually.
- Safe under cron: no stdout spam, status goes to monitor-status.json +
  one line per cycle in monitor.log. Exit nonzero if anything FAILED so a
  cron MAILTO or a supervisor notices.
- Dead runners are restarted via start_runner.sh (flock in runner.py makes
  double-start harmless).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import base64
from datetime import datetime, timezone
from pathlib import Path

G2A = Path.home() / ".local/state/token-agent/g2a"
STATUS_JSON = G2A / "monitor-status.json"
LOG = G2A / "monitor.log"
REPO = Path("/home/robot/workspace/47-chatgpt2agent/gpt2agent")

ACCOUNTS = {
    "a": None,  # default CODEX_HOME=~/.codex
    "b": str(Path.home() / ".codex-cx2"),
}

HEARTBEAT_MAX_AGE_S = 120


def _log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line)
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _jwt_exp(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def check_account(name: str, codex_home: str | None) -> dict:
    """One read-only probe per account. Runs in a subprocess so each account
    gets a clean CODEX_HOME and a fresh BackendClient."""
    env = dict(os.environ)
    if codex_home:
        env["CODEX_HOME"] = codex_home
    else:
        env.pop("CODEX_HOME", None)
    code = r"""
import json, os, sys
sys.path.insert(0, %r)
from gpt2agent.backend import BackendClient, _load_token_with_source
tok, src = _load_token_with_source()
import base64
p = tok.split('.')[1]; p += '=' * (-len(p) %% 4)
exp = json.loads(base64.urlsafe_b64decode(p)).get('exp')
c = BackendClient()
me = c.get('/backend-api/me')
print(json.dumps({'exp': exp, 'src': str(src), 'me_email': (me or {}).get('email'), 'plan': ((me or {}).get('accounts') or {})}))
""" % str(REPO)
    try:
        out = subprocess.run(
            [sys.executable, "-c", code],
            env=env, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"account": name, "ok": False, "error": "probe timeout 60s"}
    if out.returncode != 0:
        err = (out.stderr or out.stdout).strip().splitlines()
        return {"account": name, "ok": False,
                "error": err[-1][:200] if err else "probe failed"}
    try:
        data = json.loads(out.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"account": name, "ok": False, "error": "unparseable probe output"}
    exp = data.get("exp")
    ttl_min = round((exp - time.time()) / 60) if exp else None
    ok = bool(data.get("me_email")) and (ttl_min is None or ttl_min > 0)
    return {"account": name, "ok": ok, "email": data.get("me_email"),
            "token_ttl_min": ttl_min, "token_src": data.get("src")}


def _sys_env() -> dict:
    # cron provides no session bus: point systemctl --user at the user manager
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                   f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


def _pid_is_runner(pid: int) -> bool:
    try:
        return "runner.py" in Path(f"/proc/{pid}/cmdline").read_bytes().decode(
            "utf-8", "replace")
    except OSError:
        return False


def check_runner(name: str) -> dict:
    pid_file = G2A / f"runner-{name}.pid"
    hb = G2A / f"heartbeat-{name}"
    # systemd is the supervisor of record — check it first (a stale pid file
    # left by a start_runner.sh attempt must not mask a healthy unit).
    r = subprocess.run(["systemctl", "--user", "is-active",
                        f"token-agent-g2a-runner-{name}.service"],
                       capture_output=True, text=True, timeout=10,
                       env=_sys_env())
    alive = r.stdout.strip() == "active"
    pid = None
    if not alive and pid_file.exists():
        try:
            pid = int(pid_file.read_text().split("=")[-1].strip())
        except (ValueError, IndexError):
            pass
        # pid reuse guard: only count it alive if it really is runner.py
        alive = bool(pid) and _pid_is_runner(pid)
        if not alive:
            pid = None
    hb_age = None
    if hb.exists():
        hb_age = round(time.time() - hb.stat().st_mtime)
    res = {"runner": name, "pid": pid, "alive": alive,
           "heartbeat_age_s": hb_age}
    healthy = alive and hb_age is not None and hb_age < HEARTBEAT_MAX_AGE_S
    if not healthy:
        # Prefer systemd (autopilot lane owns the units now); fall back to
        # the lane's start_runner.sh. runner.py's flock makes a double-start
        # exit harmlessly if one is actually alive elsewhere.
        unit = f"token-agent-g2a-runner-{name}.service"
        r = subprocess.run(["systemctl", "--user", "restart", unit],
                           capture_output=True, text=True, timeout=30,
                           env=_sys_env())
        if r.returncode != 0 and (G2A / "start_runner.sh").exists():
            r = subprocess.run(["bash", str(G2A / "start_runner.sh"), name],
                               capture_output=True, text=True, timeout=30)
        res["restarted"] = r.returncode == 0
        res["ok"] = res["restarted"]
        _log(f"runner-{name} unhealthy (alive={alive} hb_age={hb_age}) "
             f"→ restart rc={r.returncode}")
    else:
        res["ok"] = True
    return res


def check_version() -> dict:
    try:
        sha = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short", "origin/main"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        sys.path.insert(0, str(REPO))
        import gpt2agent
        return {"ok": True, "version": gpt2agent.__version__, "clone_sha": sha}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def check_bridge() -> dict:
    """Local-only sentinel-bridge health — no upstream call.

    Catches the failure class that actually happened: bridge dir wiped or a
    venv dependency lost (ModuleNotFoundError). A live mint probe stays in
    `gpt2agent doctor` — probing upstream every 15min is itself a flag risk."""
    d = Path(os.environ.get("GPT2AGENT_SENTINEL_BRIDGE",
                            Path.home() / ".gpt2agent" / "sentinel-bridge"))
    if not (d / "ENABLED").exists():
        return {"bridge": True, "ok": False, "error": "no ENABLED marker"}
    if not (d / "wrapper" / "reverse" / "vm.py").exists():
        return {"bridge": True, "ok": False, "error": "wrapper/reverse/vm.py missing"}
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        import wrapper.reverse.vm  # noqa: F401
        return {"bridge": True, "ok": True}
    except Exception as exc:
        return {"bridge": True, "ok": False, "error": f"import: {exc}"}


def main() -> int:
    results = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "accounts": [check_account(n, h) for n, h in ACCOUNTS.items()],
        "runners": [check_runner(n) for n in ("a", "b")],
        "install": check_version(),
        "bridge": check_bridge(),
    }
    flat = results["accounts"] + results["runners"] + [results["install"], results["bridge"]]
    results["ok"] = all(r.get("ok") for r in flat)
    tmp = STATUS_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(results, indent=1) + "\n")
    tmp.chmod(0o600)
    tmp.replace(STATUS_JSON)
    summary = " | ".join(
        f"{r.get('account') or r.get('runner') or ('bridge' if r.get('bridge') else 'install')}:"
        f"{'OK' if r.get('ok') else 'FAIL ' + str(r.get('error', ''))[:80]}"
        for r in flat
    )
    _log(summary)
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
