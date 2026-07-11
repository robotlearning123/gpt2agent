#!/usr/bin/env python3
"""Create and verify a sanitized, exact-commit local account receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 3
REQUEST_TIMEOUT_SECONDS = 20
_ACCOUNT_ORIGIN = "https://chatgpt.com"
_MAX_ITEMS = 10_000
_MAX_RECEIPT_BYTES = 1024 * 1024
_PACKAGE_VERSION = "0.0.12"
_SCHEMA_VERSION = "3"
_VERIFIER_NAME = "gpt2agent-account-receipt"
_VERIFIER_VERSION = "3"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class ReceiptError(ValueError):
    """The trusted-local gate violated its fail-closed contract."""


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


@dataclass
class RuntimeAdapterChecks:
    """Exercise fixed installed-package adapters without retaining their output."""

    validators: Mapping[str, Callable[[Any], Any]]
    _exercised: set[str] = dataclass_field(default_factory=set, init=False, repr=False)
    _passed: set[str] = dataclass_field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        if set(self.validators) != set(_RUNTIME_ADAPTER_CATEGORIES) or any(
            not callable(validator) for validator in self.validators.values()
        ):
            raise ReceiptError("installed runtime adapter set is invalid")

    def validate(self, category: str, value: Any) -> _ShapeCheck | None:
        validator = self.validators.get(category)
        if validator is None:
            return None
        if category in self._exercised:
            raise ReceiptError("installed runtime adapter was exercised more than once")
        self._exercised.add(category)
        try:
            normalized = validator(value)
            if category == "sites_access":
                if not isinstance(normalized, dict):
                    raise TypeError("object adapter returned a non-object")
                result = _ShapeCheck(
                    "valid_object",
                    None,
                    continue_site_catalog=(
                        normalized.get("enabled") is not False
                        if category == "sites_access"
                        else None
                    ),
                )
            else:
                if isinstance(normalized, list):
                    items = normalized
                elif isinstance(normalized, dict) and isinstance(normalized.get("items"), list):
                    items = normalized["items"]
                else:
                    raise TypeError("collection adapter returned an invalid projection")
                result = _ShapeCheck(
                    "valid_nonempty" if items else "valid_empty",
                    len(items),
                )
        except Exception:
            raise ReceiptError("installed runtime adapter rejected account response") from None
        self._passed.add(category)
        return result

    def evidence(self, shape_results: list[dict[str, Any]]) -> dict[str, int]:
        """Return only fixed aggregate fields after checking complete coverage."""
        results = _validate_shape_results(shape_results)
        not_requested = {
            record["route_category"]
            for record in results
            if record["status"] == "not_requested"
            and record["route_category"] in _RUNTIME_ADAPTER_CATEGORIES
        }
        expected_exercised = set(_RUNTIME_ADAPTER_CATEGORIES) - not_requested
        if self._exercised != expected_exercised or self._passed != expected_exercised:
            raise ReceiptError("installed runtime adapter coverage is incomplete")
        return {
            "adapters_declared": len(_RUNTIME_ADAPTER_CATEGORIES),
            "adapters_exercised": len(self._exercised),
            "adapters_passed": len(self._passed),
            "adapters_not_requested": len(not_requested),
        }


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

_PROBE_BY_CATEGORY = {probe.category: probe for probe in PROBES}
_PROBE_BY_REQUEST = {(probe.path, probe.query): probe for probe in PROBES}
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


def _validate_response_size(response: RawResponse) -> None:
    if not isinstance(response.body, bytes):
        raise ReceiptError("account response body is malformed")
    declared = _header(response.headers, "Content-Length")
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


def installed_runtime_adapter_checks() -> RuntimeAdapterChecks:
    """Load exact adapter functions from the candidate package in this interpreter."""
    try:
        from gpt2agent.model_catalog import (
            normalize_general_models,
            normalize_work_models,
        )
        from gpt2agent.tools.apps import normalize_apps
        from gpt2agent.tools.automations import normalize_scheduled_page
        from gpt2agent.tools.codex import normalize_codex_environments
        from gpt2agent.tools.conversations import normalize_background_tasks
        from gpt2agent.tools.gpts import normalize_custom_gpts
        from gpt2agent.tools.plugins import (
            normalize_installed_plugins,
            normalize_plugin_catalog,
        )
        from gpt2agent.tools.sites import normalize_sites_access, normalize_sites_page
    except Exception:
        raise ReceiptError("installed runtime adapters could not be imported") from None

    def general_models(data: Any) -> Any:
        return normalize_general_models(data["models"])

    def work_models(data: Any) -> Any:
        return normalize_work_models(data["models"])

    def plugin_catalog(data: Any) -> Any:
        return normalize_plugin_catalog(data, limit=1, cursor=None)

    def background_jobs(data: Any) -> Any:
        return normalize_background_tasks(data, limit=1)

    def site_catalog(data: Any) -> Any:
        page = normalize_sites_page(data)
        if len(page["items"]) > 1:
            raise ValueError("site page exceeds the probed limit")
        return page

    return RuntimeAdapterChecks(
        {
            "chat_models": general_models,
            "work_models": work_models,
            "apps": normalize_apps,
            "plugins": plugin_catalog,
            "installed_plugins": normalize_installed_plugins,
            "background_jobs": background_jobs,
            "scheduled_automations": normalize_scheduled_page,
            "sites_access": normalize_sites_access,
            "site_catalog": site_catalog,
            "custom_gpts": normalize_custom_gpts,
            "codex": normalize_codex_environments,
        }
    )


def _decode_and_check_shape(
    probe: ProbeSpec,
    response: RawResponse,
    *,
    adapter_checks: RuntimeAdapterChecks | None = None,
) -> _ShapeCheck:
    content_type = (_header(response.headers, "Content-Type") or "").split(";", 1)[0].lower()
    if content_type != "application/json":
        raise ReceiptError("account response content type is not JSON")
    try:
        data = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReceiptError("account response is not valid JSON") from None
    if adapter_checks is not None:
        adapted = adapter_checks.validate(probe.category, data)
        if adapted is not None:
            return adapted
    try:
        validator = _SHAPE_VALIDATORS[probe.category]
        return validator(data)
    except KeyError:
        raise ReceiptError("account response contract requires review") from None


def execute_probe(
    probe: ProbeSpec,
    *,
    requester: Callable[..., RawResponse],
    auth_headers: Mapping[str, str],
    adapter_checks: RuntimeAdapterChecks | None = None,
) -> ProbeOutcome:
    """Issue one bounded request and return a value-only-free outcome."""
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
        _validate_response_size(response)

        if response.status in {301, 302, 303, 307, 308}:
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
            continue
        break

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

    checked = _decode_and_check_shape(
        reviewed,
        response,
        adapter_checks=adapter_checks,
    )
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
    adapter_checks: RuntimeAdapterChecks | None = None,
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
            adapter_checks=adapter_checks,
        )
        records.append(outcome.receipt_record())
        if probe.category == "sites_access":
            continue_site_catalog = outcome.continue_site_catalog is not False
    return records


def canonical_json(value: Any) -> bytes:
    """Serialize evidence deterministically for an external SHA-256."""
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _require_oid(value: Any) -> str:
    if not isinstance(value, str) or not _HEX40.fullmatch(value):
        raise ReceiptError("declared source identity is invalid")
    return value


def _git_output(checkout: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=checkout,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReceiptError("checkout Git inspection failed") from None
    if result.returncode != 0:
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
    root_text = _git_output(resolved, "rev-parse", "--show-toplevel")
    try:
        root = Path(root_text).resolve(strict=True)
    except OSError:
        raise ReceiptError("checkout Git root is invalid") from None
    if root != resolved:
        raise ReceiptError("checkout must name the repository root")
    status = _git_output(
        resolved,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
        "--ignore-submodules=none",
    )
    if status:
        raise ReceiptError("checkout must be clean")
    actual_commit = _git_output(resolved, "rev-parse", "HEAD")
    actual_tree = _git_output(resolved, "rev-parse", "HEAD^{tree}")
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
) -> dict[str, Any]:
    """Hash exactly one local wheel and sdist without exposing their bytes."""
    if package_version != _PACKAGE_VERSION:
        raise ReceiptError("candidate package version is invalid")
    commit = _require_oid(source_commit)
    tree = _require_oid(source_tree)
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
        "build_origin": "local_live_gate",
        "source": {"commit": commit, "tree": tree},
        "wheel": {"filename": wheel_name, "sha256": _sha256(wheel)},
        "sdist": {"filename": sdist_name, "sha256": _sha256(sdist)},
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
    artifacts = _exact_keys(value, {"build_origin", "source", "wheel", "sdist"})
    if artifacts["build_origin"] != "local_live_gate" or artifacts["source"] != source:
        raise ReceiptError("account receipt artifact binding is invalid")
    for kind in ("wheel", "sdist"):
        record = _exact_keys(artifacts[kind], {"filename", "sha256"})
        _validate_artifact_filename(kind, record["filename"], package_version)
        if not isinstance(record["sha256"], str) or not _HEX64.fullmatch(record["sha256"]):
            raise ReceiptError("account receipt artifact binding is invalid")


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
        raise ReceiptError("installed runtime adapter counts are invalid")
    results = (
        _validate_public_shape_results(shape_results)
        if public
        else _validate_shape_results(shape_results)
    )
    not_requested = sum(
        record["status"] == "not_requested"
        and record["route_category"] in _RUNTIME_ADAPTER_CATEGORIES
        for record in results
    )
    exercised = len(_RUNTIME_ADAPTER_CATEGORIES) - not_requested
    expected = {
        "adapters_declared": len(_RUNTIME_ADAPTER_CATEGORIES),
        "adapters_exercised": exercised,
        "adapters_passed": exercised,
        "adapters_not_requested": not_requested,
    }
    if counts != expected:
        raise ReceiptError("installed runtime adapter counts are invalid")
    return counts


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
    if path.is_symlink() or not path.is_file():
        raise ReceiptError("account receipt must be a regular file")
    try:
        if path.stat().st_size > _MAX_RECEIPT_BYTES:
            raise ReceiptError("account receipt is too large")
        payload = path.read_bytes()
    except OSError:
        raise ReceiptError("account receipt could not be read") from None
    try:
        receipt = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
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
) -> str:
    """Bind canonical evidence to current clean source and artifact bytes."""
    receipt, payload = _read_receipt(receipt_path)
    if not isinstance(expected_sha256, str) or not _HEX64.fullmatch(expected_sha256):
        raise ReceiptError("expected receipt digest is invalid")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        raise ReceiptError("account receipt digest does not match")
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
    )
    if receipt["local_candidate_artifacts"] != current_artifacts:
        raise ReceiptError("candidate artifact bytes do not match the account receipt")
    return digest


def _run_command(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> None:
    """Run a release step while deliberately discarding potentially sensitive output."""
    try:
        result = subprocess.run(
            [str(value) for value in argv],
            cwd=cwd,
            env=dict(env),
            check=False,
            capture_output=True,
            timeout=15 * 60,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReceiptError("local release subprocess failed") from None
    if result.returncode != 0:
        raise ReceiptError("local release subprocess failed")


def _subprocess_env(*, isolated_home: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    if isolated_home is not None:
        env["HOME"] = str(isolated_home)
        env.pop("CODEX_HOME", None)
    return env


def _remove_owned_build_residue(checkout: Path) -> None:
    """Remove only paths a clean setuptools build is allowed to create."""
    for relative in ("build", "gpt2agent.egg-info"):
        path = checkout / relative
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError:
            raise ReceiptError("owned build residue could not be removed") from None


def build_distributions(checkout: Path, dist: Path) -> None:
    """Build one fresh wheel/sdist set from the already-verified checkout."""
    checkout = Path(checkout).resolve()
    dist = Path(dist)
    if dist.exists() or dist.is_symlink() or not dist.parent.is_dir():
        raise ReceiptError("candidate artifact output must not already exist")
    try:
        dist.mkdir(mode=0o700)
    except OSError:
        raise ReceiptError("candidate artifact output could not be created") from None
    try:
        try:
            _run_command(
                [sys.executable, "-m", "build", "--outdir", str(dist.resolve())],
                cwd=checkout,
                env=_subprocess_env(),
            )
        finally:
            _remove_owned_build_residue(checkout)
    except ReceiptError:
        shutil.rmtree(dist, ignore_errors=True)
        raise


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


@contextmanager
def installed_candidate_context(
    dist: Path,
    artifacts: dict[str, Any],
    package_version: str,
    *,
    temp_parent: Path | None = None,
) -> Iterator[Path]:
    """Install and check both artifacts before yielding the wheel interpreter."""
    if package_version != _PACKAGE_VERSION:
        raise ReceiptError("candidate package version is invalid")
    source = _exact_keys(artifacts.get("source"), {"commit", "tree"})
    source = {"commit": _require_oid(source["commit"]), "tree": _require_oid(source["tree"])}
    _validate_artifact_record(
        artifacts,
        package_version=package_version,
        source=source,
    )
    dist = Path(dist).resolve()
    wheel = dist / artifacts["wheel"]["filename"]
    sdist = dist / artifacts["sdist"]["filename"]
    if any(path.is_symlink() or not path.is_file() for path in (wheel, sdist)):
        raise ReceiptError("candidate artifacts must be regular files")

    parent = Path(temp_parent).resolve() if temp_parent is not None else None
    if parent is not None and not parent.is_dir():
        raise ReceiptError("temporary installation parent is invalid")
    with tempfile.TemporaryDirectory(
        prefix="gpt2agent-account-gate-",
        dir=str(parent) if parent is not None else None,
    ) as temp_name:
        temp_root = Path(temp_name)
        home = temp_root / "home"
        home.mkdir(mode=0o700)
        env = _subprocess_env(isolated_home=home)
        wheel_venv = temp_root / "wheel-venv"
        sdist_venv = temp_root / "sdist-venv"
        wheel_python = _venv_python(wheel_venv)
        sdist_python = _venv_python(sdist_venv)

        _run_command([sys.executable, "-m", "venv", str(wheel_venv)], cwd=temp_root, env=env)
        _run_command(
            [
                str(wheel_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                str(wheel),
            ],
            cwd=temp_root,
            env=env,
        )
        _run_command(
            [str(wheel_python), "-m", "pip", "check"],
            cwd=temp_root,
            env=env,
        )
        _run_command(
            [
                str(wheel_python),
                "-c",
                (
                    "from importlib.metadata import version; import gpt2agent; "
                    f"assert version('gpt2agent') == '{package_version}'; "
                    f"assert gpt2agent.__version__ == '{package_version}'"
                ),
            ],
            cwd=temp_root,
            env=env,
        )

        _run_command([sys.executable, "-m", "venv", str(sdist_venv)], cwd=temp_root, env=env)
        _run_command(
            [
                str(sdist_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                str(sdist),
            ],
            cwd=temp_root,
            env=env,
        )
        _run_command(
            [str(sdist_python), "-m", "pip", "check"],
            cwd=temp_root,
            env=env,
        )
        _run_command(
            [
                str(sdist_python),
                "-c",
                (
                    "from importlib.metadata import version; import gpt2agent; "
                    f"assert version('gpt2agent') == '{package_version}'; "
                    f"assert gpt2agent.__version__ == '{package_version}'"
                ),
            ],
            cwd=temp_root,
            env=env,
        )
        yield wheel_python


class CurlCffiRequester:
    """Streaming, no-auto-redirect requester backed by the installed package session."""

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
        probe_from_url(url)
        try:
            response = self._session.get(
                url,
                headers=dict(headers),
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except Exception:
            raise ReceiptError("account probe transport failed") from None
        try:
            raw_headers = getattr(response, "headers", {})
            try:
                response_headers = {str(key): str(value) for key, value in raw_headers.items()}
            except Exception:
                raise ReceiptError("account response metadata is malformed") from None
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_probe_payload(value: Any) -> dict[str, Any]:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "package_version",
            "plan_class",
            "started_at",
            "completed_at",
            "adapter_status",
            "adapter_counts",
            "shape_results",
        },
    )
    if (
        payload["schema_version"] != _SCHEMA_VERSION
        or payload["package_version"] != _PACKAGE_VERSION
        or payload["plan_class"] != "pro"
    ):
        raise ReceiptError("installed account probe payload is invalid")
    started = _timestamp(payload["started_at"])
    completed = _timestamp(payload["completed_at"])
    if completed < started:
        raise ReceiptError("installed account probe payload is invalid")
    shape_results = _validate_shape_results(payload["shape_results"])
    if payload["adapter_status"] != "passed":
        raise ReceiptError("installed runtime adapter status is invalid")
    _validate_adapter_counts(payload["adapter_counts"], shape_results)
    return payload


def _write_probe_payload(path: Path, payload: dict[str, Any]) -> None:
    _validate_probe_payload(payload)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(payload))
    except OSError:
        raise ReceiptError("installed account probe output failed") from None


def run_installed_live_probe(output: Path, expected_plan: str) -> None:
    """Run inside the wheel venv; persist only the sanitized intermediate payload."""
    if expected_plan != "pro":
        raise ReceiptError("live gate requires the reviewed Pro plan class")
    try:
        from importlib.metadata import version

        import gpt2agent
        from gpt2agent.backend import BackendClient
        from gpt2agent.setup import detect_plan
    except Exception:
        raise ReceiptError("installed account candidate could not be imported") from None
    if version("gpt2agent") != _PACKAGE_VERSION or gpt2agent.__version__ != _PACKAGE_VERSION:
        raise ReceiptError("installed account candidate version is invalid")

    started_at = _utc_now()
    client = BackendClient()
    try:
        try:
            observed_plan = detect_plan(client)
        except Exception:
            raise ReceiptError("authenticated account plan could not be measured") from None
        if observed_plan != expected_plan:
            raise ReceiptError("authenticated account plan does not match the release gate")
        auth_headers = client.request_headers()
        adapter_checks = installed_runtime_adapter_checks()
        records = run_probe_sequence(
            requester=CurlCffiRequester(client._session),
            auth_headers=auth_headers,
            adapter_checks=adapter_checks,
        )
        adapter_counts = adapter_checks.evidence(records)
    finally:
        try:
            client._session.close()
        except Exception:
            pass
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "package_version": _PACKAGE_VERSION,
        "plan_class": observed_plan,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "adapter_status": "passed",
        "adapter_counts": adapter_counts,
        "shape_results": records,
    }
    _write_probe_payload(Path(output), payload)


def _read_probe_payload(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReceiptError("installed account probe produced no evidence")
    try:
        if path.stat().st_size > _MAX_RECEIPT_BYTES:
            raise ReceiptError("installed account probe evidence is too large")
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ReceiptError("installed account probe evidence is invalid") from None
    if canonical_json(payload) != raw:
        raise ReceiptError("installed account probe evidence is not canonical")
    return _validate_probe_payload(payload)


def probe_installed_candidate(wheel_python: Path, expected_plan: str) -> dict[str, Any]:
    """Re-exec this verifier with imports isolated to the installed wheel."""
    script = Path(__file__).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="gpt2agent-account-probe-") as temp_name:
        temp_root = Path(temp_name)
        output = temp_root / "probe.json"
        _run_command(
            [
                str(wheel_python),
                str(script),
                "_probe",
                "--output",
                str(output),
                "--expected-plan",
                expected_plan,
            ],
            cwd=temp_root,
            env=_subprocess_env(),
        )
        return _read_probe_payload(output)


def _outside_checkout(path: Path, checkout: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(checkout.resolve(strict=True))
    except ValueError:
        return True
    except OSError:
        raise ReceiptError("receipt output location is invalid") from None
    return False


def run_create_gate(
    *,
    checkout: Path,
    dist: Path,
    output: Path,
    declared_commit: str,
    declared_tree: str,
    expected_plan: str,
    builder: Callable[[Path, Path], None] = build_distributions,
    installation_context: Callable[..., Any] = installed_candidate_context,
    probe_runner: Callable[[Path, str], dict[str, Any]] = probe_installed_candidate,
) -> str:
    """Build, install, probe, re-bind, and finally emit one exact receipt."""
    checkout = Path(checkout)
    dist = Path(dist)
    output = Path(output)
    if expected_plan != "pro":
        raise ReceiptError("live gate requires the reviewed Pro plan class")
    if not _outside_checkout(dist, checkout):
        raise ReceiptError("candidate artifact output must be outside the checkout")
    if not _outside_checkout(output, checkout) or output.exists() or output.is_symlink():
        raise ReceiptError("receipt output must be new and outside the checkout")
    commit, tree = verify_checkout(
        checkout,
        declared_commit=declared_commit,
        declared_tree=declared_tree,
    )
    package_version = _project_version(checkout)
    if dist.exists() or dist.is_symlink():
        raise ReceiptError("candidate artifact output must not already exist")
    builder(checkout, dist)
    verify_checkout(checkout, declared_commit=commit, declared_tree=tree)
    artifacts = collect_local_candidate_artifacts(
        dist,
        package_version=package_version,
        source_commit=commit,
        source_tree=tree,
    )
    with installation_context(dist, artifacts, package_version) as wheel_python:
        payload = _validate_probe_payload(probe_runner(wheel_python, expected_plan))

    verify_checkout(checkout, declared_commit=commit, declared_tree=tree)
    final_artifacts = collect_local_candidate_artifacts(
        dist,
        package_version=package_version,
        source_commit=commit,
        source_tree=tree,
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
        adapter_status=payload["adapter_status"],
        adapter_counts=payload["adapter_counts"],
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

    verify = commands.add_parser("verify")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--checkout", type=Path, required=True)
    verify.add_argument("--dist", type=Path, required=True)
    verify.add_argument("--commit", required=True)
    verify.add_argument("--tree", required=True)
    verify.add_argument("--sha256", required=True)

    internal = commands.add_parser("_probe", help=argparse.SUPPRESS)
    internal.add_argument("--output", type=Path, required=True)
    internal.add_argument("--expected-plan", choices=("pro",), required=True)
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
            )
            print(f"account receipt created: sha256={digest}")
        elif args.command == "verify":
            digest = verify_receipt_file(
                args.receipt,
                checkout=args.checkout,
                dist=args.dist,
                declared_commit=args.commit,
                declared_tree=args.tree,
                expected_sha256=args.sha256,
            )
            print(f"account receipt verified: sha256={digest}")
        else:
            run_installed_live_probe(args.output, args.expected_plan)
    except Exception:
        print("account receipt gate failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
