from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from gpt2agent._secure_file import write_private_json
from gpt2agent.grok_errors import GrokError
from gpt2agent.grok_web_auth import (
    GrokWebAuthStore,
    supported_browser_impersonations,
)


def _cookie(
    name: str,
    value: str,
    *,
    expires: int,
    domain: str = ".grok.com",
) -> dict[str, Any]:
    return {
        "name": name,
        "domain": domain,
        "path": "/",
        "expires": expires,
        "secure": True,
        "http_only": True,
        "value": value,
    }


def _payload(*cookies: dict[str, Any], profile: str = "chrome131") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "browser-use",
        "browser_impersonation": profile,
        "cookies": list(cookies),
    }


def _write_auth(path: Path, payload: dict[str, Any]) -> None:
    write_private_json(path, payload)


def test_supported_impersonations_come_from_exact_desktop_chrome_enums() -> None:
    profiles = supported_browser_impersonations()

    assert "chrome131" in profiles
    assert all(profile.removeprefix("chrome").isdigit() for profile in profiles)
    assert "chrome133a" not in profiles
    assert "chrome99_android" not in profiles


def test_snapshot_is_immutable_and_reloads_as_one_generation(tmp_path: Path) -> None:
    now = int(time.time())
    auth_path = tmp_path / "private" / "grok-web-auth.json"
    original_payload = _payload(
        _cookie("sso", "first-secret", expires=now + 3_600),
        _cookie("sso-rw", "first-rw-secret", expires=now + 7_200),
    )
    _write_auth(auth_path, original_payload)
    store = GrokWebAuthStore(auth_path)

    snapshot = store.snapshot()

    assert snapshot.generation == 0
    assert isinstance(snapshot.cookies, MappingProxyType)
    assert set(snapshot.cookies) == {"sso", "sso-rw"}
    assert snapshot.cookies["sso"] == "first-secret"
    with pytest.raises(TypeError):
        snapshot.cookies["sso"] = "mutation"  # type: ignore[index]

    replacement = original_payload | {
        "cookies": [
            cookie | {"value": "rotated-secret"}
            for cookie in original_payload["cookies"]
        ]
    }
    _write_auth(auth_path, replacement)
    next_snapshot = store.snapshot()

    assert store.auth_generation == 1
    assert next_snapshot.generation == 1
    assert next_snapshot.cookies["sso"] == "rotated-secret"
    assert snapshot.cookies["sso"] == "first-secret"


def test_snapshot_reloads_atomic_replacement_with_preserved_mtime(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    auth_path = tmp_path / "private" / "grok-web-auth.json"
    original = _payload(
        _cookie("sso", "first-secret", expires=now + 3_600),
        _cookie("sso-rw", "first-secret", expires=now + 3_600),
    )
    replacement = _payload(
        _cookie("sso", "later-secret", expires=now + 3_600),
        _cookie("sso-rw", "later-secret", expires=now + 3_600),
    )
    _write_auth(auth_path, original)
    original_stat = auth_path.stat()
    store = GrokWebAuthStore(auth_path)
    first = store.snapshot()

    _write_auth(auth_path, replacement)
    os.utime(
        auth_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    replacement_stat = auth_path.stat()

    assert replacement_stat.st_size == original_stat.st_size
    assert replacement_stat.st_mtime_ns == original_stat.st_mtime_ns
    assert replacement_stat.st_ino != original_stat.st_ino
    second = store.snapshot()
    assert first.cookies["sso"] == "first-secret"
    assert second.cookies["sso"] == "later-secret"
    assert second.generation == 1


def _invalid_payload_cases(now: int) -> list[tuple[str, Callable[[dict[str, Any]], None]]]:
    def set_schema(payload: dict[str, Any]) -> None:
        payload["schema_version"] = 2

    def drop_required(payload: dict[str, Any]) -> None:
        payload["cookies"].pop()

    def duplicate(payload: dict[str, Any]) -> None:
        payload["cookies"].append(dict(payload["cookies"][0]))

    def unknown_cookie(payload: dict[str, Any]) -> None:
        payload["cookies"].append(
            _cookie("session", "planted-secret", expires=now + 3_600)
        )

    def wrong_domain(payload: dict[str, Any]) -> None:
        payload["cookies"][0]["domain"] = ".example.com"

    def wrong_path(payload: dict[str, Any]) -> None:
        payload["cookies"][0]["path"] = "/rest"

    def insecure(payload: dict[str, Any]) -> None:
        payload["cookies"][0]["secure"] = False

    def not_http_only(payload: dict[str, Any]) -> None:
        payload["cookies"][0]["http_only"] = False

    def expired(payload: dict[str, Any]) -> None:
        payload["cookies"][0]["expires"] = now - 1

    def huge(payload: dict[str, Any]) -> None:
        payload["cookies"][0]["value"] = "x" * 16_385

    def raw_user_agent(payload: dict[str, Any]) -> None:
        payload["browser_impersonation"] = (
            "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36"
        )

    def suffixed_profile(payload: dict[str, Any]) -> None:
        payload["browser_impersonation"] = "chrome133a"

    def unknown_root_field(payload: dict[str, Any]) -> None:
        payload["identity"] = "planted-identity"

    def unknown_cookie_field(payload: dict[str, Any]) -> None:
        payload["cookies"][0]["same_site"] = "Lax"

    return [
        ("schema", set_schema),
        ("required", drop_required),
        ("duplicate", duplicate),
        ("unknown-cookie", unknown_cookie),
        ("domain", wrong_domain),
        ("path", wrong_path),
        ("secure", insecure),
        ("http-only", not_http_only),
        ("expired", expired),
        ("bounded", huge),
        ("raw-ua", raw_user_agent),
        ("profile", suffixed_profile),
        ("closed-root", unknown_root_field),
        ("closed-cookie", unknown_cookie_field),
    ]


@pytest.mark.parametrize(
    ("case", "mutate"),
    _invalid_payload_cases(int(time.time())),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_snapshot_rejects_invalid_closed_schema_without_secret_echo(
    tmp_path: Path,
    case: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    now = int(time.time())
    planted = f"planted-{case}-secret"
    payload = _payload(
        _cookie("sso", planted, expires=now + 3_600),
        _cookie("sso-rw", "planted-rw-secret", expires=now + 3_600),
    )
    mutate(payload)
    auth_path = tmp_path / "private" / "grok-web-auth.json"
    _write_auth(auth_path, payload)

    with pytest.raises(GrokError) as caught:
        GrokWebAuthStore(auth_path).snapshot()

    assert caught.value.code in {"GROK_WEB_AUTH_MISSING", "GROK_WEB_AUTH_EXPIRED"}
    assert planted not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_status_is_bounded_and_contains_no_cookie_or_identity_values(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    auth_path = tmp_path / "private" / "grok-web-auth.json"
    _write_auth(
        auth_path,
        _payload(
            _cookie("sso", "planted-sso-secret", expires=now + 3_600),
            _cookie("sso-rw", "planted-rw-secret", expires=now + 7_200),
            _cookie("grok_device_id", "planted-device-secret", expires=now + 8_000),
        ),
    )

    status = GrokWebAuthStore(auth_path).status()

    assert status == {
        "configured": True,
        "authenticated": True,
        "expires_at": now + 3_600,
        "cookie_names": ["grok_device_id", "sso", "sso-rw"],
    }
    rendered = repr(status)
    assert "planted" not in rendered
    assert "identity" not in rendered
    assert "browser_impersonation" not in rendered


def test_missing_status_uses_only_the_public_status_shape(tmp_path: Path) -> None:
    status = GrokWebAuthStore(tmp_path / "private" / "missing.json").status()

    assert status == {
        "configured": False,
        "authenticated": False,
        "expires_at": None,
        "cookie_names": [],
    }


def test_cached_snapshot_becomes_expired_without_a_file_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpt2agent import grok_web_auth

    now = int(time.time())
    auth_path = tmp_path / "private" / "grok-web-auth.json"
    _write_auth(
        auth_path,
        _payload(
            _cookie("sso", "planted-sso-secret", expires=now + 10),
            _cookie("sso-rw", "planted-rw-secret", expires=now + 10),
        ),
    )
    store = GrokWebAuthStore(auth_path)
    assert store.snapshot().generation == 0

    monkeypatch.setattr(grok_web_auth.time, "time", lambda: now + 11)

    with pytest.raises(GrokError) as caught:
        store.snapshot()
    assert caught.value.code == "GROK_WEB_AUTH_EXPIRED"
    assert store.status() == {
        "configured": True,
        "authenticated": False,
        "expires_at": None,
        "cookie_names": [],
    }
