"""Direct chatgpt.com backend client — no proxy, no API key."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from curl_cffi import requests

from gpt2agent._log_redact import redact_error


_BASE = "https://chatgpt.com"
_CLIENT_BUILD = "5955942"
_SEC_CH_UA = '"Chromium";v="136", "Not=A?Brand";v="24", "Google Chrome";v="136"'


class TokenNotFoundError(RuntimeError):
    """No ChatGPT token could be located from any known source.

    A ``RuntimeError`` subclass so existing ``except RuntimeError`` handlers and
    ``pytest.raises(RuntimeError, ...)`` assertions keep working, while callers
    that want to distinguish "not logged in yet" (an expected first-run state)
    from a genuine backend failure can catch this specific type — e.g. the CLI
    turns it into a clean one-line message instead of a traceback.
    """


class UpstreamChallengeError(RuntimeError):
    """ChatGPT's Sentinel challenge changed and can no longer be satisfied.

    A ``RuntimeError`` subclass so existing ``except RuntimeError`` handlers and
    ``pytest.raises(RuntimeError, ...)`` assertions keep working, while callers
    that need to tell "chatgpt.com moved the goalposts" apart from "your token
    expired" can catch this specific type. Raised by ``sentinel.py`` when the
    proof-of-work or Turnstile stage of ``sentinel/chat-requirements`` cannot be
    completed — the read-only REST endpoints keep working, so this is never a
    login or configuration problem on the user's side.
    """


class UpstreamEndpointError(RuntimeError):
    """A backend-api endpoint changed or disappeared upstream.

    A ``RuntimeError`` subclass so existing ``except RuntimeError`` handlers and
    ``pytest.raises(RuntimeError, ...)`` assertions keep working. Raised where a
    tool's endpoint returns a permanent protocol-level refusal (e.g. HTTP 405
    where a GET used to be served) — retrying or re-authenticating cannot help,
    so the tool is broken until gpt2agent is updated to match the new surface.
    """


class UsageLimitError(RuntimeError):
    """The account hit a model/feature usage cap upstream.

    Raised from the ``usage_limit`` SSE error frame and by the
    ``conversation/init`` pre-flight when the requested model sits in
    ``model_limits``. ``resets_after`` carries the ISO timestamp the cap lifts
    when known, so callers can present a real wait time instead of a bare
    "hit your limit".
    """

    def __init__(self, message: str, *, resets_after: str | None = None) -> None:
        super().__init__(message)
        self.resets_after = resets_after


def limits_from_init(init: dict | None, *, model: str | None = None,
                     feature: str | None = None) -> dict:
    """Extract quota state from a ``/backend-api/conversation/init`` response.

    Returns ``{"model_resets_after": str|None, "feature_remaining": int|None,
    "feature_resets_after": str|None, "default_model_slug": str|None}``.
    """
    out: dict = {
        "model_resets_after": None,
        "feature_remaining": None,
        "feature_resets_after": None,
        "default_model_slug": (init or {}).get("default_model_slug"),
    }
    if not init:
        return out
    if model:
        for lim in init.get("model_limits") or []:
            if isinstance(lim, dict) and lim.get("model_slug") == model:
                out["model_resets_after"] = lim.get("resets_after")
                break
    if feature:
        for lim in init.get("limits_progress") or []:
            if isinstance(lim, dict) and lim.get("feature_name") == feature:
                raw = lim.get("remaining")
                if raw is not None:
                    try:
                        out["feature_remaining"] = int(raw)
                    except (TypeError, ValueError):
                        pass
                out["feature_resets_after"] = lim.get("reset_after")
                break
        for feat in init.get("blocked_features") or []:
            if isinstance(feat, dict) and feat.get("name") == feature:
                out["feature_resets_after"] = (
                    out["feature_resets_after"] or feat.get("resets_after")
                )
    return out


def _load_token_with_source() -> tuple[str, Path | None]:
    """Load the ChatGPT bearer token and return its source file for mtime tracking.

    Search order:
      1. ``$CODEX_HOME/auth.json`` (or ``~/.codex/auth.json`` if CODEX_HOME
         is unset) with ``tokens.access_token`` (codex login). Honoring
         CODEX_HOME lets a second account (e.g. ``CODEX_HOME=~/.codex-alt``)
         be used without touching the default login.
      2. ``~/.gpt2agent/token.json`` with ``token`` (flat) OR
         ``tokens.access_token`` (nested) — written by ``gpt2agent setup``

    Returns (token, source_path). source_path is the file we read; callers
    can stat it later to detect codex's background refresh and reload.
    Raises RuntimeError only if neither source yields a token.
    """
    # Source 1: codex login (honor CODEX_HOME for multi-account)
    _codex_home = os.environ.get("CODEX_HOME")
    codex_path = (Path(_codex_home) if _codex_home else Path.home() / ".codex") / "auth.json"
    codex_err: str | None = None
    if codex_path.exists():
        try:
            data = json.loads(codex_path.read_text())
            token = (data.get("tokens") or {}).get("access_token")
            if token:
                return token, codex_path
            codex_err = f"tokens.access_token missing in {codex_path}"
        except (json.JSONDecodeError, OSError) as exc:
            codex_err = f"Failed to read {codex_path}: {exc}"

    # Source 2: gpt2agent setup wizard
    wizard_path = Path.home() / ".gpt2agent" / "token.json"
    wizard_err: str | None = None
    if wizard_path.exists():
        try:
            data = json.loads(wizard_path.read_text())
            # Accept flat {"token": ...} or {"access_token": ...} or nested {"tokens": {"access_token": ...}}
            token = (
                data.get("token")
                or data.get("access_token")
                or (data.get("tokens") or {}).get("access_token")
            )
            if token:
                return token, wizard_path
            wizard_err = "token/access_token/tokens.access_token missing in ~/.gpt2agent/token.json"
        except (json.JSONDecodeError, OSError) as exc:
            wizard_err = f"Failed to read ~/.gpt2agent/token.json: {exc}"

    # Nothing worked — surface the most informative error we have.
    if codex_err or wizard_err:
        details = "; ".join(e for e in (codex_err, wizard_err) if e)
        raise TokenNotFoundError(
            f"No ChatGPT token found — run `codex login` or `gpt2agent setup` "
            f"({details})"
        )
    raise TokenNotFoundError(
        "No ChatGPT token found — run `codex login` or `gpt2agent setup` "
        f"(checked {codex_path} and {wizard_path})"
    )


def _load_token() -> str:
    """Back-compat shim — return only the token (no source path)."""
    return _load_token_with_source()[0]


class BackendClient:
    def __init__(self) -> None:
        token, source = _load_token_with_source()
        self._token_source: Path | None = source
        try:
            self._token_mtime: float | None = source.stat().st_mtime if source else None
        except OSError:
            self._token_mtime = None
        self._token_lock = threading.Lock()
        # Keep TLS fingerprint + User-Agent aligned across backend / sentinel /
        # conversation streams. Cloudflare's bot manager cross-checks them and
        # will 403 mixed fingerprints. v0.0.17: identity comes from the shared
        # SimProfile — persistent device/session ids and one impersonation for
        # the whole process instead of per-client random uuids + chrome131.
        from gpt2agent.sim import get_profile

        self._profile = get_profile()
        self._session = requests.Session(
            impersonate=self._profile.impersonate, verify=True
        )
        self._session.headers.update(
            {
                "User-Agent": self._profile.ua,
                "Authorization": f"Bearer {token}",
                "OAI-Device-Id": self._profile.device_id,
                "OAI-Session-Id": self._profile.session_id,
                "OAI-Language": self._profile.locale,
                "OAI-Client-Version": self._profile.cached_client_version,
                "OAI-Client-Build-Number": _CLIENT_BUILD,
                "Origin": _BASE,
                "Referer": _BASE + "/",
                "Accept": "*/*",
                "Accept-Language": self._profile.accept_language,
                "sec-ch-ua": _SEC_CH_UA,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )

    def _reload_token_if_stale(self) -> None:
        """Re-evaluate token sources and refresh the Authorization header.

        Codex CLI auto-refreshes ``~/.codex/auth.json`` in the background. Without
        this, multi-minute calls (heavy DR poll phase, codex task waits) can 401
        once the in-memory bearer ages past the file's refresh. Re-evaluating the
        source also lets a running server pick up a new preferred Codex login or
        fall back to the setup token if that login disappears.
        """
        with self._token_lock:
            try:
                token, source = _load_token_with_source()
            except RuntimeError:
                # Keep the stale token rather than break mid-request — the next
                # 401 will surface a clearer error to the caller.
                return
            try:
                mtime = source.stat().st_mtime if source else None
            except OSError:
                mtime = None

            authorization = f"Bearer {token}"
            if (
                source == self._token_source
                and mtime == self._token_mtime
                and self._session.headers.get("Authorization") == authorization
            ):
                return

            self._session.headers["Authorization"] = authorization
            self._token_source = source
            self._token_mtime = mtime

    def get(
        self,
        path: str,
        target_path: str | None = None,
        target_route: str | None = None,
    ) -> Any:
        self._reload_token_if_stale()
        extra: dict[str, str] = {}
        if target_path is not None:
            extra["X-OpenAI-Target-Path"] = target_path
        if target_route is not None:
            extra["X-OpenAI-Target-Route"] = target_route

        r = self._session.get(_BASE + path, headers=extra, timeout=20)

        if r.status_code == 401:
            raise RuntimeError("401 Unauthorized — token expired, run `codex login`")
        if r.status_code == 403:
            raise RuntimeError(f"403 Forbidden for {path}")
        if r.status_code == 404:
            raise RuntimeError(f"404 Not Found: {path}")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for {path}")

        # Mirror post()'s guard: an HTML/empty 2xx (Cloudflare interstitial,
        # gateway page) must surface a clean redacted RuntimeError, not a raw
        # JSONDecodeError stack trace into the MCP client.
        if not r.text.strip():
            return None
        try:
            return r.json()
        except Exception as exc:
            raise RuntimeError(
                f"Expected JSON from {path} but got non-JSON 2xx response: "
                f"{redact_error(r.text)}"
            ) from exc

    def post(
        self,
        path: str,
        json: Any = None,
        target_path: str | None = None,
        target_route: str | None = None,
    ) -> Any:
        self._reload_token_if_stale()
        extra: dict[str, str] = {"Content-Type": "application/json"}
        if target_path is not None:
            extra["X-OpenAI-Target-Path"] = target_path
        if target_route is not None:
            extra["X-OpenAI-Target-Route"] = target_route

        r = self._session.post(_BASE + path, headers=extra, json=json, timeout=30)

        if r.status_code == 401:
            raise RuntimeError("401 Unauthorized — token expired, run `codex login`")
        if r.status_code == 403:
            raise RuntimeError(f"403 Forbidden for {path}")
        if r.status_code == 404:
            raise RuntimeError(f"404 Not Found: {path}")
        if r.status_code == 405:
            raise RuntimeError(f"405 Method Not Allowed: {path}")
        if not (200 <= r.status_code < 300):
            raise RuntimeError(f"HTTP {r.status_code} for {path}: {redact_error(r.text)}")

        if not r.text.strip():
            return None
        try:
            return r.json()
        except Exception as exc:
            raise RuntimeError(
                f"Expected JSON from {path} but got non-JSON 2xx response: "
                f"{redact_error(r.text)}"
            ) from exc
