#!/usr/bin/env python3
"""Publish one fully validated GitHub Release draft by exact numeric ID."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import ssl
import stat
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_NOTES_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 128 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 256 * 1024 * 1024
MAX_TOKEN_BYTES = 4096
MAX_TOKEN_PAIR_BYTES = MAX_TOKEN_BYTES * 2 + 1
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)[0-9]+)?\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?\Z")
POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*\Z")
RELEASE_API_PATH = re.compile(
    r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/[1-9][0-9]*"
    r"(?:/assets\?per_page=100)?\Z"
)
SETTINGS_API_PATH = re.compile(
    r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/immutable-releases\Z"
)


@dataclass(frozen=True)
class ExpectedAsset:
    """One local asset in the closed publication set."""

    size: int
    sha256: str


JsonRequester = Callable[[str, str, str, object | None], object]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _require_match(pattern: re.Pattern[str], value: str, label: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _parse_release_id(value: str) -> int:
    if not POSITIVE_INTEGER.fullmatch(value):
        raise ValueError("release ID must be a positive integer")
    parsed = int(value)
    if parsed > 2**53 - 1:
        raise ValueError("release ID exceeds the exact integer range")
    return parsed


def _parse_bool(value: str) -> bool:
    if value not in {"true", "false"}:
        raise ValueError("expected-prerelease must be true or false")
    return value == "true"


def _validate_token(token: str) -> str:
    try:
        encoded = token.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("GitHub token is invalid") from None
    if (
        not encoded
        or len(encoded) > MAX_TOKEN_BYTES
        or any(unicodedata.category(character).startswith("C") for character in token)
    ):
        raise ValueError("GitHub token is invalid")
    return token


def _read_single_token() -> str:
    raw_token = sys.stdin.buffer.read(MAX_TOKEN_BYTES + 1)
    if len(raw_token) > MAX_TOKEN_BYTES:
        raise ValueError("GitHub token is invalid")
    try:
        return _validate_token(raw_token.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError("GitHub token is invalid") from None


def _read_distinct_token_pair() -> tuple[str, str]:
    raw_pair = sys.stdin.buffer.read(MAX_TOKEN_PAIR_BYTES + 1)
    if len(raw_pair) > MAX_TOKEN_PAIR_BYTES or raw_pair.count(b"\x00") != 1:
        raise ValueError("GitHub token pair is invalid")
    release_bytes, settings_bytes = raw_pair.split(b"\x00")
    if len(release_bytes) > MAX_TOKEN_BYTES or len(settings_bytes) > MAX_TOKEN_BYTES:
        raise ValueError("GitHub token pair is invalid")
    try:
        release_token = _validate_token(release_bytes.decode("utf-8"))
        settings_token = _validate_token(settings_bytes.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError("GitHub token pair is invalid") from None
    if hmac.compare_digest(release_bytes, settings_bytes):
        raise ValueError("GitHub tokens must be distinct")
    return release_token, settings_token


def _require_directory(path: Path, label: str) -> list[Path]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} does not exist") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory, not a symlink")
    return list(path.iterdir())


def _require_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} does not exist") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file, not a symlink")
    return metadata


def _sha256(path: Path, expected_size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        read_size = 0
        while True:
            chunk = stream.read(min(1024 * 1024, expected_size - read_size + 1))
            if not chunk:
                break
            read_size += len(chunk)
            if read_size > expected_size:
                raise ValueError("release artifact changed while being hashed")
            digest.update(chunk)
    if read_size != expected_size:
        raise ValueError("release artifact changed while being hashed")
    return digest.hexdigest()


def _read_notes(path: Path) -> str:
    metadata = _require_regular_file(path, "release notes")
    if metadata.st_size > MAX_NOTES_BYTES:
        raise ValueError("release notes exceed size limit")
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_NOTES_BYTES + 1)
        if len(payload) != metadata.st_size or len(payload) > MAX_NOTES_BYTES:
            raise ValueError("release notes changed while being read")
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("release notes are not valid UTF-8") from None


def expected_release_assets(
    dist: Path,
    evidence: Path,
    version: str,
) -> dict[str, ExpectedAsset]:
    """Close over the exact local wheel, sdist, and evidence file."""
    normalized_version = _require_match(VERSION, version, "distribution version")
    wheel_name = f"gpt2agent-{normalized_version}-py3-none-any.whl"
    sdist_name = f"gpt2agent-{normalized_version}.tar.gz"
    entries = _require_directory(dist, "distribution directory")
    if {path.name for path in entries} != {wheel_name, sdist_name} or len(entries) != 2:
        raise ValueError("distribution directory does not contain the exact release files")
    if evidence.name != "release-workflow-artifacts.json":
        raise ValueError("release evidence has an unexpected filename")

    paths = [dist / wheel_name, dist / sdist_name, evidence]
    expected: dict[str, ExpectedAsset] = {}
    total_size = 0
    for path in paths:
        metadata = _require_regular_file(path, "release artifact")
        if metadata.st_size > MAX_ASSET_BYTES:
            raise ValueError("release asset exceeds size limit")
        total_size += metadata.st_size
        expected[path.name] = ExpectedAsset(metadata.st_size, _sha256(path, metadata.st_size))
    if total_size > MAX_TOTAL_ASSET_BYTES:
        raise ValueError("release asset set exceeds total size limit")
    return expected


def verify_release_state(
    release: object,
    assets: object,
    *,
    release_id: int,
    tag: str,
    body: str,
    prerelease: bool,
    expected_assets: Mapping[str, ExpectedAsset],
    expected_draft: bool,
) -> None:
    """Require one exact draft or one exact published immutable release."""
    if not isinstance(release, dict):
        raise ValueError("GitHub Release response must be an object")
    if release.get("id") != release_id:
        raise ValueError("GitHub Release ID does not match the requested draft")
    if release.get("tag_name") != tag:
        raise ValueError("GitHub Release tag does not match")
    if release.get("name") != tag:
        raise ValueError("GitHub Release name does not match")
    if release.get("body") != body:
        raise ValueError("GitHub Release body does not exactly match")
    if release.get("prerelease") is not prerelease:
        raise ValueError("GitHub Release prerelease flag does not match")
    if release.get("draft") is not expected_draft:
        raise ValueError("GitHub Release draft state does not match")
    expected_immutable = not expected_draft
    if release.get("immutable") is not expected_immutable:
        raise ValueError("GitHub Release immutable state does not match")
    if not isinstance(assets, list):
        raise ValueError("GitHub Release assets response must be an array")

    names: list[str] = []
    identifiers: set[int] = set()
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise ValueError("GitHub Release asset metadata is malformed")
        names.append(asset["name"])
        identifier = _require_positive_int(asset.get("id"), "GitHub Release asset ID")
        if identifier in identifiers:
            raise ValueError("GitHub Release assets must have unique IDs")
        identifiers.add(identifier)
    if len(names) != len(set(names)):
        raise ValueError("GitHub Release has duplicate asset names")
    if set(names) != set(expected_assets) or len(names) != len(expected_assets):
        raise ValueError("GitHub Release does not have the exact asset set")
    for asset in assets:
        expected = expected_assets[asset["name"]]
        if asset.get("state") != "uploaded":
            raise ValueError("GitHub Release asset is not uploaded")
        if asset.get("size") != expected.size:
            raise ValueError("GitHub Release asset size does not match")
        if asset.get("digest") != f"sha256:{expected.sha256}":
            raise ValueError("GitHub Release asset digest does not match")


def _perform_json_request(
    method: str,
    path: str,
    token: str,
    payload: object | None,
) -> object:
    if method not in {"GET", "PATCH"} or not path.startswith("/repos/"):
        raise ValueError("disallowed GitHub API request")
    _validate_token(token)
    body = None
    if method == "PATCH":
        if payload != {"draft": False}:
            raise ValueError("disallowed GitHub Release mutation")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    elif payload is not None:
        raise ValueError("GET request must not have a body")

    request = Request(
        f"{API_ROOT}{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "gpt2agent-exact-release-publisher",
            "X-GitHub-Api-Version": API_VERSION,
        },
    )
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )
    try:
        with opener.open(request, timeout=30) as response:
            if response.geturl() != f"{API_ROOT}{path}":
                raise ValueError("GitHub API redirected unexpectedly")
            data = response.read(MAX_JSON_BYTES + 1)
    except HTTPError as error:
        raise ValueError(f"GitHub API request failed with HTTP {error.code}") from None
    except URLError:
        raise ValueError("GitHub API request failed") from None
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("GitHub API response exceeds size limit")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("GitHub API returned invalid JSON") from None


def _request_release_json(
    method: str,
    path: str,
    token: str,
    payload: object | None,
) -> object:
    if not RELEASE_API_PATH.fullmatch(path):
        raise ValueError("disallowed GitHub Release API request")
    is_assets = path.endswith("/assets?per_page=100")
    if is_assets and (method != "GET" or payload is not None):
        raise ValueError("disallowed GitHub Release API request")
    if not is_assets and method not in {"GET", "PATCH"}:
        raise ValueError("disallowed GitHub Release API request")
    return _perform_json_request(method, path, token, payload)


def _request_settings_json(
    method: str,
    path: str,
    token: str,
    payload: object | None,
) -> object:
    if (
        method != "GET"
        or payload is not None
        or not SETTINGS_API_PATH.fullmatch(path)
    ):
        raise ValueError("disallowed immutable-release settings API request")
    return _perform_json_request(method, path, token, payload)


def _fetch_state(
    repository: str,
    release_id: int,
    token: str,
    request_json: JsonRequester,
) -> tuple[object, object]:
    base = f"/repos/{repository}/releases/{release_id}"
    release = request_json("GET", base, token, None)
    assets = request_json("GET", f"{base}/assets?per_page=100", token, None)
    return release, assets


def require_immutable_releases_enabled(
    repository: str,
    token: str,
    *,
    request_json: JsonRequester = _request_settings_json,
) -> None:
    normalized_repository = _require_match(REPOSITORY, repository, "repository")
    settings = request_json(
        "GET",
        f"/repos/{normalized_repository}/immutable-releases",
        token,
        None,
    )
    if (
        not isinstance(settings, dict)
        or set(settings) != {"enabled", "enforced_by_owner"}
        or settings.get("enabled") is not True
        or not isinstance(settings.get("enforced_by_owner"), bool)
    ):
        raise ValueError("GitHub immutable-release setting is not an exact enabled response")


def publish_exact_release(
    repository: str,
    release_id: int,
    tag: str,
    body: str,
    prerelease: bool,
    expected_assets: Mapping[str, ExpectedAsset],
    release_token: str,
    settings_token: str,
    *,
    release_request_json: JsonRequester = _request_release_json,
    settings_request_json: JsonRequester = _request_settings_json,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Validate twice, publish only by numeric ID, and read back immutability."""
    normalized_repository = _require_match(REPOSITORY, repository, "repository")
    normalized_release_id = _require_positive_int(release_id, "release ID")
    normalized_tag = _require_match(TAG, tag, "release tag")
    validated_release_token = _validate_token(release_token)
    validated_settings_token = _validate_token(settings_token)
    if hmac.compare_digest(
        validated_release_token.encode("utf-8"),
        validated_settings_token.encode("utf-8"),
    ):
        raise ValueError("GitHub tokens must be distinct")

    release, assets = _fetch_state(
        normalized_repository,
        normalized_release_id,
        validated_release_token,
        release_request_json,
    )
    if isinstance(release, dict) and release.get("draft") is False:
        verify_release_state(
            release,
            assets,
            release_id=normalized_release_id,
            tag=normalized_tag,
            body=body,
            prerelease=prerelease,
            expected_assets=expected_assets,
            expected_draft=False,
        )
        return "already-published"
    verify_release_state(
        release,
        assets,
        release_id=normalized_release_id,
        tag=normalized_tag,
        body=body,
        prerelease=prerelease,
        expected_assets=expected_assets,
        expected_draft=True,
    )
    require_immutable_releases_enabled(
        normalized_repository,
        validated_settings_token,
        request_json=settings_request_json,
    )

    # Close the observable validation/mutation window as much as the REST API permits.
    release, assets = _fetch_state(
        normalized_repository,
        normalized_release_id,
        validated_release_token,
        release_request_json,
    )
    verify_release_state(
        release,
        assets,
        release_id=normalized_release_id,
        tag=normalized_tag,
        body=body,
        prerelease=prerelease,
        expected_assets=expected_assets,
        expected_draft=True,
    )
    require_immutable_releases_enabled(
        normalized_repository,
        validated_settings_token,
        request_json=settings_request_json,
    )

    path = f"/repos/{normalized_repository}/releases/{normalized_release_id}"
    try:
        release_request_json(
            "PATCH", path, validated_release_token, {"draft": False}
        )
    except Exception:
        # The server may have accepted a PATCH before the transport failed.
        release, assets = _fetch_state(
            normalized_repository,
            normalized_release_id,
            validated_release_token,
            release_request_json,
        )
        if isinstance(release, dict) and release.get("draft") is False:
            verify_release_state(
                release,
                assets,
                release_id=normalized_release_id,
                tag=normalized_tag,
                body=body,
                prerelease=prerelease,
                expected_assets=expected_assets,
                expected_draft=False,
            )
            return "published-after-ambiguous-response"
        verify_release_state(
            release,
            assets,
            release_id=normalized_release_id,
            tag=normalized_tag,
            body=body,
            prerelease=prerelease,
            expected_assets=expected_assets,
            expected_draft=True,
        )
        require_immutable_releases_enabled(
            normalized_repository,
            validated_settings_token,
            request_json=settings_request_json,
        )
        release_request_json(
            "PATCH", path, validated_release_token, {"draft": False}
        )

    last_error = "published release was not observable"
    for attempt in range(1, 8):
        try:
            release, assets = _fetch_state(
                normalized_repository,
                normalized_release_id,
                validated_release_token,
                release_request_json,
            )
            verify_release_state(
                release,
                assets,
                release_id=normalized_release_id,
                tag=normalized_tag,
                body=body,
                prerelease=prerelease,
                expected_assets=expected_assets,
                expected_draft=False,
            )
            return "published"
        except ValueError as error:
            last_error = str(error)
            if attempt < 7:
                sleep(1)
    raise ValueError(f"published release did not become exact and immutable: {last_error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-stdin", action="store_true")
    parser.add_argument("--settings-token-stdin", action="store_true")
    parser.add_argument("--settings-preflight", action="store_true")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-id")
    parser.add_argument("--tag")
    parser.add_argument("--version")
    parser.add_argument("--expected-prerelease")
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--evidence", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.settings_preflight:
            if args.token_stdin or not args.settings_token_stdin:
                raise ValueError("settings preflight token must be read from standard input")
            if any(
                value is not None
                for value in (
                    args.release_id,
                    args.tag,
                    args.version,
                    args.expected_prerelease,
                    args.notes,
                    args.dist,
                    args.evidence,
                )
            ):
                raise ValueError("settings preflight does not accept publication inputs")
            token = _read_single_token()
            require_immutable_releases_enabled(args.repository, token)
            print("immutable GitHub Release settings preflight succeeded")
            return 0
        if not args.token_stdin:
            raise ValueError("GitHub tokens must be read from standard input")
        if args.settings_token_stdin:
            raise ValueError("publication requires the dual-token standard input mode")
        if any(
            value is None
            for value in (
                args.release_id,
                args.tag,
                args.version,
                args.expected_prerelease,
                args.notes,
                args.dist,
                args.evidence,
            )
        ):
            raise ValueError("publication inputs are incomplete")
        release_token, settings_token = _read_distinct_token_pair()
        release_id = _parse_release_id(args.release_id)
        prerelease = _parse_bool(args.expected_prerelease)
        expected_body = _read_notes(args.notes)
        expected_assets = expected_release_assets(args.dist, args.evidence, args.version)
        status = publish_exact_release(
            args.repository,
            release_id,
            args.tag,
            expected_body,
            prerelease,
            expected_assets,
            release_token,
            settings_token,
        )
    except ValueError as error:
        print(f"exact GitHub Release publication failed: {error}", file=sys.stderr)
        return 1
    print(f"exact GitHub Release publication succeeded: id={release_id} state={status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
