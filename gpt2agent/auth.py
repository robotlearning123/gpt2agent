"""ChatGPT token acquisition — tries multiple sources in order."""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path

from gpt2agent._secure_file import write_private_json


_ACCESS_TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")


def _is_access_token(token: str) -> bool:
    """Return whether *token* has the three-segment JWT access-token shape."""
    return bool(_ACCESS_TOKEN_RE.fullmatch(token))


# --------------------------------------------------------------------------- #
#  Token sources
# --------------------------------------------------------------------------- #


def _from_codex() -> dict | None:
    """Reuse token from Codex CLI ($CODEX_HOME/auth.json or ~/.codex/auth.json).

    Honors ``CODEX_HOME`` so a multi-account user (e.g. ``CODEX_HOME=~/.codex-alt``)
    reads the same login that ``backend.py`` will use, instead of the default one.
    """
    codex_home = os.environ.get("CODEX_HOME")
    p = (Path(codex_home) if codex_home else Path.home() / ".codex") / "auth.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        token = (
            (data.get("tokens") or {}).get("access_token")
            or data.get("accessToken")
            or data.get("access_token")
        )
        if token:
            return {"access_token": token, "source": "codex"}
    except Exception:
        pass
    return None


def _from_saved() -> dict | None:
    """Reuse previously saved token."""
    p = Path.home() / ".gpt2agent" / "token.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        # Accept every shape backend._load_token_with_source() accepts, so a
        # valid saved token isn't missed (forcing browser setup) just because it
        # was stored as {"token": …} or nested {"tokens": {"access_token": …}}.
        token = (
            data.get("access_token")
            or data.get("token")
            or (data.get("tokens") or {}).get("access_token")
        )
        if token:
            return {"access_token": token, "source": "saved"}
    except Exception:
        pass
    return None


def _from_browser() -> dict | None:
    """Open ChatGPT in browser, ask user to paste the token."""
    print()
    print("  Opening chat.openai.com — please log in if needed.")
    print()
    print("  After logging in, open DevTools (F12 / Cmd+Option+I) and run:")
    print()
    print(
        '    copy(JSON.parse(localStorage["@@auth0spajs@@::..."] || "{}").body?.access_token'
    )
    print()
    print("  Then paste the access_token below. Browser cookies such as")
    print("  __Secure-next-auth.session-token are NOT access tokens — the API")
    print("  rejects them with 401, so they are not accepted here.")
    print("  (Tip: running `codex login` once lets gpt2agent reuse that token automatically.)")
    print()
    webbrowser.open("https://chat.openai.com")
    token = getpass.getpass("  Paste access_token (input hidden): ").strip()
    if not token:
        return None
    if not _is_access_token(token):
        # ChatGPT access tokens are 3-segment JWTs; session cookies (5-segment
        # JWE) or random strings only produce 401s downstream — fail here.
        print("  That does not look like a JWT access_token (expected eyJ...x.y.z);")
        print("  not saving it. Use `codex login` for the reliable path.")
        return None
    return {"access_token": token, "source": "browser"}


def _from_browser_use() -> dict | None:
    """Extract token automatically using browser-use CLI (if installed)."""
    browser_use = shutil.which("browser-use")
    if browser_use is None:
        return None

    print("  browser-use detected — attempting automatic token extraction...")
    try:
        # Open ChatGPT and wait for login
        subprocess.run(
            [browser_use, "open", "https://chat.openai.com"],
            timeout=30,
        )
        time.sleep(3)
        input("  Log in if needed, then press Enter to extract token... ")

        # Try localStorage first
        result = subprocess.run(
            [
                browser_use,
                "eval",
                "JSON.stringify(Object.entries(localStorage).filter(([k]) => k.includes('auth0')))",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        raw = result.stdout.strip()
        if raw and "access_token" in raw:
            entries = json.loads(raw)
            for _, v in entries:
                try:
                    body = json.loads(v).get("body", {})
                    token = body.get("access_token")
                    if token and _is_access_token(token):
                        return {"access_token": token, "source": "browser-use"}
                except Exception:
                    pass

        # No cookie fallback: the __Secure-next-auth.session-token cookie is a
        # NextAuth session cookie, not the chatgpt.com-scoped access-token JWT
        # the backend sends as `Authorization: Bearer` — saving it "succeeds"
        # here and then every API call 401s. Better to fail visibly.
        print("  browser-use found no access_token in localStorage.")

    except Exception as e:
        print(f"  browser-use extraction failed: {e}")
    return None


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #


def get_token(interactive: bool = True) -> str:
    """Return a valid ChatGPT access token, trying sources in priority order."""
    # Prefer Codex for parity with BackendClient/setup/docs: it honors CODEX_HOME
    # and auto-refreshes, while ~/.gpt2agent/token.json can be stale or from a
    # different account.
    for fn in [_from_codex, _from_saved]:
        result = fn()
        if result:
            print(f"  ✓ Token found ({result['source']})")
            return result["access_token"]

    if not interactive:
        raise RuntimeError("No ChatGPT token found. Run: gpt2agent setup")

    result = _from_browser_use()
    if result:
        print(f"  ✓ Token found ({result['source']})")
        return result["access_token"]

    result = _from_browser()
    if not result:
        raise RuntimeError("Token acquisition cancelled.")

    # Save via a private directory and an exclusive same-directory temp file;
    # never open an existing token symlink or hard link in place.
    write_private_json(
        Path.home() / ".gpt2agent" / "token.json",
        result,
        indent=2,
    )
    print("  Token saved to ~/.gpt2agent/token.json")
    return result["access_token"]
