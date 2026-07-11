#!/usr/bin/env python3
"""Create and verify a sanitized, exact-commit local account receipt."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_HEADERS = 128
MAX_RESPONSE_HEADER_BYTES = 64 * 1024
MAX_REDIRECTS = 3
REQUEST_TIMEOUT_SECONDS = 20
_ACCOUNT_ORIGIN = "https://chatgpt.com"
_MAX_ITEMS = 10_000
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_AUTH_BYTES = 1024 * 1024
_PRETAG_ARTIFACT_HEADROOM = timedelta(hours=72)
_MAX_LIVE_RECEIPT_AGE = timedelta(minutes=30)
_MAX_LIVE_PROBE_DURATION = timedelta(minutes=10)
_MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=1)
_PACKAGE_VERSION = "0.0.12"
_SCHEMA_VERSION = "4"
_VERIFIER_NAME = "gpt2agent-account-receipt"
_VERIFIER_VERSION = "6"
_GIT = Path("/usr/bin/git")
_MAX_GIT_BINDING_BYTES = 4096
_MAX_TOKEN_BYTES = 16 * 1024
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_RUN_ID = re.compile(r"[1-9][0-9]*\Z")
_ARTIFACT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ACCESS_TOKEN = re.compile(
    r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z"
)
_CLIENT_VERSION = "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad"
_CLIENT_BUILD = "5955942"
_CHROME_131_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_TRUSTED_PYTHON_VERSION = (3, 12, 13)
_TRUSTED_PYTHON_PLATFORM = "linux"
_TRUSTED_PYTHON_MACHINE = "x86_64"
_TRUSTED_DISTRIBUTIONS = {
    "certifi": "2026.6.17",
    "cffi": "2.1.0",
    "curl-cffi": "0.15.0",
    "markdown-it-py": "4.2.0",
    "mdurl": "0.1.2",
    "pip": "26.1.2",
    "pycparser": "3.0",
    "pygments": "2.20.0",
    "rich": "15.0.0",
}


class ReceiptError(ValueError):
    """The trusted-local gate violated its fail-closed contract."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("JSON object contains a duplicate key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("JSON contains a non-finite number")


def _loads_strict_json(payload: str | bytes) -> Any:
    return json.loads(
        payload,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )


@dataclass(frozen=True)
class ProbeSpec:
    """One fixed, shape-only v0.0.12 account probe."""

    category: str
    path: str
    query: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RawResponse:
    """Bounded response material returned by an injectable requester."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


@dataclass(frozen=True)
class _TrustedTransportRuntime:
    """Validated third-party runtime inserted into one isolated process."""

    curl_opt: Any
    requests: Any
    original_sys_path: tuple[str, ...]
    inserted_site: str | None


@dataclass(frozen=True)
class _ShapeCheck:
    shape: str
    item_count: int | None
    continue_site_catalog: bool | None = None


_RUNTIME_ADAPTER_CATEGORIES = (
    "chat_models",
    "work_models",
    "apps",
    "plugins",
    "installed_plugins",
    "background_jobs",
    "scheduled_automations",
    "sites_access",
    "site_catalog",
    "custom_gpts",
    "codex",
)


@dataclass(frozen=True)
class ProbeOutcome:
    """Only fixed, non-identifying fields may cross into a receipt."""

    route_category: str
    status_class: str
    shape: str
    item_count: int | None
    status: str
    continue_site_catalog: bool | None = None

    def receipt_record(self) -> dict[str, Any]:
        return {
            "route_category": self.route_category,
            "status_class": self.status_class,
            "shape": self.shape,
            "item_count": self.item_count,
            "status": self.status,
        }


PROBES = (
    ProbeSpec(
        "chat_models",
        "/backend-api/models",
        (("history_and_training_disabled", "false"),),
    ),
    ProbeSpec("work_models", "/backend-api/tpp/models/"),
    ProbeSpec("apps", "/backend-api/apps/list"),
    ProbeSpec(
        "plugins",
        "/backend-api/plugins/list",
        (("limit", "1"), ("scope", "USER")),
    ),
    ProbeSpec("installed_plugins", "/backend-api/plugins/installed"),
    ProbeSpec("background_jobs", "/backend-api/tasks", (("limit", "1"),)),
    ProbeSpec(
        "scheduled_automations",
        "/backend-api/automations",
        (("filter", "scheduled"),),
    ),
    ProbeSpec("sites_access", "/backend-api/websites/access"),
    ProbeSpec("site_catalog", "/backend-api/websites", (("limit", "1"),)),
    ProbeSpec("custom_gpts", "/backend-api/gizmos/snorlax/sidebar"),
    ProbeSpec("codex", "/backend-api/codex/environments"),
    ProbeSpec("projects_candidate", "/backend-api/projects"),
)

PLAN_PROBE = ProbeSpec(
    "plan_entitlement",
    "/backend-api/accounts/check/v4-2023-04-27",
)

_PROBE_BY_CATEGORY = {probe.category: probe for probe in PROBES}
_REQUEST_PROBES = (PLAN_PROBE, *PROBES)
_PROBE_BY_REQUEST = {(probe.path, probe.query): probe for probe in _REQUEST_PROBES}
_DENIED_PATH_MARKERS = (
    "voice",
    "realtime",
    "call",
    "session",
    "webrtc",
    "transcript",
)


def expected_probe(category: str) -> ProbeSpec:
    """Return a fixed probe without accepting an arbitrary route."""
    try:
        return _PROBE_BY_CATEGORY[category]
    except (KeyError, TypeError):
        raise ReceiptError("account probe is not permitted") from None


def _canonical_query(query: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if query is None:
        return ()
    if not isinstance(query, Mapping):
        raise ReceiptError("account probe is not permitted")
    pairs: list[tuple[str, str]] = []
    for key, value in query.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ReceiptError("account probe is not permitted")
        pairs.append((key, value))
    return tuple(sorted(pairs))


def validate_probe_request(
    method: str,
    path: str,
    query: Mapping[str, str] | None = None,
) -> ProbeSpec:
    """Fail closed unless the request exactly matches the reviewed GET table."""
    if method != "GET" or not isinstance(path, str):
        raise ReceiptError("account probe is not permitted")
    lowered = path.lower()
    conversation_body = lowered.startswith("/backend-api/conversation/") or (
        lowered.startswith("/backend-api/conversations/")
    )
    if conversation_body or any(marker in lowered for marker in _DENIED_PATH_MARKERS):
        raise ReceiptError("account probe is not permitted")
    try:
        return _PROBE_BY_REQUEST[(path, _canonical_query(query))]
    except KeyError:
        raise ReceiptError("account probe is not permitted") from None


def _url_for_probe(probe: ProbeSpec) -> str:
    suffix = f"?{urlencode(probe.query)}" if probe.query else ""
    return f"{_ACCOUNT_ORIGIN}{probe.path}{suffix}"


def probe_from_url(url: str) -> ProbeSpec:
    """Map a complete URL back to the exact reviewed request table."""
    try:
        parsed = urlparse(url)
        port = parsed.port
        query = (
            tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)))
            if parsed.query
            else ()
        )
    except (TypeError, ValueError):
        raise ReceiptError("account URL is not permitted") from None
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "chatgpt.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or len({key for key, _value in query}) != len(query)
    ):
        raise ReceiptError("account URL is not permitted")
    try:
        return _PROBE_BY_REQUEST[(parsed.path, query)]
    except KeyError:
        raise ReceiptError("account URL is not permitted") from None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    if not isinstance(headers, Mapping):
        raise ReceiptError("account response metadata is malformed")
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name.lower():
            if not isinstance(value, str):
                raise ReceiptError("account response metadata is malformed")
            return value
    return None


def _bounded_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        raise ReceiptError("account response metadata is malformed")
    result: dict[str, str] = {}
    normalized_names: set[str] = set()
    total_bytes = 0
    try:
        for index, (key, value) in enumerate(headers.items()):
            if (
                index >= MAX_RESPONSE_HEADERS
                or not isinstance(key, str)
                or not isinstance(value, str)
            ):
                raise ReceiptError("account response metadata is malformed")
            normalized = key.lower()
            if not key or normalized in normalized_names:
                raise ReceiptError("account response metadata is malformed")
            normalized_names.add(normalized)
            total_bytes += len(key.encode("utf-8")) + len(value.encode("utf-8")) + 4
            if total_bytes > MAX_RESPONSE_HEADER_BYTES:
                raise ReceiptError("account response metadata is malformed")
            result[key] = value
    except ReceiptError:
        raise
    except Exception:
        raise ReceiptError("account response metadata is malformed") from None
    return result


def _validate_response_size(response: RawResponse) -> dict[str, str]:
    if not isinstance(response.body, bytes):
        raise ReceiptError("account response body is malformed")
    headers = _bounded_response_headers(response.headers)
    declared = _header(headers, "Content-Length")
    if declared is not None:
        try:
            announced = int(declared)
        except ValueError:
            raise ReceiptError("account response size is invalid") from None
        if announced < 0:
            raise ReceiptError("account response size is invalid")
        if announced > MAX_RESPONSE_BYTES:
            raise ReceiptError("account response exceeds 4 MiB")
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise ReceiptError("account response exceeds 4 MiB")
    return headers


def _items_shape(items: Any, item_validator: Callable[[Any], bool]) -> _ShapeCheck:
    if not isinstance(items, list) or len(items) > _MAX_ITEMS:
        raise ReceiptError("account response minimum shape is malformed")
    if any(not item_validator(item) for item in items):
        raise ReceiptError("account response minimum shape is malformed")
    return _ShapeCheck(
        "valid_nonempty" if items else "valid_empty",
        len(items),
    )


def _usable_string(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 2048


def _object_with_string(item: Any, *fields: str) -> bool:
    return isinstance(item, dict) and any(_usable_string(item.get(field)) for field in fields)


def _models_shape(data: Any) -> _ShapeCheck:
    if not isinstance(data, dict):
        raise ReceiptError("account response minimum shape is malformed")
    return _items_shape(data.get("models"), lambda item: _object_with_string(item, "slug"))


def _apps_shape(data: Any) -> _ShapeCheck:
    if not isinstance(data, dict):
        raise ReceiptError("account response minimum shape is malformed")
    return _items_shape(
        data.get("apps"),
        lambda item: _usable_string(item) or _object_with_string(item, "id"),
    )


def _plugins_shape(data: Any) -> _ShapeCheck:
    if isinstance(data, list):
        return _items_shape(data, lambda item: _object_with_string(item, "id"))
    if not isinstance(data, dict) or not isinstance(data.get("pagination"), dict):
        raise ReceiptError("account response minimum shape is malformed")
    return _items_shape(
        data.get("plugins"),
        lambda item: _object_with_string(item, "id") and isinstance(item.get("release"), dict),
    )


def _installed_plugins_shape(data: Any) -> _ShapeCheck:
    if not isinstance(data, dict):
        raise ReceiptError("account response minimum shape is malformed")
    envelope = data.get("plugins")
    if isinstance(envelope, list):
        items = envelope
    elif isinstance(envelope, dict) and isinstance(envelope.get("page", {}), dict):
        if envelope.get("page", {}).get("has_more") is True:
            raise ReceiptError("account response minimum shape is malformed")
        items = envelope.get("results")
    else:
        raise ReceiptError("account response minimum shape is malformed")
    return _items_shape(items, lambda item: _object_with_string(item, "id"))


def _collection_shape(data: Any, field: str, *identity_fields: str) -> _ShapeCheck:
    if not isinstance(data, dict):
        raise ReceiptError("account response minimum shape is malformed")
    return _items_shape(
        data.get(field),
        lambda item: _object_with_string(item, *identity_fields),
    )


def _scheduled_shape(data: Any) -> _ShapeCheck:
    result = _collection_shape(data, "items", "id")
    if "cursor" in data and data["cursor"] is not None and not _usable_string(data["cursor"]):
        raise ReceiptError("account response minimum shape is malformed")
    return result


def _sites_access_shape(data: Any) -> _ShapeCheck:
    if not isinstance(data, dict):
        raise ReceiptError("account response minimum shape is malformed")
    for field in ("enabled", "custom_domains_enabled", "requires_workspace_slug"):
        if data.get(field) is not None and not isinstance(data.get(field), bool):
            raise ReceiptError("account response minimum shape is malformed")
    return _ShapeCheck(
        "valid_object",
        None,
        continue_site_catalog=data.get("enabled") is not False,
    )


def _site_catalog_shape(data: Any) -> _ShapeCheck:
    result = _collection_shape(data, "items", "id")
    if "cursor" in data and data["cursor"] is not None and not _usable_string(data["cursor"]):
        raise ReceiptError("account response minimum shape is malformed")
    return result


def _custom_gpts_shape(data: Any) -> _ShapeCheck:
    if not isinstance(data, dict):
        raise ReceiptError("account response minimum shape is malformed")

    def valid(item: Any) -> bool:
        if not isinstance(item, dict) or not isinstance(item.get("gizmo"), dict):
            return False
        return _object_with_string(item["gizmo"], "id", "short_url")

    return _items_shape(data.get("items"), valid)


def _codex_shape(data: Any) -> _ShapeCheck:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("environments")
    else:
        raise ReceiptError("account response minimum shape is malformed")
    return _items_shape(items, lambda item: _object_with_string(item, "id"))


_SHAPE_VALIDATORS: dict[str, Callable[[Any], _ShapeCheck]] = {
    "chat_models": _models_shape,
    "work_models": _models_shape,
    "apps": _apps_shape,
    "plugins": _plugins_shape,
    "installed_plugins": _installed_plugins_shape,
    "background_jobs": lambda data: _collection_shape(data, "tasks", "task_id", "id"),
    "scheduled_automations": _scheduled_shape,
    "sites_access": _sites_access_shape,
    "site_catalog": _site_catalog_shape,
    "custom_gpts": _custom_gpts_shape,
    "codex": _codex_shape,
}


def _decode_and_check_shape(
    probe: ProbeSpec,
    response: RawResponse,
) -> _ShapeCheck:
    content_type = (_header(response.headers, "Content-Type") or "").split(";", 1)[0].lower()
    if content_type != "application/json":
        raise ReceiptError("account response content type is not JSON")
    try:
        data = _loads_strict_json(response.body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReceiptError("account response is not valid JSON") from None
    try:
        validator = _SHAPE_VALIDATORS[probe.category]
        return validator(data)
    except KeyError:
        raise ReceiptError("account response contract requires review") from None


def _fetch_probe_response(
    probe: ProbeSpec,
    *,
    requester: Callable[..., RawResponse],
    auth_headers: Mapping[str, str],
) -> RawResponse:
    """Fetch one reviewed route with bounded exact-route redirects."""
    reviewed = validate_probe_request("GET", probe.path, dict(probe.query))
    if reviewed.category != probe.category:
        raise ReceiptError("account probe is not permitted")
    current_url = _url_for_probe(reviewed)
    try:
        request_headers = dict(auth_headers)
    except (TypeError, ValueError):
        raise ReceiptError("account authorization snapshot is malformed") from None
    request_headers["X-OpenAI-Target-Path"] = reviewed.path
    redirects = 0
    while True:
        try:
            response = requester(
                method="GET",
                url=current_url,
                headers=dict(request_headers),
                timeout=REQUEST_TIMEOUT_SECONDS,
                max_bytes=MAX_RESPONSE_BYTES,
            )
        except Exception:
            raise ReceiptError("account probe transport failed") from None
        if not isinstance(response, RawResponse):
            raise ReceiptError("account probe transport returned malformed metadata")
        if isinstance(response.status, bool) or not isinstance(response.status, int):
            raise ReceiptError("account response status is malformed")
        try:
            observed_probe = probe_from_url(response.url)
        except ReceiptError:
            raise ReceiptError("account response origin is not permitted") from None
        if observed_probe.category != reviewed.category:
            raise ReceiptError("account response route is not permitted")
        response_headers = _validate_response_size(response)
        response = RawResponse(
            status=response.status,
            headers=response_headers,
            body=response.body,
            url=response.url,
        )

        if response.status not in {301, 302, 303, 307, 308}:
            return response
        if redirects >= MAX_REDIRECTS:
            raise ReceiptError("account redirect limit exceeded")
        location = _header(response.headers, "Location")
        if location is None:
            raise ReceiptError("account redirect is malformed")
        try:
            next_url = urljoin(current_url, location)
            redirected_probe = probe_from_url(next_url)
        except ReceiptError:
            raise ReceiptError("account redirect is not permitted") from None
        if redirected_probe.category != reviewed.category:
            raise ReceiptError("account redirect is not permitted")
        current_url = next_url
        redirects += 1


def execute_probe(
    probe: ProbeSpec,
    *,
    requester: Callable[..., RawResponse],
    auth_headers: Mapping[str, str],
) -> ProbeOutcome:
    """Issue one bounded request and return a value-only-free outcome."""
    reviewed = validate_probe_request("GET", probe.path, dict(probe.query))
    response = _fetch_probe_response(
        reviewed,
        requester=requester,
        auth_headers=auth_headers,
    )

    if reviewed.category == "projects_candidate":
        if response.status not in {404, 405}:
            raise ReceiptError("candidate project route requires contract review")
        return ProbeOutcome(
            reviewed.category,
            "4xx",
            "not_applicable",
            None,
            "unsupported",
        )
    if not 200 <= response.status < 300:
        raise ReceiptError("account route did not satisfy the live gate")

    checked = _decode_and_check_shape(reviewed, response)
    status = (
        "unavailable"
        if reviewed.category == "sites_access" and checked.continue_site_catalog is False
        else "ok"
    )
    return ProbeOutcome(
        reviewed.category,
        "2xx",
        checked.shape,
        checked.item_count,
        status,
        checked.continue_site_catalog,
    )


def run_probe_sequence(
    *,
    requester: Callable[..., RawResponse],
    auth_headers: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Run the reviewed table serially, with only the Sites conditional."""
    records: list[dict[str, Any]] = []
    continue_site_catalog = True
    for probe in PROBES:
        if probe.category == "site_catalog" and not continue_site_catalog:
            records.append(
                ProbeOutcome(
                    probe.category,
                    "not_requested",
                    "not_requested",
                    None,
                    "not_requested",
                ).receipt_record()
            )
            continue
        outcome = execute_probe(
            probe,
            requester=requester,
            auth_headers=auth_headers,
        )
        records.append(outcome.receipt_record())
        if probe.category == "sites_access":
            continue_site_catalog = outcome.continue_site_catalog is not False
    return records


def parse_active_pro_entitlement(value: Any) -> str:
    """Accept active entitlements only when every active plan is Pro."""
    if not isinstance(value, dict):
        raise ReceiptError("authenticated account plan is invalid")
    accounts = value.get("accounts")
    if not isinstance(accounts, dict) or not accounts or len(accounts) > 100:
        raise ReceiptError("authenticated account plan is invalid")
    active_plans: list[str] = []
    for account in accounts.values():
        if not isinstance(account, dict):
            raise ReceiptError("authenticated account plan is invalid")
        entitlement = account.get("entitlement")
        if not isinstance(entitlement, dict):
            raise ReceiptError("authenticated account plan is invalid")
        plan = entitlement.get("subscription_plan")
        active = entitlement.get("has_active_subscription")
        if not isinstance(plan, str) or not plan or len(plan) > 128 or not isinstance(active, bool):
            raise ReceiptError("authenticated account plan is invalid")
        if active:
            active_plans.append(plan)
    if not active_plans or any(plan not in {"pro", "chatgptpro"} for plan in active_plans):
        raise ReceiptError("authenticated account plan does not match the release gate")
    return "pro"


def execute_plan_probe(
    *,
    requester: Callable[..., RawResponse],
    auth_headers: Mapping[str, str],
) -> str:
    """Measure the fixed entitlement route without retaining account values."""
    response = _fetch_probe_response(
        PLAN_PROBE,
        requester=requester,
        auth_headers=auth_headers,
    )
    if not 200 <= response.status < 300:
        raise ReceiptError("authenticated account plan could not be measured")
    content_type = (_header(response.headers, "Content-Type") or "").split(";", 1)[0].lower()
    if content_type != "application/json":
        raise ReceiptError("authenticated account plan response is invalid")
    try:
        payload = _loads_strict_json(response.body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReceiptError("authenticated account plan response is invalid") from None
    return parse_active_pro_entitlement(payload)


def canonical_json(value: Any) -> bytes:
    """Serialize evidence deterministically for an external SHA-256."""
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _require_oid(value: Any) -> str:
    if not isinstance(value, str) or not _HEX40.fullmatch(value):
        raise ReceiptError("declared source identity is invalid")
    return value


def _require_repository(value: Any) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise ReceiptError("candidate workflow identity is invalid")
    return value


def _require_positive_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ReceiptError("candidate workflow identity is invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _RUN_ID.fullmatch(value):
        parsed = int(value)
    else:
        raise ReceiptError("candidate workflow identity is invalid")
    if parsed < 1 or parsed > 10**20:
        raise ReceiptError("candidate workflow identity is invalid")
    return parsed


def _require_artifact_digest(value: Any) -> str:
    if not isinstance(value, str) or not _ARTIFACT_DIGEST.fullmatch(value):
        raise ReceiptError("candidate workflow identity is invalid")
    return value


def _trusted_git() -> Path:
    try:
        resolved = _GIT.resolve(strict=True)
        metadata = _GIT.lstat()
    except OSError:
        raise ReceiptError("trusted Git executable is unavailable") from None
    if (
        resolved != _GIT
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o6022
        or not metadata.st_mode & 0o111
    ):
        raise ReceiptError("trusted Git executable is invalid")
    for parent in (_GIT.parent, *_GIT.parent.parents):
        try:
            parent_metadata = parent.lstat()
        except OSError:
            raise ReceiptError("trusted Git parent path is unavailable") from None
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != 0
            or parent_metadata.st_mode & 0o022
        ):
            raise ReceiptError("trusted Git parent path is invalid")
    return _GIT


def _protected_git_directory(path: Path) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise ReceiptError("checkout Git administration binding is invalid") from None
    if (
        resolved != path
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_mode & 0o022
    ):
        raise ReceiptError("checkout Git administration binding is invalid")
    return resolved


def _read_git_binding_line(path: Path, *, prefix: bytes = b"") -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.getuid()}
            or metadata.st_mode & 0o022
            or metadata.st_nlink != 1
            or metadata.st_size < len(prefix) + 2
            or metadata.st_size > _MAX_GIT_BINDING_BYTES
        ):
            raise ReceiptError("checkout Git administration binding is invalid")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            payload = stream.read(_MAX_GIT_BINDING_BYTES + 1)
    except OSError:
        raise ReceiptError("checkout Git administration binding is invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        len(payload) > _MAX_GIT_BINDING_BYTES
        or not payload.startswith(prefix)
        or not payload.endswith(b"\n")
        or payload.count(b"\n") != 1
        or b"\x00" in payload
        or b"\r" in payload
    ):
        raise ReceiptError("checkout Git administration binding is invalid")
    value = os.fsdecode(payload[len(prefix) : -1])
    if not value:
        raise ReceiptError("checkout Git administration binding is invalid")
    return value


def _checkout_git_directory(checkout: Path) -> Path:
    """Resolve a clone directory or reciprocal linked-worktree gitfile binding."""
    marker = checkout / ".git"
    try:
        metadata = marker.lstat()
    except OSError:
        raise ReceiptError("checkout Git administration binding is invalid") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise ReceiptError("checkout Git administration binding is invalid")
    if stat.S_ISDIR(metadata.st_mode):
        return _protected_git_directory(marker)
    if not stat.S_ISREG(metadata.st_mode):
        raise ReceiptError("checkout Git administration binding is invalid")

    target_text = _read_git_binding_line(marker, prefix=b"gitdir: ")
    target = Path(target_text)
    if not target.is_absolute():
        raise ReceiptError("checkout Git administration binding is invalid")
    git_directory = _protected_git_directory(target)
    if git_directory != target:
        raise ReceiptError("checkout Git administration binding is invalid")

    backlink_text = _read_git_binding_line(git_directory / "gitdir")
    backlink = Path(backlink_text)
    try:
        resolved_backlink = backlink.resolve(strict=True)
    except OSError:
        raise ReceiptError("checkout Git administration binding is invalid") from None
    if not backlink.is_absolute() or backlink != resolved_backlink or backlink != marker:
        raise ReceiptError("checkout Git administration binding is invalid")
    return git_directory


def _git_output(
    checkout: Path,
    *args: str,
    git_directory: Path | None = None,
    pin_work_tree: bool = True,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> str:
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    try:
        _trusted_git()
        result = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(checkout),
                *((f"--git-dir={git_directory}",) if git_directory is not None else ()),
                *((f"--work-tree={checkout}",) if pin_work_tree else ()),
                *args,
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReceiptError("checkout Git inspection failed") from None
    if result.returncode not in allowed_returncodes:
        raise ReceiptError("checkout Git inspection failed")
    return result.stdout.strip()


def verify_checkout(
    checkout: Path,
    *,
    declared_commit: str,
    declared_tree: str,
) -> tuple[str, str]:
    """Require an exact repository root, full source identity, and clean tree."""
    commit = _require_oid(declared_commit)
    tree = _require_oid(declared_tree)
    checkout = Path(checkout)
    if checkout.is_symlink() or not checkout.is_dir():
        raise ReceiptError("checkout is not a regular directory")
    try:
        resolved = checkout.resolve(strict=True)
    except OSError:
        raise ReceiptError("checkout is not a regular directory") from None
    git_directory = _checkout_git_directory(resolved)
    discovered_git_directory_text = _git_output(
        resolved,
        "rev-parse",
        "--absolute-git-dir",
        pin_work_tree=False,
    )
    try:
        discovered_git_directory = Path(discovered_git_directory_text).resolve(strict=True)
    except OSError:
        raise ReceiptError("checkout Git administration binding is invalid") from None
    if discovered_git_directory != git_directory:
        raise ReceiptError("checkout Git administration binding is invalid")
    core_worktree_keys = _git_output(
        resolved,
        "config",
        "--name-only",
        "--get-regexp",
        r"^core\.worktree$",
        git_directory=git_directory,
        pin_work_tree=False,
        allowed_returncodes=(0, 1),
    )
    if core_worktree_keys:
        raise ReceiptError("checkout core.worktree configuration is forbidden")
    root_text = _git_output(
        resolved,
        "rev-parse",
        "--show-toplevel",
        git_directory=git_directory,
    )
    try:
        root = Path(root_text).resolve(strict=True)
    except OSError:
        raise ReceiptError("checkout Git root is invalid") from None
    if root != resolved:
        raise ReceiptError("checkout must name the repository root")
    index_state = _git_output(resolved, "ls-files", "-v", git_directory=git_directory)
    if any(
        len(line) >= 2
        and line[1] == " "
        and (line[0] == "S" or "a" <= line[0] <= "z")
        for line in index_state.splitlines()
    ):
        raise ReceiptError("checkout must be clean: index contains hidden tracked state")
    status = _git_output(
        resolved,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
        "--ignore-submodules=none",
        git_directory=git_directory,
    )
    if status:
        raise ReceiptError("checkout must be clean")
    actual_commit = _git_output(resolved, "rev-parse", "HEAD", git_directory=git_directory)
    actual_tree = _git_output(
        resolved,
        "rev-parse",
        "HEAD^{tree}",
        git_directory=git_directory,
    )
    if actual_commit != commit or actual_tree != tree:
        raise ReceiptError("checkout does not match the declared source")
    return actual_commit, actual_tree


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise ReceiptError("candidate artifact could not be read") from None
    return digest.hexdigest()


def _validate_artifact_filename(kind: str, filename: Any, package_version: str) -> str:
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ReceiptError("candidate artifact filename is invalid")
    version = re.escape(package_version)
    if kind == "wheel":
        pattern = rf"gpt2agent-{version}(?:-[0-9A-Za-z.]+)?-[0-9A-Za-z_.]+-[0-9A-Za-z_.]+-[0-9A-Za-z_.]+\.whl"
    else:
        pattern = rf"gpt2agent-{version}\.tar\.gz"
    if re.fullmatch(pattern, filename) is None:
        raise ReceiptError("candidate artifact filename is invalid")
    return filename


def collect_local_candidate_artifacts(
    dist: Path,
    *,
    package_version: str,
    source_commit: str,
    source_tree: str,
    repository: str,
    run_id: str,
    run_attempt: str,
    artifact_id: str,
    artifact_digest: str,
    artifact_size: str,
    artifact_expires_at: str,
    require_unexpired: bool = True,
) -> dict[str, Any]:
    """Hash the exact main-CI wheel and sdist without exposing their bytes."""
    if package_version != _PACKAGE_VERSION:
        raise ReceiptError("candidate package version is invalid")
    commit = _require_oid(source_commit)
    tree = _require_oid(source_tree)
    repository = _require_repository(repository)
    normalized_run_id = _require_positive_int(run_id)
    normalized_run_attempt = _require_positive_int(run_attempt)
    normalized_artifact_id = _require_positive_int(artifact_id)
    normalized_artifact_size = _require_positive_int(artifact_size)
    artifact_digest = _require_artifact_digest(artifact_digest)
    expires_at = _timestamp(artifact_expires_at)
    if (
        require_unexpired
        and expires_at <= datetime.now(timezone.utc) + _PRETAG_ARTIFACT_HEADROOM
    ):
        raise ReceiptError("candidate CI artifact is expired or expires too soon")
    dist = Path(dist)
    if dist.is_symlink() or not dist.is_dir():
        raise ReceiptError("candidate artifact directory is invalid")
    wheels = sorted(dist.glob("*.whl"), key=lambda path: path.name)
    sdists = sorted(dist.glob("*.tar.gz"), key=lambda path: path.name)
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReceiptError("expected exactly one candidate wheel and sdist")
    wheel, sdist = wheels[0], sdists[0]
    try:
        entries = set(dist.iterdir())
    except OSError:
        raise ReceiptError("candidate artifact directory could not be read") from None
    if entries != {wheel, sdist}:
        raise ReceiptError("candidate artifact set contains unexpected entries")
    if any(path.is_symlink() or not path.is_file() for path in (wheel, sdist)):
        raise ReceiptError("candidate artifacts must be regular files")
    wheel_name = _validate_artifact_filename("wheel", wheel.name, package_version)
    sdist_name = _validate_artifact_filename("sdist", sdist.name, package_version)
    return {
        "build_origin": "main_ci_package_artifact",
        "source": {"commit": commit, "tree": tree},
        "workflow": {
            "artifact_digest": artifact_digest,
            "artifact_expires_at": artifact_expires_at,
            "artifact_id": normalized_artifact_id,
            "artifact_name": (
                f"release-candidate-{commit}-{normalized_run_id}-{normalized_run_attempt}"
            ),
            "artifact_size": normalized_artifact_size,
            "event": "push",
            "job": "package",
            "ref": "refs/heads/main",
            "repository": repository,
            "run_attempt": normalized_run_attempt,
            "run_id": normalized_run_id,
            "workflow_file": ".github/workflows/ci.yml",
        },
        "wheel": {
            "filename": wheel_name,
            "sha256": _sha256(wheel),
            "size_bytes": wheel.stat().st_size,
        },
        "sdist": {
            "filename": sdist_name,
            "sha256": _sha256(sdist),
            "size_bytes": sdist.stat().st_size,
        },
    }


def _timestamp(value: Any) -> datetime:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
            value,
        )
        is None
    ):
        raise ReceiptError("receipt timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ReceiptError("receipt timestamp is invalid") from None
    if parsed.tzinfo != timezone.utc:
        raise ReceiptError("receipt timestamp is invalid")
    return parsed


def _utc_datetime_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_pretag_freshness(value: Mapping[str, Any]) -> None:
    """Reject replayed or clock-implausible live evidence before tagging."""
    started = _timestamp(value.get("started_at"))
    completed = _timestamp(value.get("completed_at"))
    now = _utc_datetime_now()
    if (
        completed < started
        or completed - started > _MAX_LIVE_PROBE_DURATION
        or completed > now + _MAX_FUTURE_CLOCK_SKEW
        or now - completed > _MAX_LIVE_RECEIPT_AGE
    ):
        raise ReceiptError("account receipt freshness is invalid")


def _exact_keys(value: Any, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ReceiptError("account receipt schema is invalid")
    return value


def _validate_artifact_record(
    value: Any,
    *,
    package_version: str,
    source: dict[str, str],
) -> None:
    artifacts = _exact_keys(
        value,
        {"build_origin", "source", "workflow", "wheel", "sdist"},
    )
    if artifacts["build_origin"] != "main_ci_package_artifact" or artifacts["source"] != source:
        raise ReceiptError("account receipt artifact binding is invalid")
    workflow = _exact_keys(
        artifacts["workflow"],
        {
            "artifact_digest",
            "artifact_expires_at",
            "artifact_id",
            "artifact_name",
            "artifact_size",
            "event",
            "job",
            "ref",
            "repository",
            "run_attempt",
            "run_id",
            "workflow_file",
        },
    )
    run_id = _require_positive_int(workflow["run_id"])
    run_attempt = _require_positive_int(workflow["run_attempt"])
    if (
        workflow["artifact_name"] != f"release-candidate-{source['commit']}-{run_id}-{run_attempt}"
        or workflow["job"] != "package"
        or workflow["workflow_file"] != ".github/workflows/ci.yml"
        or workflow["event"] != "push"
        or workflow["ref"] != "refs/heads/main"
    ):
        raise ReceiptError("account receipt artifact binding is invalid")
    _require_repository(workflow["repository"])
    _require_positive_int(workflow["artifact_id"])
    _require_positive_int(workflow["artifact_size"])
    _require_artifact_digest(workflow["artifact_digest"])
    _timestamp(workflow["artifact_expires_at"])
    for kind in ("wheel", "sdist"):
        record = _exact_keys(artifacts[kind], {"filename", "sha256", "size_bytes"})
        _validate_artifact_filename(kind, record["filename"], package_version)
        if (
            not isinstance(record["sha256"], str)
            or not _HEX64.fullmatch(record["sha256"])
            or isinstance(record["size_bytes"], bool)
            or not isinstance(record["size_bytes"], int)
            or record["size_bytes"] < 1
        ):
            raise ReceiptError("account receipt artifact binding is invalid")


def artifact_set_sha256(value: Any) -> str:
    """Digest one closed, source-and-workflow-bound candidate artifact set."""
    artifacts = _exact_keys(
        value,
        {"build_origin", "source", "workflow", "wheel", "sdist"},
    )
    source = _exact_keys(artifacts["source"], {"commit", "tree"})
    source = {"commit": _require_oid(source["commit"]), "tree": _require_oid(source["tree"])}
    _validate_artifact_record(
        artifacts,
        package_version=_PACKAGE_VERSION,
        source=source,
    )
    return hashlib.sha256(canonical_json(artifacts)).hexdigest()


def _validate_shape_results(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(PROBES):
        raise ReceiptError("account receipt shape results are invalid")
    expected_categories = [probe.category for probe in PROBES]
    categories: list[str] = []
    for record in value:
        result = _exact_keys(
            record,
            {"route_category", "status_class", "shape", "item_count", "status"},
        )
        category = result["route_category"]
        if not isinstance(category, str):
            raise ReceiptError("account receipt shape results are invalid")
        categories.append(category)
        count = result["item_count"]
        if isinstance(count, bool) or (count is not None and not isinstance(count, int)):
            raise ReceiptError("account receipt shape results are invalid")

        if category == "projects_candidate":
            expected = ("4xx", "not_applicable", None, "unsupported")
        elif category == "site_catalog" and result["status"] == "not_requested":
            expected = ("not_requested", "not_requested", None, "not_requested")
        elif category == "sites_access":
            allowed_statuses = {"ok"}
            if category == "sites_access":
                allowed_statuses.add("unavailable")
            if (
                result["status_class"] != "2xx"
                or result["shape"] != "valid_object"
                or count is not None
                or result["status"] not in allowed_statuses
            ):
                raise ReceiptError("account receipt shape results are invalid")
            continue
        else:
            if result["shape"] == "valid_empty":
                valid_count = count == 0
            elif result["shape"] == "valid_nonempty":
                valid_count = isinstance(count, int) and 1 <= count <= _MAX_ITEMS
            else:
                valid_count = False
            if result["status_class"] != "2xx" or result["status"] != "ok" or not valid_count:
                raise ReceiptError("account receipt shape results are invalid")
            continue
        actual = (
            result["status_class"],
            result["shape"],
            count,
            result["status"],
        )
        if actual != expected:
            raise ReceiptError("account receipt shape results are invalid")
    if categories != expected_categories:
        raise ReceiptError("account receipt shape results are invalid")
    site_access = value[expected_categories.index("sites_access")]
    site_catalog = value[expected_categories.index("site_catalog")]
    if (site_catalog["status"] == "not_requested") != (site_access["status"] == "unavailable"):
        raise ReceiptError("account receipt Sites result is inconsistent")
    return value


def _public_shape_results(value: Any) -> list[dict[str, Any]]:
    """Drop exact account collection counts from the public receipt."""
    return [
        {
            "route_category": record["route_category"],
            "status_class": record["status_class"],
            "shape": record["shape"],
            "status": record["status"],
        }
        for record in _validate_shape_results(value)
    ]


def _validate_public_shape_results(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(PROBES):
        raise ReceiptError("account receipt shape results are invalid")
    expected_categories = [probe.category for probe in PROBES]
    categories: list[str] = []
    for record in value:
        result = _exact_keys(
            record,
            {"route_category", "status_class", "shape", "status"},
        )
        category = result["route_category"]
        if not isinstance(category, str):
            raise ReceiptError("account receipt shape results are invalid")
        categories.append(category)
        if category == "projects_candidate":
            expected = ("4xx", "not_applicable", "unsupported")
        elif category == "site_catalog" and result["status"] == "not_requested":
            expected = ("not_requested", "not_requested", "not_requested")
        elif category == "sites_access":
            allowed_statuses = {"ok"}
            if category == "sites_access":
                allowed_statuses.add("unavailable")
            if (
                result["status_class"] != "2xx"
                or result["shape"] != "valid_object"
                or result["status"] not in allowed_statuses
            ):
                raise ReceiptError("account receipt shape results are invalid")
            continue
        else:
            if (
                result["status_class"] != "2xx"
                or result["shape"] not in {"valid_empty", "valid_nonempty"}
                or result["status"] != "ok"
            ):
                raise ReceiptError("account receipt shape results are invalid")
            continue
        actual = (
            result["status_class"],
            result["shape"],
            result["status"],
        )
        if actual != expected:
            raise ReceiptError("account receipt shape results are invalid")
    if categories != expected_categories:
        raise ReceiptError("account receipt shape results are invalid")
    site_access = value[expected_categories.index("sites_access")]
    site_catalog = value[expected_categories.index("site_catalog")]
    if (site_catalog["status"] == "not_requested") != (site_access["status"] == "unavailable"):
        raise ReceiptError("account receipt Sites result is inconsistent")
    return value


def _counts(
    shape_results: list[dict[str, Any]],
    adapter_counts: Any,
) -> dict[str, int]:
    results = _validate_public_shape_results(shape_results)
    requested = sum(record["status"] != "not_requested" for record in results)
    result = {
        "routes_declared": len(PROBES),
        "routes_requested": requested,
        "routes_passed": requested,
    }
    result.update(_validate_adapter_counts(adapter_counts, results, public=True))
    return result


def _validate_adapter_counts(
    value: Any,
    shape_results: list[dict[str, Any]],
    *,
    public: bool = False,
) -> dict[str, int]:
    fields = {
        "adapters_declared",
        "adapters_exercised",
        "adapters_passed",
        "adapters_not_requested",
    }
    counts = _exact_keys(value, fields)
    if any(isinstance(count, bool) or not isinstance(count, int) for count in counts.values()):
        raise ReceiptError("offline runtime adapter counts are invalid")
    if public:
        _validate_public_shape_results(shape_results)
    else:
        _validate_shape_results(shape_results)
    expected = _offline_adapter_counts()
    if counts != expected:
        raise ReceiptError("offline runtime adapter counts are invalid")
    return counts


def _offline_adapter_counts() -> dict[str, int]:
    """Return the fixed main-CI corpus evidence represented by schema v4."""
    declared = len(_RUNTIME_ADAPTER_CATEGORIES)
    return {
        "adapters_declared": declared,
        "adapters_exercised": declared,
        "adapters_passed": declared,
        "adapters_not_requested": 0,
    }


def validate_receipt(receipt: Any) -> None:
    """Reject unknown fields so account values cannot hitchhike in evidence."""
    document = _exact_keys(
        receipt,
        {
            "schema_version",
            "verifier",
            "package_version",
            "plan_class",
            "started_at",
            "completed_at",
            "source",
            "local_candidate_artifacts",
            "adapter_status",
            "counts",
            "shape_results",
        },
    )
    if (
        document["schema_version"] != _SCHEMA_VERSION
        or document["package_version"] != _PACKAGE_VERSION
    ):
        raise ReceiptError("account receipt version is invalid")
    verifier = _exact_keys(document["verifier"], {"name", "version"})
    if verifier != {"name": _VERIFIER_NAME, "version": _VERIFIER_VERSION}:
        raise ReceiptError("account receipt verifier is invalid")
    if document["plan_class"] != "pro" or document["adapter_status"] != "passed":
        raise ReceiptError("account receipt release status is invalid")
    started = _timestamp(document["started_at"])
    completed = _timestamp(document["completed_at"])
    if completed < started:
        raise ReceiptError("account receipt timestamps are inconsistent")
    source = _exact_keys(document["source"], {"commit", "tree"})
    source = {"commit": _require_oid(source["commit"]), "tree": _require_oid(source["tree"])}
    _validate_artifact_record(
        document["local_candidate_artifacts"],
        package_version=document["package_version"],
        source=source,
    )
    shape_results = _validate_public_shape_results(document["shape_results"])
    if not isinstance(document["counts"], dict):
        raise ReceiptError("account receipt counts are invalid")
    adapter_counts = {
        field: document["counts"].get(field)
        for field in (
            "adapters_declared",
            "adapters_exercised",
            "adapters_passed",
            "adapters_not_requested",
        )
    }
    if document["counts"] != _counts(shape_results, adapter_counts):
        raise ReceiptError("account receipt counts are invalid")


def build_receipt(
    *,
    package_version: str,
    plan_class: str,
    started_at: str,
    completed_at: str,
    source_commit: str,
    source_tree: str,
    local_candidate_artifacts: dict[str, Any],
    adapter_status: str,
    adapter_counts: dict[str, int],
    shape_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a closed-schema receipt from sanitized probe summaries."""
    source = {"commit": source_commit, "tree": source_tree}
    public_results = _public_shape_results(shape_results)
    receipt = {
        "schema_version": _SCHEMA_VERSION,
        "verifier": {"name": _VERIFIER_NAME, "version": _VERIFIER_VERSION},
        "package_version": package_version,
        "plan_class": plan_class,
        "started_at": started_at,
        "completed_at": completed_at,
        "source": source,
        "local_candidate_artifacts": local_candidate_artifacts,
        "adapter_status": adapter_status,
        "counts": _counts(public_results, adapter_counts),
        "shape_results": public_results,
    }
    validate_receipt(receipt)
    return receipt


def write_receipt(path: Path, receipt: dict[str, Any]) -> str:
    """Create one mode-0600 canonical receipt without overwriting evidence."""
    validate_receipt(receipt)
    payload = canonical_json(receipt)
    path = Path(path)
    if not path.parent.is_dir():
        raise ReceiptError("receipt output directory does not exist")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise ReceiptError("receipt output must be a new regular file") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ReceiptError("receipt output could not be written") from None
    return hashlib.sha256(payload).hexdigest()


def _project_version(checkout: Path) -> str:
    try:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10 package dependency
            import tomli as tomllib
        data = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))
        version = data["project"]["version"]
    except (OSError, KeyError, TypeError, ValueError):
        raise ReceiptError("checkout package version could not be verified") from None
    if version != _PACKAGE_VERSION:
        raise ReceiptError("checkout package version does not match the receipt")
    return version


def _read_receipt(path: Path) -> tuple[dict[str, Any], bytes]:
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ReceiptError("account receipt must be a protected regular file") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise ReceiptError("account receipt must be a protected regular file")
        if metadata.st_size > _MAX_RECEIPT_BYTES:
            raise ReceiptError("account receipt is too large")
        with os.fdopen(descriptor, "rb", closefd=True) as receipt_file:
            descriptor = -1
            payload = receipt_file.read(_MAX_RECEIPT_BYTES + 1)
        if len(payload) > _MAX_RECEIPT_BYTES:
            raise ReceiptError("account receipt is too large")
    except OSError:
        raise ReceiptError("account receipt could not be read") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        receipt = _loads_strict_json(payload)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReceiptError("account receipt is not valid JSON") from None
    if canonical_json(receipt) != payload:
        raise ReceiptError("account receipt is not canonical JSON")
    validate_receipt(receipt)
    return receipt, payload


def verify_receipt_file(
    receipt_path: Path,
    *,
    checkout: Path,
    dist: Path,
    declared_commit: str,
    declared_tree: str,
    expected_sha256: str,
    ci_repository: str,
    ci_run_id: str,
    ci_run_attempt: str,
    ci_artifact_id: str,
    ci_artifact_digest: str,
    ci_artifact_size: str,
    ci_artifact_expires_at: str,
) -> str:
    """Bind canonical evidence to current clean source and artifact bytes."""
    receipt, payload = _read_receipt(receipt_path)
    if not isinstance(expected_sha256, str) or not _HEX64.fullmatch(expected_sha256):
        raise ReceiptError("expected receipt digest is invalid")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        raise ReceiptError("account receipt digest does not match")
    _validate_pretag_freshness(receipt)
    commit = _require_oid(declared_commit)
    tree = _require_oid(declared_tree)
    if receipt["source"] != {"commit": commit, "tree": tree}:
        raise ReceiptError("account receipt does not match the declared source")
    verify_checkout(checkout, declared_commit=commit, declared_tree=tree)
    package_version = _project_version(Path(checkout))
    current_artifacts = collect_local_candidate_artifacts(
        dist,
        package_version=package_version,
        source_commit=commit,
        source_tree=tree,
        repository=ci_repository,
        run_id=ci_run_id,
        run_attempt=ci_run_attempt,
        artifact_id=ci_artifact_id,
        artifact_digest=ci_artifact_digest,
        artifact_size=ci_artifact_size,
        artifact_expires_at=ci_artifact_expires_at,
    )
    if receipt["local_candidate_artifacts"] != current_artifacts:
        raise ReceiptError("candidate artifact bytes do not match the account receipt")
    return digest


def _release_tag_metadata_module() -> Any:
    module_path = Path(__file__).resolve(strict=True).with_name("release_tag_metadata.py")
    if module_path.is_symlink() or not module_path.is_file():
        raise ReceiptError("release tag metadata verifier is unavailable")
    try:
        specification = importlib.util.spec_from_file_location(
            "_gpt2agent_release_tag_metadata",
            module_path,
        )
        if specification is None or specification.loader is None:
            raise ReceiptError("release tag metadata verifier is unavailable")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
    except ReceiptError:
        raise
    except Exception:
        raise ReceiptError("release tag metadata verifier is unavailable") from None
    return module


def prepare_tag_request(
    receipt_path: Path,
    *,
    checkout: Path,
    dist: Path,
    output: Path,
    tag: str,
    declared_commit: str,
    declared_tree: str,
    expected_sha256: str,
    ci_repository: str,
    ci_run_id: str,
    ci_run_attempt: str,
    ci_artifact_id: str,
    ci_artifact_digest: str,
    ci_artifact_size: str,
    ci_artifact_expires_at: str,
) -> None:
    """Revalidate the closed receipt and atomically prepare one tag API request."""
    checkout = Path(checkout)
    dist = Path(dist)
    output = Path(output)
    if tag != f"v{_PACKAGE_VERSION}":
        raise ReceiptError("release tag does not match the receipt version")
    if (
        not _outside_checkout(output, checkout)
        or _inside_candidate_dist(output, dist)
        or output.exists()
        or output.is_symlink()
    ):
        raise ReceiptError("release tag request must be a new regular file")
    parent = output.parent
    try:
        parent_metadata = parent.lstat()
    except OSError:
        raise ReceiptError("release tag request directory is invalid") from None
    if (
        not output.is_absolute()
        or stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or stat.S_IMODE(parent_metadata.st_mode) & 0o077
    ):
        raise ReceiptError("release tag request directory is invalid")

    digest = verify_receipt_file(
        receipt_path,
        checkout=checkout,
        dist=dist,
        declared_commit=declared_commit,
        declared_tree=declared_tree,
        expected_sha256=expected_sha256,
        ci_repository=ci_repository,
        ci_run_id=ci_run_id,
        ci_run_attempt=ci_run_attempt,
        ci_artifact_id=ci_artifact_id,
        ci_artifact_digest=ci_artifact_digest,
        ci_artifact_size=ci_artifact_size,
        ci_artifact_expires_at=ci_artifact_expires_at,
    )
    receipt, receipt_payload = _read_receipt(receipt_path)
    if hashlib.sha256(receipt_payload).hexdigest() != digest:
        raise ReceiptError("account receipt changed during tag preparation")
    _validate_pretag_freshness(receipt)
    artifacts = receipt["local_candidate_artifacts"]
    workflow = artifacts["workflow"]
    tag_metadata = _release_tag_metadata_module()
    try:
        metadata = tag_metadata.build_metadata(
            repository=workflow["repository"],
            tag=tag,
            version=receipt["package_version"],
            commit=receipt["source"]["commit"],
            tree=receipt["source"]["tree"],
            receipt_sha256=digest,
            artifact_set_sha256=artifact_set_sha256(artifacts),
            candidate_run_id=workflow["run_id"],
            candidate_run_attempt=workflow["run_attempt"],
            candidate_artifact_id=workflow["artifact_id"],
            candidate_artifact_digest=workflow["artifact_digest"],
            candidate_artifact_size=workflow["artifact_size"],
            candidate_artifact_expires_at=workflow["artifact_expires_at"],
        )
        payload = tag_metadata.render_github_tag_request(metadata)
    except Exception:
        raise ReceiptError("release tag request metadata is invalid") from None

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    created = False
    try:
        descriptor = os.open(output, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        if created:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
        raise ReceiptError("release tag request could not be written") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _account_token_from_file(path: Path, *, codex: bool) -> str | None:
    path = Path(path)
    descriptor = -1
    try:
        if path.is_symlink():
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 1
            or metadata.st_size > _MAX_AUTH_BYTES
            or (os.name == "posix" and metadata.st_uid != os.getuid())
            or (os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077)
        ):
            return None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(_MAX_AUTH_BYTES + 1)
        if len(raw) > _MAX_AUTH_BYTES:
            return None
        value = _loads_strict_json(raw)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not isinstance(value, dict):
        return None
    tokens = value.get("tokens")
    nested = tokens.get("access_token") if isinstance(tokens, dict) else None
    token = (
        nested
        if codex
        else value.get("token") or value.get("access_token") or nested
    )
    if (
        not isinstance(token, str)
        or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
        or _ACCESS_TOKEN.fullmatch(token) is None
    ):
        return None
    return token


def _reviewed_account_access_token() -> str:
    try:
        operator_home = Path.home()
    except RuntimeError:
        raise ReceiptError("reviewed ChatGPT account auth material is unavailable") from None
    configured_codex_home = os.environ.get("CODEX_HOME")
    codex_home = (
        Path(configured_codex_home)
        if configured_codex_home
        else operator_home / ".codex"
    )
    token = _account_token_from_file(codex_home / "auth.json", codex=True)
    if token is None:
        token = _account_token_from_file(
            operator_home / ".gpt2agent" / "token.json",
            codex=False,
        )
    if token is None:
        raise ReceiptError("reviewed ChatGPT account auth material is unavailable")
    return token


class CurlCffiRequester:
    """Streaming, no-auto-redirect requester owned by the trusted verifier."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: int,
        max_bytes: int,
    ) -> RawResponse:
        if method != "GET" or timeout != REQUEST_TIMEOUT_SECONDS or max_bytes != MAX_RESPONSE_BYTES:
            raise ReceiptError("account transport request is not permitted")
        probe = probe_from_url(url)
        expected_header_names = {
            "Accept",
            "Authorization",
            "OAI-Client-Build-Number",
            "OAI-Client-Version",
            "OAI-Device-Id",
            "OAI-Language",
            "OAI-Session-Id",
            "Origin",
            "Referer",
            "User-Agent",
            "X-OpenAI-Target-Path",
        }
        try:
            header_snapshot = dict(headers)
            device_id = uuid.UUID(header_snapshot["OAI-Device-Id"])
            session_id = uuid.UUID(header_snapshot["OAI-Session-Id"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ReceiptError("account transport headers are invalid") from None
        authorization = header_snapshot.get("Authorization")
        if (
            set(header_snapshot) != expected_header_names
            or not isinstance(authorization, str)
            or not authorization.startswith("Bearer ")
            or len(authorization.removeprefix("Bearer ").encode("utf-8")) > _MAX_TOKEN_BYTES
            or _ACCESS_TOKEN.fullmatch(authorization.removeprefix("Bearer ")) is None
            or device_id.version != 4
            or session_id.version != 4
            or str(device_id) != header_snapshot["OAI-Device-Id"]
            or str(session_id) != header_snapshot["OAI-Session-Id"]
            or header_snapshot["Accept"] != "*/*"
            or header_snapshot["OAI-Client-Build-Number"] != _CLIENT_BUILD
            or header_snapshot["OAI-Client-Version"] != _CLIENT_VERSION
            or header_snapshot["OAI-Language"] != "en-US"
            or header_snapshot["Origin"] != _ACCOUNT_ORIGIN
            or header_snapshot["Referer"] != f"{_ACCOUNT_ORIGIN}/"
            or header_snapshot["User-Agent"] != _CHROME_131_UA
            or header_snapshot["X-OpenAI-Target-Path"] != probe.path
        ):
            raise ReceiptError("account transport headers are invalid")
        try:
            response = self._session.get(
                url,
                headers=header_snapshot,
                timeout=timeout,
                allow_redirects=False,
                max_redirects=0,
                proxy="",
                verify=True,
                stream=True,
                discard_cookies=True,
                default_headers=False,
                accept_encoding=None,
            )
        except Exception:
            raise ReceiptError("account probe transport failed") from None
        try:
            response_headers = _bounded_response_headers(getattr(response, "headers", {}))
            declared = _header(response_headers, "Content-Length")
            if declared is not None:
                try:
                    announced = int(declared)
                except ValueError:
                    raise ReceiptError("account response size is invalid") from None
                if announced < 0 or announced > max_bytes:
                    raise ReceiptError("account response exceeds 4 MiB")
            status = getattr(response, "status_code", None)
            if isinstance(status, bool) or not isinstance(status, int):
                raise ReceiptError("account response status is malformed")
            chunks: list[bytes] = []
            total = 0
            if 200 <= status < 300:
                try:
                    iterator = response.iter_content(chunk_size=64 * 1024)
                    for chunk in iterator:
                        if not isinstance(chunk, bytes):
                            raise ReceiptError("account response body is malformed")
                        total += len(chunk)
                        if total > max_bytes:
                            raise ReceiptError("account response exceeds 4 MiB")
                        chunks.append(chunk)
                except ReceiptError:
                    raise
                except Exception:
                    raise ReceiptError("account probe transport failed") from None
            response_url = str(getattr(response, "url", ""))
            return RawResponse(
                status=status,
                headers=response_headers,
                body=b"".join(chunks),
                url=response_url,
            )
        finally:
            try:
                response.close()
            except Exception:
                pass


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_lstat(path: Path, *, owner: int, directory: bool | None = None) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != owner:
        return False
    if metadata.st_mode & 0o022:
        return False
    if directory is True and not stat.S_ISDIR(metadata.st_mode):
        return False
    if directory is False and not stat.S_ISREG(metadata.st_mode):
        return False
    return True


def _secure_path_chain(path: Path, root: Path, *, owner: int) -> bool:
    if not path.is_absolute() or not root.is_absolute():
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    if not _safe_lstat(current, owner=owner, directory=True):
        return False
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        is_last = index == len(parts) - 1
        if not _safe_lstat(current, owner=owner, directory=None if is_last else True):
            return False
    return True


def _path_has_secure_ancestors(path: Path, *, current_uid: int) -> bool:
    if not path.is_absolute():
        return False
    current = path
    while True:
        try:
            metadata = current.lstat()
        except OSError:
            return False
        if (
            stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid not in (0, current_uid)
            or metadata.st_mode & 0o022
        ):
            return False
        parent = current.parent
        if parent == current:
            return stat.S_ISDIR(metadata.st_mode)
        current = parent


def _dependency_origin_is_trusted(origin: Path, forbidden_roots: tuple[Path, ...]) -> bool:
    if not origin.is_absolute() or origin.is_symlink():
        return False
    try:
        resolved = origin.resolve(strict=True)
        metadata = origin.lstat()
    except OSError:
        return False
    if (
        resolved != origin
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        return False
    current = origin.parent
    while current != current.parent:
        try:
            parent_metadata = current.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(parent_metadata.st_mode) or parent_metadata.st_mode & 0o022:
            return False
        current = current.parent
    for root in forbidden_roots:
        try:
            resolved.relative_to(Path(root).resolve(strict=True))
        except (OSError, ValueError):
            continue
        return False
    return True


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _trusted_runtime_identity_is_exact(
    *,
    implementation: str,
    version: tuple[int, int, int],
    platform_name: str,
    machine: str,
) -> bool:
    return (
        implementation == "cpython"
        and version == _TRUSTED_PYTHON_VERSION
        and platform_name == _TRUSTED_PYTHON_PLATFORM
        and machine == _TRUSTED_PYTHON_MACHINE
    )


def _require_isolated_runtime_site(
    trusted_site_packages: Path,
    forbidden_roots: tuple[Path, ...],
) -> tuple[Path, Path]:
    flags = sys.flags
    if (
        not _trusted_runtime_identity_is_exact(
            implementation=sys.implementation.name,
            version=sys.version_info[:3],
            platform_name=sys.platform,
            machine=os.uname().machine,
        )
        or flags.isolated != 1
        or flags.no_site != 1
        or flags.no_user_site != 1
        or not flags.safe_path
    ):
        raise ReceiptError("trusted account runtime is not isolated")

    executable = Path(sys.executable)
    if not executable.is_absolute():
        raise ReceiptError("trusted account runtime is invalid")
    venv = executable.parent.parent
    owner = os.getuid()
    if (
        not _path_has_secure_ancestors(executable, current_uid=owner)
        or not _path_has_secure_ancestors(venv, current_uid=owner)
        or not _safe_lstat(venv, owner=owner, directory=True)
    ):
        raise ReceiptError("trusted account runtime is invalid")
    try:
        venv_mode = stat.S_IMODE(venv.lstat().st_mode)
    except OSError:
        raise ReceiptError("trusted account runtime is invalid") from None
    if (
        venv_mode & 0o077
        or not _secure_path_chain(executable, venv, owner=owner)
        or not _safe_lstat(executable, owner=owner, directory=False)
    ):
        raise ReceiptError("trusted account runtime is invalid")

    configuration = venv / "pyvenv.cfg"
    if not _secure_path_chain(configuration, venv, owner=owner) or not _safe_lstat(
        configuration, owner=owner, directory=False
    ):
        raise ReceiptError("trusted account runtime is invalid")
    try:
        configuration_bytes = configuration.read_bytes()
    except OSError:
        raise ReceiptError("trusted account runtime is invalid") from None
    if (
        len(configuration_bytes) > 4096
        or b"include-system-site-packages = false" not in configuration_bytes.lower()
    ):
        raise ReceiptError("trusted account runtime is invalid")

    expected_site = venv / "lib" / "python3.12" / "site-packages"
    supplied_site = Path(trusted_site_packages)
    if not supplied_site.is_absolute() or supplied_site != expected_site:
        raise ReceiptError("trusted account runtime site-packages is invalid")
    if not _secure_path_chain(expected_site, venv, owner=owner) or not _safe_lstat(
        expected_site, owner=owner, directory=True
    ):
        raise ReceiptError("trusted account runtime site-packages is invalid")

    resolved_venv = venv.resolve(strict=True)
    resolved_site = expected_site.resolve(strict=True)
    for root in forbidden_roots:
        try:
            resolved_root = Path(root).resolve(strict=True)
        except OSError:
            raise ReceiptError("trusted account runtime boundary is invalid") from None
        if _path_is_within(resolved_venv, resolved_root) or _path_is_within(
            resolved_root, resolved_venv
        ):
            raise ReceiptError("trusted account runtime overlaps candidate data")

    base = Path(sys.base_prefix)
    if not base.is_absolute() or sys.prefix != sys.base_prefix:
        raise ReceiptError("trusted account runtime base is invalid")
    try:
        base_metadata = base.lstat()
    except OSError:
        raise ReceiptError("trusted account runtime base is invalid") from None
    if (
        not stat.S_ISDIR(base_metadata.st_mode)
        or stat.S_ISLNK(base_metadata.st_mode)
        or base_metadata.st_uid not in (0, owner)
        or base_metadata.st_mode & 0o022
        or not _path_has_secure_ancestors(base, current_uid=owner)
    ):
        raise ReceiptError("trusted account runtime base is invalid")
    base_owner = base_metadata.st_uid
    try:
        resolved_base = base.resolve(strict=True)
    except OSError:
        raise ReceiptError("trusted account runtime base is invalid") from None
    if resolved_base != base or not _secure_path_chain(base, base, owner=base_owner):
        raise ReceiptError("trusted account runtime base is invalid")
    for root in forbidden_roots:
        try:
            resolved_root = Path(root).resolve(strict=True)
        except OSError:
            raise ReceiptError("trusted account runtime boundary is invalid") from None
        if _path_is_within(resolved_base, resolved_root) or _path_is_within(
            resolved_root, resolved_base
        ):
            raise ReceiptError("trusted account runtime base overlaps candidate data")
    base_library = base / "lib" / "python3.12"
    base_zip = base / "lib" / "python312.zip"
    if not _secure_path_chain(base_library, base, owner=base_owner) or not _safe_lstat(
        base_library, owner=base_owner, directory=True
    ):
        raise ReceiptError("trusted account runtime path is invalid")
    for entry in sys.path:
        candidate = Path(entry)
        if not candidate.is_absolute():
            raise ReceiptError("trusted account runtime path is invalid")
        if candidate == base_zip:
            if base_zip.exists() and (
                not _secure_path_chain(base_zip, base, owner=base_owner)
                or not _safe_lstat(base_zip, owner=base_owner, directory=False)
            ):
                raise ReceiptError("trusted account runtime path is invalid")
            continue
        try:
            resolved_entry = candidate.resolve(strict=True)
            resolved_library = base_library.resolve(strict=True)
        except OSError:
            raise ReceiptError("trusted account runtime path is invalid") from None
        if not _path_is_within(resolved_entry, resolved_library):
            raise ReceiptError("trusted account runtime path is invalid")
        current = resolved_entry
        while current != base:
            try:
                metadata = current.lstat()
            except OSError:
                raise ReceiptError("trusted account runtime path is invalid") from None
            if (
                stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != base_owner
                or metadata.st_mode & 0o022
            ):
                raise ReceiptError("trusted account runtime path is invalid")
            current = current.parent

    return resolved_venv, resolved_site


def _require_exact_distribution_closure(site: Path, venv: Path) -> None:
    observed: dict[str, str] = {}
    distributions: dict[str, importlib.metadata.Distribution] = {}
    try:
        candidates = list(importlib.metadata.distributions(path=[str(site)]))
    except Exception:
        raise ReceiptError("trusted account dependency metadata is invalid") from None
    for distribution in candidates:
        try:
            raw_name = distribution.metadata["Name"]
            version = distribution.version
        except Exception:
            raise ReceiptError("trusted account dependency metadata is invalid") from None
        if not isinstance(raw_name, str) or not isinstance(version, str):
            raise ReceiptError("trusted account dependency metadata is invalid")
        name = _normalized_distribution_name(raw_name)
        if name in observed:
            raise ReceiptError("trusted account dependency closure is ambiguous")
        observed[name] = version
        distributions[name] = distribution
    if observed != _TRUSTED_DISTRIBUTIONS:
        raise ReceiptError("trusted account dependency closure is invalid")

    owner = os.getuid()
    recorded_files: set[Path] = set()
    for distribution in distributions.values():
        files = distribution.files
        if not files:
            raise ReceiptError("trusted account dependency metadata is invalid")
        dist_info_directories = {
            Path(str(relative)).parent
            for relative in files
            if Path(str(relative)).name == "METADATA"
            and Path(str(relative)).parent.name.endswith(".dist-info")
        }
        if len(dist_info_directories) != 1:
            raise ReceiptError("trusted account dependency metadata is invalid")
        expected_record = next(iter(dist_info_directories)) / "RECORD"
        if sum(Path(str(relative)) == expected_record for relative in files) != 1:
            raise ReceiptError("trusted account dependency metadata is invalid")
        for relative in files:
            try:
                installed = Path(distribution.locate_file(relative))
                resolved_installed = installed.resolve(strict=True)
            except Exception:
                raise ReceiptError("trusted account dependency metadata is invalid") from None
            if (
                resolved_installed in recorded_files
                or not _secure_path_chain(resolved_installed, venv, owner=owner)
                or not _safe_lstat(resolved_installed, owner=owner, directory=False)
            ):
                raise ReceiptError("trusted account dependency file is invalid")
            recorded_files.add(resolved_installed)

            relative_path = Path(str(relative))
            try:
                record_hash = relative.hash
                record_size = relative.size
            except Exception:
                raise ReceiptError("trusted account dependency metadata is invalid") from None
            if relative_path == expected_record:
                if record_hash is not None or record_size is not None:
                    raise ReceiptError("trusted account dependency metadata is invalid")
                continue
            if (
                record_hash is None
                or record_hash.mode != "sha256"
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", record_hash.value)
                or not isinstance(record_size, int)
                or isinstance(record_size, bool)
                or record_size < 0
            ):
                raise ReceiptError("trusted account dependency RECORD is incomplete")
            try:
                if resolved_installed.stat().st_size != record_size:
                    raise ReceiptError("trusted account dependency integrity check failed")
                digest = hashlib.sha256()
                with resolved_installed.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                raise ReceiptError("trusted account dependency file is invalid") from None
            encoded_digest = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=")
            if encoded_digest.decode("ascii") != record_hash.value:
                raise ReceiptError("trusted account dependency integrity check failed")

    importable_suffixes = tuple(importlib.machinery.all_suffixes())
    try:
        site_entries = tuple(site.rglob("*"))
    except OSError:
        raise ReceiptError("trusted account dependency directory is invalid") from None
    for entry in site_entries:
        try:
            metadata = entry.lstat()
        except OSError:
            raise ReceiptError("trusted account dependency directory is invalid") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ReceiptError("trusted account dependency symlink is forbidden")
        if not stat.S_ISREG(metadata.st_mode) or not entry.name.endswith(importable_suffixes):
            continue
        try:
            resolved_entry = entry.resolve(strict=True)
        except OSError:
            raise ReceiptError("trusted account dependency file is invalid") from None
        if resolved_entry not in recorded_files:
            raise ReceiptError("trusted account dependency has an unrecorded importable file")

    for forbidden_name in ("sitecustomize.py", "usercustomize.py"):
        if (site / forbidden_name).exists() or (site / forbidden_name).is_symlink():
            raise ReceiptError("trusted account dependency startup hook is forbidden")
    try:
        forbidden_links = [*site.glob("*.pth"), *site.glob("*.egg-link")]
    except OSError:
        raise ReceiptError("trusted account dependency directory is invalid") from None
    if forbidden_links:
        raise ReceiptError("trusted account dependency startup hook is forbidden")


def _load_trusted_curl_runtime(
    *,
    trusted_site_packages: Path,
    forbidden_roots: tuple[Path, ...],
) -> _TrustedTransportRuntime:
    venv, site = _require_isolated_runtime_site(trusted_site_packages, forbidden_roots)
    _require_exact_distribution_closure(site, venv)
    if any(name == "curl_cffi" or name.startswith("curl_cffi.") for name in sys.modules):
        raise ReceiptError("trusted account transport was loaded before validation")

    original_sys_path = tuple(sys.path)
    inserted_site = str(site)
    sys.path.insert(0, inserted_site)
    try:
        curl_cffi_module = importlib.import_module("curl_cffi")
        curl_requests = importlib.import_module("curl_cffi.requests")
        curl_wrapper = importlib.import_module("curl_cffi._wrapper")
        curl_const = importlib.import_module("curl_cffi.const")
        if tuple(sys.path) != (inserted_site, *original_sys_path):
            raise ReceiptError("trusted account transport changed the import path")
        if getattr(curl_cffi_module, "__version__", None) != _TRUSTED_DISTRIBUTIONS["curl-cffi"]:
            raise ReceiptError("trusted account transport version is invalid")
        for module in (curl_cffi_module, curl_requests, curl_wrapper, curl_const):
            module_origin = getattr(module, "__file__", None)
            if module_origin is None:
                raise ReceiptError("trusted account transport origin is invalid")
            origin = Path(module_origin)
            try:
                resolved_origin = origin.resolve(strict=True)
            except OSError:
                raise ReceiptError("trusted account transport origin is invalid") from None
            if (
                not _path_is_within(resolved_origin, site)
                or not _secure_path_chain(origin, venv, owner=os.getuid())
                or not _safe_lstat(origin, owner=os.getuid(), directory=False)
            ):
                raise ReceiptError("trusted account transport origin is invalid")
        return _TrustedTransportRuntime(
            curl_opt=curl_const.CurlOpt,
            requests=curl_requests,
            original_sys_path=original_sys_path,
            inserted_site=inserted_site,
        )
    except ReceiptError:
        sys.path[:] = original_sys_path
        raise
    except Exception:
        sys.path[:] = original_sys_path
        raise ReceiptError("trusted account transport is unavailable") from None


@contextmanager
def trusted_curl_cffi_requester(
    *,
    forbidden_roots: tuple[Path, ...] = (),
    trusted_site_packages: Path | None = None,
    session_factory: Callable[..., Any] | None = None,
    runtime_loader: Callable[..., Any] = _load_trusted_curl_runtime,
) -> Iterator[CurlCffiRequester]:
    """Create one verifier-owned session from an exact isolated dependency set."""
    if trusted_site_packages is None:
        raise ReceiptError("trusted account runtime site-packages is required")
    try:
        runtime = runtime_loader(
            trusted_site_packages=Path(trusted_site_packages),
            forbidden_roots=forbidden_roots,
        )
    except ReceiptError:
        raise
    except Exception:
        raise ReceiptError("trusted account runtime is invalid") from None

    factory = runtime.requests.Session if session_factory is None else session_factory
    session = None
    path_drift = False
    try:
        session = factory(
            impersonate="chrome131",
            verify=True,
            trust_env=False,
            default_headers=False,
            curl_options={runtime.curl_opt.MAXFILESIZE_LARGE: MAX_RESPONSE_BYTES},
        )
        if (
            getattr(session, "trust_env", None) is not False
            or getattr(session, "default_headers", None) is not False
        ):
            raise ReceiptError("trusted account transport session policy is invalid")
        yield CurlCffiRequester(session)
    except ReceiptError:
        raise
    except Exception:
        raise ReceiptError("trusted account transport is unavailable") from None
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        if runtime.inserted_site is not None:
            path_drift = tuple(sys.path) != (
                runtime.inserted_site,
                *runtime.original_sys_path,
            )
            sys.path[:] = runtime.original_sys_path
        if path_drift:
            raise ReceiptError("trusted account transport changed the import path")


def _trusted_headers(token: str) -> dict[str, str]:
    if (
        not isinstance(token, str)
        or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
        or _ACCESS_TOKEN.fullmatch(token) is None
    ):
        raise ReceiptError("reviewed ChatGPT account auth material is unavailable")
    return {
        "Accept": "*/*",
        "Authorization": f"Bearer {token}",
        "OAI-Client-Build-Number": _CLIENT_BUILD,
        "OAI-Client-Version": _CLIENT_VERSION,
        "OAI-Device-Id": str(uuid.uuid4()),
        "OAI-Language": "en-US",
        "OAI-Session-Id": str(uuid.uuid4()),
        "Origin": _ACCOUNT_ORIGIN,
        "Referer": f"{_ACCOUNT_ORIGIN}/",
        "User-Agent": _CHROME_131_UA,
    }


def _utc_now() -> str:
    return _utc_datetime_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_live_probe_payload(value: Any) -> dict[str, Any]:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "package_version",
            "plan_class",
            "started_at",
            "completed_at",
            "shape_results",
        },
    )
    if (
        payload["schema_version"] != _SCHEMA_VERSION
        or payload["package_version"] != _PACKAGE_VERSION
        or payload["plan_class"] != "pro"
    ):
        raise ReceiptError("trusted account probe payload is invalid")
    started = _timestamp(payload["started_at"])
    completed = _timestamp(payload["completed_at"])
    if completed < started:
        raise ReceiptError("trusted account probe payload is invalid")
    _validate_shape_results(payload["shape_results"])
    return payload


def run_trusted_live_probe(
    expected_plan: str,
    *,
    token_loader: Callable[[], str] = _reviewed_account_access_token,
    requester_context: Callable[..., Any] = trusted_curl_cffi_requester,
    forbidden_roots: tuple[Path, ...] = (),
    trusted_site_packages: Path | None = None,
) -> dict[str, Any]:
    """Run the verifier-owned live route checks without candidate code."""
    if expected_plan != "pro":
        raise ReceiptError("live gate requires the reviewed Pro plan class")
    started_at = _utc_now()
    token = ""
    auth_headers: dict[str, str] = {}
    try:
        with requester_context(
            forbidden_roots=forbidden_roots,
            trusted_site_packages=trusted_site_packages,
        ) as requester:
            with Path(os.devnull).open("w", encoding="utf-8") as sink:
                with redirect_stdout(sink), redirect_stderr(sink):
                    try:
                        token = token_loader()
                    except ReceiptError:
                        raise
                    except Exception:
                        raise ReceiptError(
                            "reviewed ChatGPT account auth material is unavailable"
                        ) from None
                    auth_headers = _trusted_headers(token)
                    observed_plan = execute_plan_probe(
                        requester=requester,
                        auth_headers=auth_headers,
                    )
                    if observed_plan != expected_plan:
                        raise ReceiptError(
                            "authenticated account plan does not match the release gate"
                        )
                    records = run_probe_sequence(
                        requester=requester,
                        auth_headers=auth_headers,
                    )
    except ReceiptError:
        raise
    except Exception:
        raise ReceiptError("trusted account transport failed") from None
    finally:
        auth_headers.clear()
        token = ""
    return _validate_live_probe_payload(
        {
            "schema_version": _SCHEMA_VERSION,
            "package_version": _PACKAGE_VERSION,
            "plan_class": observed_plan,
            "started_at": started_at,
            "completed_at": _utc_now(),
            "shape_results": records,
        }
    )


def _outside_checkout(path: Path, checkout: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(checkout.resolve(strict=True))
    except ValueError:
        return True
    except OSError:
        raise ReceiptError("receipt output location is invalid") from None
    return False


def _inside_candidate_dist(path: Path, dist: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(dist.resolve(strict=False))
    except ValueError:
        return False
    except OSError:
        raise ReceiptError("receipt output location is invalid") from None
    return True


def run_create_gate(
    *,
    checkout: Path,
    dist: Path,
    output: Path,
    declared_commit: str,
    declared_tree: str,
    expected_plan: str,
    ci_repository: str,
    ci_run_id: str,
    ci_run_attempt: str,
    ci_artifact_id: str,
    ci_artifact_digest: str,
    ci_artifact_size: str,
    ci_artifact_expires_at: str,
    trusted_site_packages: Path | None = None,
    trusted_probe_runner: Callable[[str], dict[str, Any]] | None = None,
) -> str:
    """Probe with trusted code, then attest one inert main-CI artifact set."""
    checkout = Path(checkout)
    dist = Path(dist)
    output = Path(output)
    if expected_plan != "pro":
        raise ReceiptError("live gate requires the reviewed Pro plan class")
    if not _outside_checkout(dist, checkout):
        raise ReceiptError("candidate artifact output must be outside the checkout")
    if not _outside_checkout(output, checkout) or output.exists() or output.is_symlink():
        raise ReceiptError("receipt output must be new and outside the checkout")
    if _inside_candidate_dist(output, dist):
        raise ReceiptError("receipt output must be outside the candidate artifact directory")
    commit, tree = verify_checkout(
        checkout,
        declared_commit=declared_commit,
        declared_tree=declared_tree,
    )
    package_version = _project_version(checkout)
    artifacts = collect_local_candidate_artifacts(
        dist,
        package_version=package_version,
        source_commit=commit,
        source_tree=tree,
        repository=ci_repository,
        run_id=ci_run_id,
        run_attempt=ci_run_attempt,
        artifact_id=ci_artifact_id,
        artifact_digest=ci_artifact_digest,
        artifact_size=ci_artifact_size,
        artifact_expires_at=ci_artifact_expires_at,
    )
    if trusted_probe_runner is None:
        payload = run_trusted_live_probe(
            expected_plan,
            forbidden_roots=(checkout, dist),
            trusted_site_packages=trusted_site_packages,
        )
    else:
        payload = trusted_probe_runner(expected_plan)
    payload = _validate_live_probe_payload(payload)
    _validate_pretag_freshness(payload)

    verify_checkout(checkout, declared_commit=commit, declared_tree=tree)
    final_artifacts = collect_local_candidate_artifacts(
        dist,
        package_version=package_version,
        source_commit=commit,
        source_tree=tree,
        repository=ci_repository,
        run_id=ci_run_id,
        run_attempt=ci_run_attempt,
        artifact_id=ci_artifact_id,
        artifact_digest=ci_artifact_digest,
        artifact_size=ci_artifact_size,
        artifact_expires_at=ci_artifact_expires_at,
    )
    if final_artifacts != artifacts:
        raise ReceiptError("candidate artifacts changed during the live gate")
    receipt = build_receipt(
        package_version=package_version,
        plan_class=payload["plan_class"],
        started_at=payload["started_at"],
        completed_at=payload["completed_at"],
        source_commit=commit,
        source_tree=tree,
        local_candidate_artifacts=artifacts,
        adapter_status="passed",
        adapter_counts=_offline_adapter_counts(),
        shape_results=payload["shape_results"],
    )
    return write_receipt(output, receipt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--checkout", type=Path, required=True)
    create.add_argument("--dist", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--tree", required=True)
    create.add_argument("--expected-plan", choices=("pro",), required=True)
    create.add_argument("--repository", required=True)
    create.add_argument("--ci-run-id", required=True)
    create.add_argument("--ci-run-attempt", required=True)
    create.add_argument("--ci-artifact-id", required=True)
    create.add_argument("--ci-artifact-digest", required=True)
    create.add_argument("--ci-artifact-size", required=True)
    create.add_argument("--ci-artifact-expires-at", required=True)
    create.add_argument("--trusted-site-packages", type=Path, required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--checkout", type=Path, required=True)
    verify.add_argument("--dist", type=Path, required=True)
    verify.add_argument("--commit", required=True)
    verify.add_argument("--tree", required=True)
    verify.add_argument("--sha256", required=True)
    verify.add_argument("--repository", required=True)
    verify.add_argument("--ci-run-id", required=True)
    verify.add_argument("--ci-run-attempt", required=True)
    verify.add_argument("--ci-artifact-id", required=True)
    verify.add_argument("--ci-artifact-digest", required=True)
    verify.add_argument("--ci-artifact-size", required=True)
    verify.add_argument("--ci-artifact-expires-at", required=True)

    prepare_tag = commands.add_parser("prepare-tag")
    prepare_tag.add_argument("--receipt", type=Path, required=True)
    prepare_tag.add_argument("--checkout", type=Path, required=True)
    prepare_tag.add_argument("--dist", type=Path, required=True)
    prepare_tag.add_argument("--output", type=Path, required=True)
    prepare_tag.add_argument("--tag", required=True)
    prepare_tag.add_argument("--commit", required=True)
    prepare_tag.add_argument("--tree", required=True)
    prepare_tag.add_argument("--sha256", required=True)
    prepare_tag.add_argument("--repository", required=True)
    prepare_tag.add_argument("--ci-run-id", required=True)
    prepare_tag.add_argument("--ci-run-attempt", required=True)
    prepare_tag.add_argument("--ci-artifact-id", required=True)
    prepare_tag.add_argument("--ci-artifact-digest", required=True)
    prepare_tag.add_argument("--ci-artifact-size", required=True)
    prepare_tag.add_argument("--ci-artifact-expires-at", required=True)

    check_runtime = commands.add_parser("check-runtime")
    check_runtime.add_argument("--trusted-site-packages", type=Path, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            digest = run_create_gate(
                checkout=args.checkout,
                dist=args.dist,
                output=args.output,
                declared_commit=args.commit,
                declared_tree=args.tree,
                expected_plan=args.expected_plan,
                ci_repository=args.repository,
                ci_run_id=args.ci_run_id,
                ci_run_attempt=args.ci_run_attempt,
                ci_artifact_id=args.ci_artifact_id,
                ci_artifact_digest=args.ci_artifact_digest,
                ci_artifact_size=args.ci_artifact_size,
                ci_artifact_expires_at=args.ci_artifact_expires_at,
                trusted_site_packages=args.trusted_site_packages,
            )
            receipt, _payload = _read_receipt(args.output)
            print(f"account-receipt-sha256: {digest}")
            print(f"account-artifact-set-sha256: {artifact_set_sha256(receipt['local_candidate_artifacts'])}")
            print(f"account-ci-run-id: {args.ci_run_id}")
            print(f"account-ci-run-attempt: {args.ci_run_attempt}")
            print(f"account-ci-artifact-id: {args.ci_artifact_id}")
            print(f"account-ci-artifact-digest: {args.ci_artifact_digest}")
            print(f"account-ci-artifact-size: {args.ci_artifact_size}")
            print(f"account-ci-artifact-expires-at: {args.ci_artifact_expires_at}")
        elif args.command == "check-runtime":
            with trusted_curl_cffi_requester(
                trusted_site_packages=args.trusted_site_packages
            ):
                pass
        elif args.command == "prepare-tag":
            prepare_tag_request(
                args.receipt,
                checkout=args.checkout,
                dist=args.dist,
                output=args.output,
                tag=args.tag,
                declared_commit=args.commit,
                declared_tree=args.tree,
                expected_sha256=args.sha256,
                ci_repository=args.repository,
                ci_run_id=args.ci_run_id,
                ci_run_attempt=args.ci_run_attempt,
                ci_artifact_id=args.ci_artifact_id,
                ci_artifact_digest=args.ci_artifact_digest,
                ci_artifact_size=args.ci_artifact_size,
                ci_artifact_expires_at=args.ci_artifact_expires_at,
            )
        elif args.command == "verify":
            digest = verify_receipt_file(
                args.receipt,
                checkout=args.checkout,
                dist=args.dist,
                declared_commit=args.commit,
                declared_tree=args.tree,
                expected_sha256=args.sha256,
                ci_repository=args.repository,
                ci_run_id=args.ci_run_id,
                ci_run_attempt=args.ci_run_attempt,
                ci_artifact_id=args.ci_artifact_id,
                ci_artifact_digest=args.ci_artifact_digest,
                ci_artifact_size=args.ci_artifact_size,
                ci_artifact_expires_at=args.ci_artifact_expires_at,
            )
            receipt, _payload = _read_receipt(args.receipt)
            print(f"account-receipt-sha256: {digest}")
            print(f"account-artifact-set-sha256: {artifact_set_sha256(receipt['local_candidate_artifacts'])}")
            print(f"account-ci-run-id: {args.ci_run_id}")
            print(f"account-ci-run-attempt: {args.ci_run_attempt}")
            print(f"account-ci-artifact-id: {args.ci_artifact_id}")
            print(f"account-ci-artifact-digest: {args.ci_artifact_digest}")
            print(f"account-ci-artifact-size: {args.ci_artifact_size}")
            print(f"account-ci-artifact-expires-at: {args.ci_artifact_expires_at}")
    except Exception:
        print("account receipt gate failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
