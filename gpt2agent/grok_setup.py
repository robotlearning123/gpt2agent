"""Secret-safe setup flow for independent Grok Build and website auth lanes."""

from __future__ import annotations

import getpass
import json
import math
import os
import re
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from ._bounded_process import (
    BoundedProcessError,
    ProcessResult,
    run_bounded_process,
)
from ._secure_file import write_private_json
from .errors import InputValidationError
from .grok_build import GrokBuildClient, GrokBuildConfig
from .grok_errors import GrokError
from .grok_web_auth import (
    GrokWebAuthStore,
    browser_impersonation_by_major,
)


Runner = Callable[..., Awaitable[ProcessResult]]
Resolver = Callable[[str], str | None]
SessionFactory = Callable[[], str]
Getpass = Callable[[str], str]
Probe = Callable[[], Awaitable[dict[str, Any]]]

_GROK_URL = "https://grok.com/"
_BROWSER_TIMEOUT_SECONDS = 120.0
_BROWSER_OUTPUT_BYTES = 131_072
_MANUAL_LIFETIME_SECONDS = 30 * 24 * 60 * 60
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CHROME_UA_RE = re.compile(r"(?:^|[^A-Za-z0-9])Chrome/([0-9]{1,3})(?:\.|\s|$)")
_IMPORTED_COOKIE_NAMES = frozenset(
    {"sso", "sso-rw", "grok_device_id", "cf_clearance"}
)
_REQUIRED_COOKIE_NAMES = frozenset({"sso", "sso-rw"})


def _error(code: str) -> GrokError:
    return GrokError(code, retryable=False)


def _owned_session() -> str:
    return f"gpt2agent-grok-{secrets.token_hex(12)}"


async def _browser_call(
    runner: Runner,
    argv: Sequence[str],
) -> ProcessResult:
    env = os.environ.copy()
    env.pop("XAI_API_KEY", None)
    env.pop("GROK_CODE_XAI_API_KEY", None)
    return await runner(
        argv,
        cwd=Path.cwd().resolve(),
        env=env,
        timeout_seconds=_BROWSER_TIMEOUT_SECONDS,
        max_output_bytes=_BROWSER_OUTPUT_BYTES,
    )


def _response_data(result: ProcessResult) -> dict[str, Any]:
    if result.returncode != 0:
        raise _error("GROK_WEB_FAILED")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _error("GROK_WEB_FAILED") from None
    if (
        not isinstance(payload, dict)
        or payload.get("success") is not True
        or not isinstance(payload.get("data"), dict)
    ):
        raise _error("GROK_WEB_FAILED")
    return payload["data"]


def _chrome_profile_from_ua(user_agent: Any) -> str | None:
    if not isinstance(user_agent, str) or len(user_agent) > 4_096:
        return None
    match = _CHROME_UA_RE.search(user_agent)
    if match is None:
        return None
    return browser_impersonation_by_major().get(int(match.group(1)))


def _default_browser_profile() -> str:
    profiles = browser_impersonation_by_major()
    if not profiles:  # pragma: no cover - dependency contract guard
        raise _error("GROK_WEB_FAILED")
    return profiles[max(profiles)]


def _normalize_expiry(value: Any) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 1
        or value > 2**53 - 1
    ):
        return None
    converted = int(value)
    return converted if converted > int(time.time()) else None


def _stored_browser_cookies(
    raw_cookies: Any,
    *,
    compatible_profile: str | None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_cookies, list) or len(raw_cookies) > 4_096:
        raise _error("GROK_WEB_FAILED")
    selected: dict[str, dict[str, Any]] = {}
    for raw in raw_cookies:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or name not in _IMPORTED_COOKIE_NAMES:
            continue
        if name == "cf_clearance" and compatible_profile is None:
            continue
        if name in selected:
            raise _error("GROK_WEB_FAILED")
        expires = _normalize_expiry(raw.get("expires"))
        value = raw.get("value")
        bounded_value = False
        if isinstance(value, str) and value:
            try:
                encoded_value = value.encode("utf-8")
            except UnicodeEncodeError:
                pass
            else:
                bounded_value = len(encoded_value) <= 16_384 and all(
                    ord(char) >= 32 and ord(char) != 127 for char in value
                )
        valid = not (
            raw.get("domain") not in {"grok.com", ".grok.com"}
            or raw.get("path") != "/"
            or raw.get("secure") is not True
            or raw.get("httpOnly") is not True
            or expires is None
            or not bounded_value
        )
        if not valid:
            if name not in _REQUIRED_COOKIE_NAMES:
                continue
            raise _error("GROK_WEB_FAILED")
        selected[name] = {
            "name": name,
            "domain": raw["domain"],
            "path": "/",
            "expires": expires,
            "secure": True,
            "http_only": True,
            "value": value,
        }
    if not _REQUIRED_COOKIE_NAMES.issubset(selected):
        raise _error("GROK_WEB_AUTH_MISSING")
    return [selected[name] for name in sorted(selected)]


async def import_browser_web_auth(
    path: Path,
    *,
    chrome_profile: str = "Default",
    runner: Runner = run_bounded_process,
    resolver: Resolver = shutil.which,
    session_factory: SessionFactory = _owned_session,
) -> dict[str, Any]:
    """Import only allowlisted Grok cookies from one uniquely owned session."""
    command = resolver("browser-use")
    if command is None:
        raise _error("GROK_WEB_FAILED")
    session = session_factory()
    if not isinstance(session, str) or _SESSION_RE.fullmatch(session) is None:
        raise _error("GROK_WEB_FAILED")
    if (
        not isinstance(chrome_profile, str)
        or not chrome_profile
        or len(chrome_profile) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in chrome_profile)
    ):
        raise _error("GROK_WEB_FAILED")

    base = [command, "--profile", chrome_profile, "--session", session]
    failure: GrokError | None = None
    final_status: dict[str, Any] | None = None
    try:
        opened = await _browser_call(
            runner,
            [
                command,
                "--headed",
                "--profile",
                chrome_profile,
                "--session",
                session,
                "--json",
                "open",
                _GROK_URL,
            ],
        )
        _response_data(opened)
        cookie_result = await _browser_call(
            runner,
            [*base, "--json", "cookies", "get", "--url", _GROK_URL],
        )
        cookie_data = _response_data(cookie_result)
        if not isinstance(cookie_data.get("cookies"), list):
            raise _error("GROK_WEB_FAILED")
        ua_result = await _browser_call(
            runner,
            [*base, "--json", "eval", "navigator.userAgent"],
        )
        user_agent = _response_data(ua_result).get("result")
        compatible_profile = _chrome_profile_from_ua(user_agent)
        cookies = _stored_browser_cookies(
            cookie_data.get("cookies"),
            compatible_profile=compatible_profile,
        )
        write_private_json(
            path,
            {
                "schema_version": 1,
                "source": "browser-use",
                "browser_impersonation": compatible_profile
                or _default_browser_profile(),
                "cookies": cookies,
            },
        )
        status = GrokWebAuthStore(path).status()
        if status.get("authenticated") is not True:
            failure = _error("GROK_WEB_AUTH_MISSING")
        else:
            final_status = status
    except GrokError as exc:
        failure = _error(exc.code)
    except (BoundedProcessError, OSError, RuntimeError, ValueError):
        failure = _error("GROK_WEB_FAILED")
    finally:
        try:
            closed = await _browser_call(runner, [*base, "close"])
            if closed.returncode != 0 and failure is None:
                failure = _error("GROK_WEB_FAILED")
        except (BoundedProcessError, OSError, RuntimeError, ValueError):
            if failure is None:
                failure = _error("GROK_WEB_FAILED")
    if failure is not None:
        raise failure from None
    if final_status is None:  # pragma: no cover - control-flow guard
        raise _error("GROK_WEB_FAILED")
    return final_status


def _manual_document(getpass_fn: Getpass) -> dict[str, Any]:
    values: dict[str, str] = {}
    failure: GrokError | None = None
    try:
        values["sso"] = getpass_fn("Grok sso cookie: ")
        values["sso-rw"] = getpass_fn("Grok sso-rw cookie: ")
    except (EOFError, KeyboardInterrupt, RuntimeError, StopIteration):
        failure = _error("GROK_WEB_AUTH_MISSING")
    if failure is not None:
        raise failure from None
    expires = int(time.time()) + _MANUAL_LIFETIME_SECONDS
    cookies = [
        {
            "name": name,
            "domain": ".grok.com",
            "path": "/",
            "expires": expires,
            "secure": True,
            "http_only": True,
            "value": value,
        }
        for name, value in sorted(values.items())
    ]
    return {
        "schema_version": 1,
        "source": "manual",
        "browser_impersonation": _default_browser_profile(),
        "cookies": cookies,
    }


async def _manual_web_auth(path: Path, getpass_fn: Getpass) -> dict[str, Any]:
    failure: GrokError | None = None
    try:
        write_private_json(path, _manual_document(getpass_fn))
        status = GrokWebAuthStore(path).status()
        if status.get("authenticated") is not True:
            failure = _error("GROK_WEB_AUTH_MISSING")
        else:
            return status
    except GrokError as exc:
        failure = _error(exc.code)
    except (OSError, RuntimeError, ValueError):
        failure = _error("GROK_WEB_FAILED")
    if failure is None:  # pragma: no cover - control-flow guard
        failure = _error("GROK_WEB_FAILED")
    raise failure from None


async def setup_web_auth(
    path: Path | None = None,
    *,
    manual: bool = False,
    chrome_profile: str = "Default",
    runner: Runner = run_bounded_process,
    resolver: Resolver = shutil.which,
    session_factory: SessionFactory = _owned_session,
    getpass_fn: Getpass = getpass.getpass,
) -> dict[str, Any]:
    """Run browser-assisted setup, or hidden manual input when unavailable."""
    auth_path = path or Path.home() / ".gpt2agent" / "grok-web-auth.json"
    if manual:
        return await _manual_web_auth(auth_path, getpass_fn)
    command = resolver("browser-use")
    if command is None:
        return await _manual_web_auth(auth_path, getpass_fn)
    return await import_browser_web_auth(
        auth_path,
        chrome_profile=chrome_profile,
        runner=runner,
        resolver=lambda _: command,
        session_factory=session_factory,
    )


def _lane_success() -> dict[str, str]:
    return {"status": "ok", "code": "OK"}


def _lane_failure(code: str) -> dict[str, str]:
    return {"status": "failed", "code": code}


async def run_grok_setup(
    *,
    refresh: bool = False,
    chrome_profile: str = "Default",
    manual: bool = False,
    auth_path: Path | None = None,
    build_probe: Probe | None = None,
    web_setup: Probe | None = None,
) -> dict[str, Any]:
    """Attempt Build and website setup independently and return a bounded receipt."""
    del refresh  # The setup command always imports current auth; refresh is explicit intent.
    if build_probe is None:
        build_client = GrokBuildClient(
            GrokBuildConfig.from_mapping({"roots": [str(Path.cwd())]})
        )
        build_probe = build_client.status
    if web_setup is None:

        async def default_web_setup() -> dict[str, Any]:
            return await setup_web_auth(
                auth_path,
                manual=manual,
                chrome_profile=chrome_profile,
            )

        web_setup = default_web_setup

    try:
        build_status = await build_probe()
        if build_status.get("installed") is not True:
            build = _lane_failure("GROK_BUILD_CLI_NOT_FOUND")
        elif build_status.get("authenticated") is not True:
            build = _lane_failure("GROK_BUILD_AUTH_MISSING")
        else:
            build = _lane_success()
    except GrokError as exc:
        build = _lane_failure(exc.code)
    except InputValidationError:
        build = _lane_failure("GROK_BUILD_FAILED")

    try:
        web_status = await web_setup()
        if web_status.get("authenticated") is True:
            web = _lane_success()
        else:
            web = _lane_failure("GROK_WEB_AUTH_MISSING")
    except GrokError as exc:
        web = _lane_failure(exc.code)
    except InputValidationError:
        web = _lane_failure("GROK_WEB_FAILED")

    successes = sum(lane["status"] == "ok" for lane in (build, web))
    status = "ok" if successes == 2 else "partial" if successes == 1 else "failed"
    return {"status": status, "build": build, "web": web}
