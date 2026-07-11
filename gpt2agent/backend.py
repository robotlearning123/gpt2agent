"""Direct chatgpt.com backend client — no proxy, no API key."""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from curl_cffi import CurlECode, CurlOpt, requests

from gpt2agent.errors import (
    BackendContractError,
    BackendHTTPError,
    backend_http_error,
    normalize_route,
)
from gpt2agent.request_policy import RequestPolicy


_BASE = "https://chatgpt.com"
_CLIENT_VERSION = "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad"
_CLIENT_BUILD = "5955942"
_MAX_JSON_BYTES = 4 * 1024 * 1024
_DEFAULT_GET_TIMEOUT_SECONDS = 20.0
_MIN_GET_TIMEOUT_SECONDS = 0.1
_REQUEST_POLICY = RequestPolicy()
_CURL_WRITEFUNC_ERROR = 0xFFFFFFFF


class _BoundedResponseBody:
    """Collect a curl response without ever retaining more than ``limit`` bytes."""

    __slots__ = ("content", "limit", "overflowed")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.content = bytearray()
        self.overflowed = False

    def __call__(self, chunk: bytes) -> int:
        if self.overflowed or len(chunk) > self.limit - len(self.content):
            self.overflowed = True
            return _CURL_WRITEFUNC_ERROR
        self.content.extend(chunk)
        return len(chunk)


def _is_filesize_exceeded(error: BaseException) -> bool:
    """Return whether libcurl rejected a response at MAXFILESIZE_LARGE."""
    return getattr(error, "code", None) == CurlECode.FILESIZE_EXCEEDED


def _clamp_get_timeout(timeout_seconds: float | None) -> float:
    if timeout_seconds is None:
        return _DEFAULT_GET_TIMEOUT_SECONDS
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout):
        raise ValueError("timeout_seconds must be finite")
    return min(
        _DEFAULT_GET_TIMEOUT_SECONDS,
        max(_MIN_GET_TIMEOUT_SECONDS, timeout),
    )


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
        raise RuntimeError(
            f"No ChatGPT token found — run `codex login` or `gpt2agent setup` "
            f"({details})"
        )
    raise RuntimeError(
        "No ChatGPT token found — run `codex login` or `gpt2agent setup` "
        f"(checked {codex_path} and {wizard_path})"
    )


def _load_token() -> str:
    """Back-compat shim — return only the token (no source path)."""
    return _load_token_with_source()[0]


_CHROME_131_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class BackendClient:
    def __init__(self) -> None:
        token, source = _load_token_with_source()
        self._token = token
        self._auth_generation = 0
        self._token_source: Path | None = source
        try:
            self._token_mtime: float | None = source.stat().st_mtime if source else None
        except OSError:
            self._token_mtime = None
        self._token_lock = threading.Lock()
        # Keep TLS fingerprint + User-Agent aligned across backend / sentinel /
        # conversation streams. Cloudflare's bot manager cross-checks them and
        # will 403 mixed fingerprints.
        self._session = requests.Session(
            impersonate="chrome131",
            verify=True,
            curl_options={CurlOpt.MAXFILESIZE_LARGE: _MAX_JSON_BYTES},
        )
        self._session.headers.update(
            {
                "User-Agent": _CHROME_131_UA,
                "OAI-Device-Id": str(uuid.uuid4()),
                "OAI-Session-Id": str(uuid.uuid4()),
                "OAI-Language": "en-US",
                "OAI-Client-Version": _CLIENT_VERSION,
                "OAI-Client-Build-Number": _CLIENT_BUILD,
                "Origin": _BASE,
                "Referer": _BASE + "/",
                "Accept": "*/*",
            }
        )

    @property
    def auth_generation(self) -> int:
        """Non-secret counter incremented whenever the token source changes."""
        with self._token_lock:
            self._refresh_token_locked()
            return self._auth_generation

    def _refresh_token_locked(self) -> None:
        """Refresh in-memory token metadata while ``_token_lock`` is held."""
        try:
            token, source = _load_token_with_source()
        except RuntimeError:
            # Preserve the last in-memory bearer. The next account request will
            # return a typed login failure if it is no longer valid.
            return
        try:
            mtime = source.stat().st_mtime if source else None
        except OSError:
            mtime = None

        if (
            token == self._token
            and source == self._token_source
            and mtime == self._token_mtime
        ):
            return
        self._token = token
        self._token_source = source
        self._token_mtime = mtime
        self._auth_generation += 1

    def _reload_token_if_stale(self) -> None:
        """Re-evaluate token sources without mutating shared session headers.

        Codex CLI auto-refreshes ``~/.codex/auth.json`` in the background. Without
        this, multi-minute calls (heavy DR poll phase, codex task waits) can 401
        once the in-memory bearer ages past the file's refresh. Re-evaluating the
        source also lets a running server pick up a new preferred Codex login or
        fall back to the setup token if that login disappears.
        """
        with self._token_lock:
            self._refresh_token_locked()

    def auth_snapshot(
        self, extra: Mapping[str, str] | None = None
    ) -> tuple[int, dict[str, str]]:
        """Atomically pair an auth generation with its request headers.

        Callers that perform a compound operation pass the returned copy through
        every phase. Token rotation is observed only by the next snapshot.
        """
        with self._token_lock:
            self._refresh_token_locked()
            generation = self._auth_generation
            headers = dict(self._session.headers)
            headers["Authorization"] = f"Bearer {self._token}"
        if extra:
            headers.update({str(key): str(value) for key, value in extra.items()})
        return generation, headers

    def request_headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Create one immutable-by-convention authorization snapshot."""
        _, headers = self.auth_snapshot(extra)
        return headers

    @staticmethod
    def _status_error(
        method: str,
        path: str,
        status: int,
        *,
        established: bool,
        fixed_probe: bool,
        retry_after: float | None = None,
    ) -> BackendHTTPError:
        return backend_http_error(
            method,
            path,
            status,
            established=established,
            fixed_probe=fixed_probe,
            retry_after=retry_after,
        )

    @staticmethod
    def _retry_after_header(response: Any) -> Any:
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        return headers.get("Retry-After") or headers.get("retry-after")

    @staticmethod
    def _validate_json_size(response: Any, body: bytearray, route: str) -> None:
        raw_length = None
        response_headers = getattr(response, "headers", None)
        if response_headers is not None:
            raw_length = response_headers.get("Content-Length") or response_headers.get(
                "content-length"
            )
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except (TypeError, ValueError):
                declared = -1
            if declared > _MAX_JSON_BYTES:
                raise BackendContractError(normalize_route(route), "response exceeds 4 MiB")

        if len(body) > _MAX_JSON_BYTES:
            raise BackendContractError(normalize_route(route), "response exceeds 4 MiB")

    @classmethod
    def _decode_json(cls, response: Any, body: bytearray, route: str) -> Any:
        cls._validate_json_size(response, body, route)
        if not body.strip():
            return None
        decoded = False
        value: Any = None
        try:
            value = json.loads(body)
        except Exception:
            pass
        else:
            decoded = True
        if decoded:
            return value
        raise BackendContractError(
            normalize_route(route), "expected a JSON 2xx response"
        ) from None

    def get(
        self,
        path: str,
        target_path: str | None = None,
        target_route: str | None = None,
        *,
        params: Mapping[str, Any] | None = None,
        auth_headers: Mapping[str, str] | None = None,
        established: bool = True,
        fixed_probe: bool = False,
        timeout_seconds: float | None = None,
    ) -> Any:
        timeout = _clamp_get_timeout(timeout_seconds)
        extra: dict[str, str] = {}
        if target_path is not None:
            extra["X-OpenAI-Target-Path"] = target_path
        if target_route is not None:
            extra["X-OpenAI-Target-Route"] = target_route

        headers = dict(auth_headers) if auth_headers is not None else self.request_headers()
        headers.update(extra)
        with _REQUEST_POLICY.request("GET", path):
            body = _BoundedResponseBody(_MAX_JSON_BYTES)
            response_oversized = False
            network_failed = False
            try:
                request_kwargs: dict[str, Any] = {
                    "headers": headers,
                    "timeout": timeout,
                    "allow_redirects": False,
                    "content_callback": body,
                }
                if params is not None:
                    request_kwargs["params"] = dict(params)
                r = self._session.get(_BASE + path, **request_kwargs)
            except Exception as exc:
                response_oversized = body.overflowed or _is_filesize_exceeded(exc)
                network_failed = not response_oversized
            if response_oversized:
                raise BackendContractError(
                    normalize_route(path), "response exceeds 4 MiB"
                ) from None
            if network_failed:
                raise BackendHTTPError(
                    "GET", path, None, code="temporarily_failed", retryable=True
                ) from None

            if r.status_code != 200:
                retry_after = None
                if r.status_code == 429:
                    retry_after = _REQUEST_POLICY.activate_cooldown(
                        path, self._retry_after_header(r)
                    )
                raise self._status_error(
                    "GET",
                    path,
                    r.status_code,
                    established=established,
                    fixed_probe=fixed_probe,
                    retry_after=retry_after,
                )
            return self._decode_json(r, body.content, path)

    def post(
        self,
        path: str,
        json: Any = None,
        target_path: str | None = None,
        target_route: str | None = None,
        *,
        params: Mapping[str, Any] | None = None,
        auth_headers: Mapping[str, str] | None = None,
        established: bool = True,
        fixed_probe: bool = False,
    ) -> Any:
        extra: dict[str, str] = {"Content-Type": "application/json"}
        if target_path is not None:
            extra["X-OpenAI-Target-Path"] = target_path
        if target_route is not None:
            extra["X-OpenAI-Target-Route"] = target_route

        headers = dict(auth_headers) if auth_headers is not None else self.request_headers()
        headers.update(extra)
        with _REQUEST_POLICY.request("POST", path):
            body = _BoundedResponseBody(_MAX_JSON_BYTES)
            response_oversized = False
            network_failed = False
            try:
                request_kwargs: dict[str, Any] = {
                    "headers": headers,
                    "json": json,
                    "timeout": 30,
                    "allow_redirects": False,
                    "content_callback": body,
                }
                if params is not None:
                    request_kwargs["params"] = dict(params)
                r = self._session.post(_BASE + path, **request_kwargs)
            except Exception as exc:
                response_oversized = body.overflowed or _is_filesize_exceeded(exc)
                network_failed = not response_oversized
            if response_oversized:
                raise BackendContractError(
                    normalize_route(path), "response exceeds 4 MiB"
                ) from None
            if network_failed:
                raise BackendHTTPError(
                    "POST", path, None, code="temporarily_failed", retryable=True
                ) from None

            if not (200 <= r.status_code < 300):
                retry_after = None
                if r.status_code == 429:
                    retry_after = _REQUEST_POLICY.activate_cooldown(
                        path, self._retry_after_header(r)
                    )
                raise self._status_error(
                    "POST",
                    path,
                    r.status_code,
                    established=established,
                    fixed_probe=fixed_probe,
                    retry_after=retry_after,
                )
            return self._decode_json(r, body.content, path)
