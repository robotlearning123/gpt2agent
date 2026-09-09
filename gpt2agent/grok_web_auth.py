"""Private, reloadable Grok website authentication storage."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from curl_cffi.requests.impersonate import BrowserType

from ._secure_file import read_private_json
from .grok_errors import GrokError


_PROFILE_RE = re.compile(r"^chrome[0-9]+$")
_COOKIE_NAMES = frozenset({"sso", "sso-rw", "grok_device_id", "cf_clearance"})
_REQUIRED_COOKIE_NAMES = frozenset({"sso", "sso-rw"})
_ROOT_FIELDS = frozenset(
    {"schema_version", "source", "browser_impersonation", "cookies"}
)
_COOKIE_FIELDS = frozenset(
    {"name", "domain", "path", "expires", "secure", "http_only", "value"}
)
_MAX_COOKIE_VALUE_BYTES = 16_384
_MAX_AUTH_BYTES = 131_072
_MAX_LOAD_ATTEMPTS = 3


def supported_browser_impersonations() -> frozenset[str]:
    """Return exact desktop Chrome profiles exposed by curl-cffi."""
    return frozenset(
        item.value
        for item in BrowserType
        if isinstance(item.value, str) and _PROFILE_RE.fullmatch(item.value)
    )


def browser_impersonation_by_major() -> Mapping[int, str]:
    """Map each exact supported desktop Chrome major to its enum value."""
    return MappingProxyType(
        {
            int(profile.removeprefix("chrome")): profile
            for profile in supported_browser_impersonations()
        }
    )


def _error(code: str) -> GrokError:
    return GrokError(code, retryable=False)


@dataclass(frozen=True)
class GrokWebAuthSnapshot:
    generation: int
    cookies: Mapping[str, str]
    browser_impersonation: str


@dataclass(frozen=True)
class _LoadedAuth:
    fingerprint: tuple[int, int, int, int]
    snapshot: GrokWebAuthSnapshot
    expires_at: int


def _fingerprint(path: Path) -> tuple[int, int, int, int]:
    observed = path.lstat()
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
    )


def _bounded_cookie_value(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        len(encoded) <= _MAX_COOKIE_VALUE_BYTES
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def validate_grok_web_auth_document(
    payload: Any,
    *,
    now: int | None = None,
) -> tuple[dict[str, str], str, int]:
    """Validate one complete closed auth document without reading or writing."""
    current_time = int(time.time()) if now is None else now
    if not isinstance(payload, dict) or frozenset(payload) != _ROOT_FIELDS:
        raise _error("GROK_WEB_AUTH_MISSING")
    if payload.get("schema_version") != 1 or payload.get("source") not in {
        "browser-use",
        "manual",
    }:
        raise _error("GROK_WEB_AUTH_MISSING")

    profile = payload.get("browser_impersonation")
    if not isinstance(profile, str) or profile not in supported_browser_impersonations():
        raise _error("GROK_WEB_AUTH_MISSING")

    raw_cookies = payload.get("cookies")
    if not isinstance(raw_cookies, list) or not 2 <= len(raw_cookies) <= len(
        _COOKIE_NAMES
    ):
        raise _error("GROK_WEB_AUTH_MISSING")

    cookies: dict[str, str] = {}
    expirations: list[int] = []
    for raw_cookie in raw_cookies:
        if not isinstance(raw_cookie, dict) or frozenset(raw_cookie) != _COOKIE_FIELDS:
            raise _error("GROK_WEB_AUTH_MISSING")
        name = raw_cookie.get("name")
        expires = raw_cookie.get("expires")
        if (
            not isinstance(name, str)
            or name not in _COOKIE_NAMES
            or name in cookies
            or raw_cookie.get("domain") not in {"grok.com", ".grok.com"}
            or raw_cookie.get("path") != "/"
            or raw_cookie.get("secure") is not True
            or raw_cookie.get("http_only") is not True
            or isinstance(expires, bool)
            or not isinstance(expires, int)
            or not 1 <= expires <= 2**53 - 1
            or not _bounded_cookie_value(raw_cookie.get("value"))
        ):
            raise _error("GROK_WEB_AUTH_MISSING")
        if expires <= current_time:
            raise _error("GROK_WEB_AUTH_EXPIRED")
        cookies[name] = raw_cookie["value"]
        expirations.append(expires)

    if not _REQUIRED_COOKIE_NAMES.issubset(cookies):
        raise _error("GROK_WEB_AUTH_MISSING")
    return cookies, profile, min(expirations)


class GrokWebAuthStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".gpt2agent" / "grok-web-auth.json"
        self._loaded: _LoadedAuth | None = None

    @property
    def auth_generation(self) -> int:
        return self.snapshot().generation

    def _load(self) -> _LoadedAuth:
        failure: GrokError | None = None
        for _ in range(_MAX_LOAD_ATTEMPTS):
            try:
                before = _fingerprint(self.path)
                if self._loaded is not None and self._loaded.fingerprint == before:
                    if self._loaded.expires_at <= int(time.time()):
                        raise _error("GROK_WEB_AUTH_EXPIRED")
                    return self._loaded
                payload = read_private_json(self.path, maximum_bytes=_MAX_AUTH_BYTES)
                after = _fingerprint(self.path)
            except GrokError as exc:
                failure = _error(exc.code)
                break
            except (OSError, RuntimeError, ValueError):
                failure = _error("GROK_WEB_AUTH_MISSING")
                break
            if before != after:
                continue
            try:
                cookies, profile, expires_at = validate_grok_web_auth_document(
                    payload,
                    now=int(time.time()),
                )
            except GrokError as exc:
                failure = _error(exc.code)
                break
            generation = 0 if self._loaded is None else self._loaded.snapshot.generation + 1
            snapshot = GrokWebAuthSnapshot(
                generation=generation,
                cookies=MappingProxyType(dict(cookies)),
                browser_impersonation=profile,
            )
            self._loaded = _LoadedAuth(after, snapshot, expires_at)
            return self._loaded
        if failure is None:
            failure = _error("GROK_WEB_AUTH_MISSING")
        raise failure from None

    def snapshot(self) -> GrokWebAuthSnapshot:
        """Return immutable cookies plus one validated impersonation profile."""
        return self._load().snapshot

    def status(self) -> dict[str, Any]:
        """Return configured/authenticated/expires_at/cookie_names only."""
        try:
            loaded = self._load()
        except GrokError:
            return {
                "configured": self.path.is_file(),
                "authenticated": False,
                "expires_at": None,
                "cookie_names": [],
            }
        return {
            "configured": True,
            "authenticated": True,
            "expires_at": loaded.expires_at,
            "cookie_names": sorted(loaded.snapshot.cookies),
        }
