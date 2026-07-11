#!/usr/bin/env python3
"""Check bounded public product/documentation surfaces without account access."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import ParseResult, urljoin, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2 * 1024 * 1024
MAX_PREVIOUS_REPORT_BYTES = 2 * 1024 * 1024
MAX_STATIC_ASSETS = 32
MAX_TOTAL_BUNDLE_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 10
TOTAL_RUNTIME_SECONDS = 180
ALLOWED_HOSTS = frozenset(
    {
        "api.github.com",
        "cdn.oaistatic.com",
        "chatgpt.com",
        "help.openai.com",
        "learn.chatgpt.com",
        "pypi.org",
    }
)
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/ld+json",
        "application/x-javascript",
        "text/html",
        "text/javascript",
        "text/plain",
    }
)
HARD_FAILURE_STATUSES = frozenset({"contract_marker_missing", "fallback_metadata_stale"})


class RadarError(ValueError):
    """A public fetch or evidence input violated a fail-closed boundary."""


def validate_url(url: str, allowed_hosts: frozenset[str] = ALLOWED_HOSTS) -> ParseResult:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise RadarError("only HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise RadarError("URL credentials are forbidden")
    try:
        port = parsed.port
    except ValueError as error:
        raise RadarError("invalid URL port") from error
    if port not in {None, 443}:
        raise RadarError("only the default HTTPS port is allowed")
    hostname = (parsed.hostname or "").lower()
    if hostname not in allowed_hosts:
        raise RadarError("URL host is not allowlisted")
    if parsed.fragment:
        raise RadarError("URL fragments are forbidden")
    return parsed


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    url: str
    kind: str
    markers: tuple[str, ...] = ()
    expected_sha256: str | None = None
    surface: str = "other"
    parser: str = "text"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", self.source_id):
            raise RadarError("invalid source identifier")
        if self.kind not in {"contract", "documentation"}:
            raise RadarError("source kind must be contract or documentation")
        if self.surface not in {"chatgpt", "codex", "mcp", "other", "radar"}:
            raise RadarError("invalid public surface")
        if self.parser not in {
            "chatgpt_bundles",
            "mcp_spec_release",
            "mcp_v1_pypi",
            "mcp_v1_release",
            "text",
        }:
            raise RadarError("invalid source parser")
        if self.parser != "text" and self.kind != "contract":
            raise RadarError("structured parsers require a contract source")
        validate_url(self.url)
        if self.expected_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.expected_sha256
        ):
            raise RadarError("invalid expected SHA-256")
        if any(not marker or len(marker) > 200 for marker in self.markers):
            raise RadarError("markers must be non-empty and bounded")


# Each group is satisfied when any reviewed literal is present in the fetched
# public shell/static bundles. These strings model only the new 0.0.12 adapter
# routes, fixed query, and distinctive envelope fields. Downloaded JavaScript is
# scanned as inert text and is never imported, evaluated, or written to disk.
CHATGPT_CONTRACT_MARKERS: dict[str, tuple[str, ...]] = {
    "models-route": ("/backend-api/models",),
    "apps-route": ("/backend-api/apps/list",),
    "automations-route": ("/backend-api/automations",),
    "automations-query": (
        'filter:"scheduled"',
        '"filter":"scheduled"',
        "filter=scheduled",
    ),
    "plugins-catalog-route": ("/backend-api/plugins/list",),
    "plugins-installed-route": ("/backend-api/plugins/installed",),
    "work-models-route": ("/backend-api/tpp/models/", "/backend-api/tpp/models"),
    "sites-route": ("/backend-api/websites",),
    "scheduled-envelope": ("next_run_times",),
    "plugin-pagination-envelope": ("next_page_token",),
    "work-model-envelope": ("thinking_efforts",),
    "sites-access-envelope": ("custom_domains_enabled",),
}


SOURCES = (
    SourceSpec(
        "chatgpt-release-notes",
        "https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
        "documentation",
        ("ChatGPT",),
        surface="chatgpt",
    ),
    SourceSpec(
        "chatgpt-whats-new",
        "https://learn.chatgpt.com/docs/whats-new",
        "documentation",
        ("ChatGPT",),
        surface="chatgpt",
    ),
    SourceSpec(
        "chatgpt-voice",
        "https://help.openai.com/en/articles/20001274",
        "documentation",
        ("Voice",),
        surface="chatgpt",
    ),
    SourceSpec(
        "chatgpt-work",
        "https://help.openai.com/en/articles/20001275",
        "documentation",
        ("ChatGPT",),
        surface="chatgpt",
    ),
    SourceSpec(
        "chatgpt-sites",
        "https://help.openai.com/en/articles/20001339",
        "documentation",
        ("ChatGPT",),
        surface="chatgpt",
    ),
    SourceSpec(
        "chatgpt-plugins",
        "https://help.openai.com/en/articles/20001256-plugins-in-chatgpt-and-codex",
        "documentation",
        ("Plugins",),
        surface="chatgpt",
    ),
    SourceSpec(
        "codex-changelog",
        "https://learn.chatgpt.com/docs/changelog",
        "documentation",
        ("Codex",),
        surface="codex",
    ),
    SourceSpec(
        "chatgpt-manifest-bundles",
        "https://chatgpt.com/",
        "contract",
        surface="chatgpt",
        parser="chatgpt_bundles",
    ),
    SourceSpec(
        "mcp-python-sdk-release",
        "https://api.github.com/repos/modelcontextprotocol/python-sdk/releases/latest",
        "contract",
        surface="mcp",
        parser="mcp_v1_release",
    ),
    SourceSpec(
        "mcp-python-sdk-pypi",
        "https://pypi.org/pypi/mcp/json",
        "contract",
        surface="mcp",
        parser="mcp_v1_pypi",
    ),
    SourceSpec(
        "mcp-specification-release",
        ("https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/releases/latest"),
        "contract",
        surface="mcp",
        parser="mcp_spec_release",
    ),
)


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def __init__(self, original_host: str) -> None:
        super().__init__()
        self.original_host = original_host

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parsed = validate_url(newurl)
        if parsed.hostname != self.original_host:
            raise RadarError("cross-host redirect is forbidden")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(source_host: str) -> Callable[..., Any]:
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        _SameHostRedirectHandler(source_host),
    )
    return opener.open


def fetch_source(
    source: SourceSpec,
    *,
    opener: Callable[..., Any] | None = None,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> bytes:
    parsed = validate_url(source.url)
    if timeout_seconds <= 0 or timeout_seconds > TIMEOUT_SECONDS:
        raise RadarError("request timeout is outside the allowed bound")
    request = urllib.request.Request(
        source.url,
        headers={
            "Accept": "application/json,text/html,text/javascript,text/plain;q=0.9",
            "User-Agent": "gpt2agent-public-surface-radar/2",
        },
        method="GET",
    )
    open_request = opener or _default_opener(parsed.hostname or "")
    try:
        with open_request(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
            if not 200 <= status < 300:
                raise RadarError(f"unexpected HTTP status {status}")
            final_url = response.geturl()
            if urlparse(final_url).hostname != parsed.hostname:
                raise RadarError("cross-host redirect is forbidden")
            validate_url(final_url)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in ALLOWED_CONTENT_TYPES:
                raise RadarError(f"unexpected content type: {content_type or 'missing'}")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    announced_size = int(content_length)
                except ValueError as error:
                    raise RadarError("invalid Content-Length") from error
                if announced_size < 0 or announced_size > MAX_BYTES:
                    raise RadarError("response is too large")
            body = response.read(MAX_BYTES + 1)
    except RadarError:
        raise
    except (OSError, urllib.error.URLError) as error:
        raise RadarError(f"public fetch failed: {type(error).__name__}") from error
    if len(body) > MAX_BYTES:
        raise RadarError("response is too large")
    return body


class _VisibleTextParser(HTMLParser):
    _IGNORED = frozenset({"script", "style", "svg", "template", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._IGNORED:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _normalized_document_text(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    if "<" not in text:
        return " ".join(text.split())
    parser = _VisibleTextParser()
    parser.feed(text)
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fingerprint(body: bytes, *, document: bool = False) -> tuple[str, int]:
    if document:
        normalized = _normalized_document_text(body)
    else:
        normalized = " ".join(body.decode("utf-8", errors="replace").split())
    return _sha256_text(normalized), len(body)


def _structured_contract(source: SourceSpec, body: bytes) -> tuple[str | None, str]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, _sha256_text("invalid-json")
    if not isinstance(payload, dict):
        return None, _sha256_text("invalid-envelope")

    value: Any
    if source.parser == "mcp_v1_pypi":
        info = payload.get("info")
        value = info.get("version") if isinstance(info, dict) else None
    else:
        value = payload.get("tag_name")
    if not isinstance(value, str):
        return None, _sha256_text("missing-observed-value")
    normalized = value.removeprefix("v")

    if source.parser in {"mcp_v1_pypi", "mcp_v1_release"}:
        if not re.fullmatch(r"1\.[0-9]+\.[0-9]+", normalized):
            return None, _sha256_text(f"unsupported-v1:{normalized}")
    elif source.parser == "mcp_spec_release":
        if not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", normalized):
            return None, _sha256_text(f"invalid-spec-date:{normalized}")
    else:  # pragma: no cover - SourceSpec prevents this path.
        raise RadarError("unsupported structured parser")
    return normalized, _sha256_text(canonical_json({"observed_value": normalized}).strip())


def _apply_drift_status(
    result: dict[str, Any],
    source: SourceSpec,
    previous_fingerprints: dict[str, str],
) -> None:
    if result["status"] != "ok":
        return
    baseline = source.expected_sha256 or previous_fingerprints.get(source.source_id)
    if baseline is None:
        result["status"] = "review_needed"
        result["drift_reason"] = "baseline_missing"
        result["baseline_source"] = "none"
    elif baseline != result["normalized_sha256"]:
        result["status"] = "review_needed"
        result["drift_reason"] = "normalized_fingerprint_changed"
        result["baseline_source"] = (
            "checked_in" if source.expected_sha256 is not None else "previous_artifact"
        )
    else:
        result["baseline_source"] = (
            "checked_in" if source.expected_sha256 is not None else "previous_artifact"
        )


def evaluate_source(
    source: SourceSpec,
    body: bytes,
    *,
    previous_fingerprints: dict[str, str] | None = None,
) -> dict[str, Any]:
    previous_fingerprints = previous_fingerprints or {}
    text = (
        _normalized_document_text(body)
        if source.kind == "documentation"
        else body.decode("utf-8", errors="replace")
    )
    missing_markers = [marker for marker in source.markers if marker not in text]
    observed_value: str | None = None
    if source.parser in {"mcp_spec_release", "mcp_v1_pypi", "mcp_v1_release"}:
        observed_value, fingerprint = _structured_contract(source, body)
        byte_count = len(body)
        if observed_value is None:
            missing_markers.append("structured-contract")
    else:
        fingerprint, byte_count = _fingerprint(body, document=source.kind == "documentation")

    if missing_markers and source.kind == "contract":
        status = "contract_marker_missing"
    elif missing_markers:
        status = "review_needed"
    else:
        status = "ok"
    result: dict[str, Any] = {
        "source_id": source.source_id,
        "url": source.url,
        "kind": source.kind,
        "surface": source.surface,
        "status": status,
        "observed": True,
        "normalized_sha256": fingerprint,
        "response_bytes": byte_count,
        "missing_markers": missing_markers,
    }
    if observed_value is not None:
        result["observed_value"] = observed_value
    if missing_markers and source.kind == "documentation":
        result["drift_reason"] = "documentation_marker_missing"
    _apply_drift_status(result, source, previous_fingerprints)
    return result


_STATIC_ASSET_RE = re.compile(
    r"""["'](?P<url>(?:https://[^"']+|/[^"']+|[A-Za-z0-9_.-]+/[^"']+)"""
    r"""\.(?:js|mjs)(?:\?[^"']*)?)["']"""
)
_STATIC_PATH_PREFIXES = ("/assets/", "/cdn/assets/", "/_next/static/")
_STATIC_HOSTS = frozenset({"cdn.oaistatic.com", "chatgpt.com"})


def _static_asset_urls(base_url: str, body: bytes) -> list[str]:
    text = body.decode("utf-8", errors="replace").replace("\\/", "/")
    urls: list[str] = []
    seen: set[str] = set()
    for match in _STATIC_ASSET_RE.finditer(text):
        candidate = urljoin(base_url, match.group("url"))
        try:
            parsed = validate_url(candidate)
        except RadarError:
            continue
        if parsed.hostname not in _STATIC_HOSTS:
            continue
        if parsed.hostname == "chatgpt.com" and not parsed.path.startswith(_STATIC_PATH_PREFIXES):
            continue
        if candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    return urls


def _marker_presence(bodies: list[bytes]) -> dict[str, bool]:
    texts = [body.decode("utf-8", errors="replace") for body in bodies]
    return {
        group: any(variant in text for variant in variants for text in texts)
        for group, variants in CHATGPT_CONTRACT_MARKERS.items()
    }


def evaluate_chatgpt_bundles(
    source: SourceSpec,
    manifest_body: bytes,
    *,
    fetch_asset: Callable[[str], bytes],
    fallback_client_version: str,
    fallback_client_build: str,
    previous_fingerprints: dict[str, str] | None = None,
    max_assets: int = MAX_STATIC_ASSETS,
) -> dict[str, Any]:
    if source.parser != "chatgpt_bundles":
        raise RadarError("bundle evaluator requires the ChatGPT bundle parser")
    if not re.fullmatch(r"prod-[0-9a-f]{40}", fallback_client_version):
        raise RadarError("packaged fallback client version is invalid")
    if not re.fullmatch(r"[0-9]{6,10}", fallback_client_build):
        raise RadarError("packaged fallback client build is invalid")
    if not 1 <= max_assets <= MAX_STATIC_ASSETS:
        raise RadarError("static asset limit is outside the allowed bound")

    bodies = [manifest_body]
    fingerprints = [(source.url, _fingerprint(manifest_body)[0])]
    queue = _static_asset_urls(source.url, manifest_body)
    discovered = set(queue)
    attempted_assets = 0
    observed_assets = 0
    failed_assets = 0
    total_bytes = len(manifest_body)
    coverage_incomplete = False

    while queue and attempted_assets < max_assets:
        asset_url = queue.pop(0)
        attempted_assets += 1
        try:
            body = fetch_asset(asset_url)
            if len(body) > MAX_BYTES:
                raise RadarError("static asset is too large")
            if total_bytes + len(body) > MAX_TOTAL_BUNDLE_BYTES:
                coverage_incomplete = True
                break
        except RadarError:
            failed_assets += 1
            coverage_incomplete = True
            continue
        observed_assets += 1
        total_bytes += len(body)
        bodies.append(body)
        fingerprints.append((asset_url, _fingerprint(body)[0]))
        for nested in _static_asset_urls(asset_url, body):
            if nested not in discovered:
                discovered.add(nested)
                queue.append(nested)

        marker_presence = _marker_presence(bodies)
        combined = [chunk.decode("utf-8", errors="replace") for chunk in bodies]
        version_present = any(fallback_client_version in text for text in combined)
        build_present = any(fallback_client_build in text for text in combined)
        if all(marker_presence.values()) and version_present and build_present:
            queue.clear()
            break

    if queue:
        coverage_incomplete = True
    marker_presence = _marker_presence(bodies)
    missing_groups = sorted(group for group, present in marker_presence.items() if not present)
    text_bodies = [chunk.decode("utf-8", errors="replace") for chunk in bodies]
    fallback_current = any(fallback_client_version in text for text in text_bodies) and any(
        fallback_client_build in text for text in text_bodies
    )

    if failed_assets or coverage_incomplete:
        status = "fetch_failed"
    elif missing_groups:
        status = "contract_marker_missing"
    elif not fallback_current:
        status = "fallback_metadata_stale"
    else:
        status = "ok"
    fingerprint = _sha256_text(canonical_json(fingerprints).strip())
    result: dict[str, Any] = {
        "source_id": source.source_id,
        "url": source.url,
        "kind": source.kind,
        "surface": source.surface,
        "status": status,
        "observed": True,
        "normalized_sha256": fingerprint,
        "response_bytes": total_bytes,
        "static_assets_discovered": len(discovered),
        "static_assets_attempted": attempted_assets,
        "static_assets_observed": observed_assets,
        "static_assets_failed": failed_assets,
        "missing_marker_groups": missing_groups,
        "observed_marker_group_count": len(marker_presence) - len(missing_groups),
        "expected_marker_group_count": len(marker_presence),
        "fallback_metadata_status": "current" if fallback_current else "stale",
        "executed_downloaded_code": False,
    }
    _apply_drift_status(result, source, previous_fingerprints or {})
    return result


def failed_source(source: SourceSpec, error: RadarError) -> dict[str, Any]:
    status = "fetch_failed" if source.kind == "contract" else "review_needed"
    result: dict[str, Any] = {
        "source_id": source.source_id,
        "url": source.url,
        "kind": source.kind,
        "surface": source.surface,
        "status": status,
        "observed": False,
        "error_class": type(error).__name__,
    }
    if source.kind == "documentation":
        result["drift_reason"] = "fetch_failed"
    return result


def load_previous_fingerprints(path: Path) -> dict[str, str]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RadarError("previous radar report could not be read") from error
    if len(raw) > MAX_PREVIOUS_REPORT_BYTES:
        raise RadarError("previous radar report is too large")
    try:
        report = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RadarError("previous radar report is invalid JSON") from error
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != "1"
        or report.get("scope") != "public_surface_drift"
        or not isinstance(report.get("results"), list)
        or len(report["results"]) > 256
    ):
        raise RadarError("previous radar report has an invalid envelope")
    fingerprints: dict[str, str] = {}
    for item in report["results"]:
        if not isinstance(item, dict):
            raise RadarError("previous radar result must be an object")
        source_id = item.get("source_id")
        fingerprint = item.get("normalized_sha256")
        if fingerprint is None:
            continue
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", source_id)
            or not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or source_id in fingerprints
        ):
            raise RadarError("previous radar fingerprint is invalid")
        fingerprints[source_id] = fingerprint
    return fingerprints


def _fallback_metadata(path: Path = PROJECT_ROOT / "gpt2agent" / "backend.py") -> tuple[str, str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as error:
        raise RadarError("packaged fallback metadata could not be read") from error
    values: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id in {"_CLIENT_BUILD", "_CLIENT_VERSION"}
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            values[target.id] = node.value.value
    version = values.get("_CLIENT_VERSION", "")
    build = values.get("_CLIENT_BUILD", "")
    if not re.fullmatch(r"prod-[0-9a-f]{40}", version) or not re.fullmatch(r"[0-9]{6,10}", build):
        raise RadarError("packaged fallback metadata is invalid")
    return version, build


def build_report(results: list[dict[str, Any]], *, observed_at: str) -> dict[str, Any]:
    ordered = sorted(results, key=lambda result: result["source_id"])
    observed = sum(result.get("observed") is True for result in ordered)
    failed = len(ordered) - observed
    hard_failures = sum(result["status"] in HARD_FAILURE_STATUSES for result in ordered)
    chatgpt_results = [result for result in ordered if result.get("surface") == "chatgpt"]
    chatgpt_observed = sum(result.get("observed") is True for result in chatgpt_results)
    insufficient_chatgpt = bool(chatgpt_results) and chatgpt_observed == 0
    if insufficient_chatgpt:
        coverage_status = "insufficient_coverage"
    elif failed:
        coverage_status = "partial"
    else:
        coverage_status = "complete"
    exit_code = int(hard_failures > 0 or insufficient_chatgpt)
    if exit_code:
        status = "failed"
    elif failed or any(result["status"] == "review_needed" for result in ordered):
        status = "review_needed"
    else:
        status = "ok"
    return {
        "schema_version": "1",
        "scope": "public_surface_drift",
        "observed_at": observed_at,
        "account_contract_status": "not_checked",
        "private_adapter_status": "not_checked",
        "coverage_status": coverage_status,
        "status": status,
        "coverage": {
            "expected_sources": len(ordered),
            "observed_sources": observed,
            "failed_sources": failed,
            "hard_failure_sources": hard_failures,
            "chatgpt_expected_sources": len(chatgpt_results),
            "chatgpt_observed_sources": chatgpt_observed,
        },
        "exit_code": exit_code,
        "results": ordered,
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def markdown_report(report: dict[str, Any]) -> str:
    coverage = report["coverage"]
    lines = [
        "# Public surface radar",
        "",
        f"Observed: `{report['observed_at']}`",
        f"Result: `{report['status']}`",
        f"Coverage: `{report['coverage_status']}`",
        "",
        "Account contract status: `not_checked`",
        "Private adapter status: `not_checked`",
        "",
        (
            "Observed sources: "
            f"`{coverage['observed_sources']}/{coverage['expected_sources']}`; "
            f"failed observations: `{coverage['failed_sources']}`; "
            f"hard failures: `{coverage['hard_failure_sources']}`."
        ),
        (
            "Observable ChatGPT sources: "
            f"`{coverage['chatgpt_observed_sources']}/"
            f"{coverage['chatgpt_expected_sources']}`."
        ),
        "",
        (
            "This public-only result does not check an account, private adapter "
            "reachability, entitlement, or release readiness."
        ),
        "",
        "| Source | Surface | Kind | Status | Observed value |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in report["results"]:
        lines.append(
            f"| `{result['source_id']}` | {result.get('surface', 'other')} | "
            f"{result['kind']} | `{result['status']}` | "
            f"`{result.get('observed_value', '-')}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--previous-report", type=Path)
    return parser


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RadarError("total runtime budget exhausted")
    return min(float(TIMEOUT_SECONDS), remaining)


def main() -> int:
    args = _parser().parse_args()
    previous_fingerprints: dict[str, str] = {}
    baseline_result: dict[str, Any] | None = None
    if args.previous_report is not None:
        try:
            previous_fingerprints = load_previous_fingerprints(args.previous_report)
        except RadarError as error:
            baseline_result = {
                "source_id": "previous-radar-artifact",
                "url": "local://redacted",
                "kind": "documentation",
                "surface": "radar",
                "status": "review_needed",
                "observed": False,
                "drift_reason": "invalid_previous_artifact",
                "error_class": type(error).__name__,
            }

    deadline = time.monotonic() + TOTAL_RUNTIME_SECONDS
    results: list[dict[str, Any]] = []
    if baseline_result is not None:
        results.append(baseline_result)
    try:
        fallback_client_version, fallback_client_build = _fallback_metadata()
    except RadarError:
        fallback_client_version = ""
        fallback_client_build = ""

    for source in SOURCES:
        try:
            body = fetch_source(source, timeout_seconds=_remaining_timeout(deadline))
            if source.parser == "chatgpt_bundles":
                if not fallback_client_version or not fallback_client_build:
                    raise RadarError("packaged fallback metadata is unavailable")

                def fetch_asset(url: str) -> bytes:
                    asset = SourceSpec(
                        "chatgpt-static-asset",
                        url,
                        "contract",
                        surface="chatgpt",
                    )
                    return fetch_source(asset, timeout_seconds=_remaining_timeout(deadline))

                results.append(
                    evaluate_chatgpt_bundles(
                        source,
                        body,
                        fetch_asset=fetch_asset,
                        fallback_client_version=fallback_client_version,
                        fallback_client_build=fallback_client_build,
                        previous_fingerprints=previous_fingerprints,
                    )
                )
            else:
                results.append(
                    evaluate_source(
                        source,
                        body,
                        previous_fingerprints=previous_fingerprints,
                    )
                )
        except RadarError as error:
            results.append(failed_source(source, error))

    observed_at = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    report = build_report(results, observed_at=observed_at)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(canonical_json(report), encoding="utf-8")
    args.markdown_output.write_text(markdown_report(report), encoding="utf-8")
    for result in report["results"]:
        if result["status"] in {"fetch_failed", "review_needed"}:
            print(f"::warning title=Public surface review needed::{result['source_id']}")
        elif result["status"] in HARD_FAILURE_STATUSES:
            print(f"::error title=Public contract check failed::{result['source_id']}")
    if report["coverage_status"] == "insufficient_coverage":
        print(
            "::error title=Public ChatGPT coverage insufficient::"
            "No ChatGPT public surface was observable"
        )
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
