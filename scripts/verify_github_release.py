#!/usr/bin/env python3
"""Fail closed unless a public GitHub Release matches promoted artifacts.

Release notes are compared byte-for-byte after UTF-8 decoding. No newline or
whitespace normalization is applied: the uploaded ``release_notes.md`` is the
exact body contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlsplit
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"
MAX_PAGES = 100
MAX_ASSETS = 1000
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_NOTES_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 128 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 256 * 1024 * 1024
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)[0-9]+)?\Z")
POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*\Z")


@dataclass(frozen=True)
class ExpectedAsset:
    """One exact local artifact promoted to the public release."""

    path: Path
    size: int
    sha256: str


JsonFetcher = Callable[[str, str], tuple[Any, Mapping[str, str]]]
AssetDownloader = Callable[[str, int, Path, str, ExpectedAsset], None]


def _require_repository(repository: str) -> str:
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("invalid GitHub repository")
    return repository


def _require_tag(tag: str) -> str:
    if not TAG.fullmatch(tag):
        raise ValueError("invalid GitHub Release tag")
    return tag


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _require_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} does not exist") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file, not a symlink")
    return metadata


def _require_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} does not exist") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory, not a symlink")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_release_assets(dist: Path, evidence: Path) -> dict[str, ExpectedAsset]:
    """Return the exact wheel, sdist, and evidence file closed over local inputs."""
    _require_directory(dist, "distribution directory")
    entries = list(dist.iterdir())
    wheels = [path for path in entries if path.name.endswith(".whl")]
    sdists = [path for path in entries if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one sdist")
    paths = [wheels[0], sdists[0]]
    if set(entries) != set(paths):
        raise ValueError("expected exactly one wheel and one sdist and no other entries")
    if evidence.name != "release-workflow-artifacts.json":
        raise ValueError("release evidence has an unexpected filename")
    _require_regular_file(evidence, "release evidence")
    metadata: dict[Path, os.stat_result] = {}
    total_size = 0
    for path in [*paths, evidence]:
        item = _require_regular_file(path, "release artifacts")
        if item.st_size > MAX_ASSET_BYTES:
            raise ValueError("release asset exceeds size limit")
        total_size += item.st_size
        metadata[path] = item
    if total_size > MAX_TOTAL_ASSET_BYTES:
        raise ValueError("release asset set exceeds total size limit")
    if evidence.name in {path.name for path in paths}:
        raise ValueError("release asset filenames must be unique")
    return {
        path.name: ExpectedAsset(
            path=path,
            size=metadata[path].st_size,
            sha256=_sha256(path),
        )
        for path in [*paths, evidence]
    }


def verify_release_metadata(
    release: object,
    assets: object,
    *,
    tag: str,
    expected_body: str,
    expected_prerelease: bool,
    expected_assets: Mapping[str, ExpectedAsset],
) -> dict[str, int]:
    """Verify exact release state and complete API asset metadata."""
    expected_tag = _require_tag(tag)
    if not isinstance(release, dict):
        raise ValueError("GitHub Release response must be an object")
    _require_positive_int(release.get("id"), "GitHub Release ID")
    if release.get("tag_name") != expected_tag:
        raise ValueError("GitHub Release tag does not match the expected tag")
    if release.get("name") != expected_tag:
        raise ValueError("GitHub Release name does not match the expected tag")
    if release.get("body") != expected_body:
        raise ValueError("GitHub Release body does not exactly match release notes")
    if release.get("draft") is not False:
        raise ValueError("GitHub Release must not be a draft")
    if release.get("prerelease") is not expected_prerelease:
        raise ValueError("GitHub Release prerelease flag does not match")
    if release.get("immutable") is not True:
        raise ValueError("GitHub Release must be immutable")
    if not isinstance(assets, list):
        raise ValueError("GitHub Release assets response must be an array")

    names: list[str] = []
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise ValueError("GitHub Release asset metadata is malformed")
        names.append(asset["name"])
    if len(names) != len(set(names)):
        raise ValueError("GitHub Release has a duplicate asset name")
    if set(names) != set(expected_assets):
        raise ValueError("GitHub Release does not have the exact GitHub Release asset names")

    asset_ids: dict[str, int] = {}
    seen_ids: set[int] = set()
    for asset in assets:
        name = asset["name"]
        expected = expected_assets[name]
        asset_id = _require_positive_int(asset.get("id"), "GitHub Release asset ID")
        if asset_id in seen_ids:
            raise ValueError("GitHub Release assets must have unique IDs")
        seen_ids.add(asset_id)
        if asset.get("state") != "uploaded":
            raise ValueError("GitHub Release asset is not in uploaded state")
        size = asset.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size != expected.size:
            raise ValueError(f"GitHub Release asset size does not match for {name}")
        if asset.get("digest") != f"sha256:{expected.sha256}":
            raise ValueError(f"GitHub Release asset digest does not match for {name}")
        asset_ids[name] = asset_id
    return asset_ids


def _request(url: str, token: str, *, accept: str) -> Request:
    request = Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "gpt2agent-release-verifier",
            "X-GitHub-Api-Version": API_VERSION,
        },
    )
    # Keep credentials off cross-origin redirects to GitHub's asset storage.
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    return request


def _fetch_json(
    url: str,
    token: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> tuple[Any, Mapping[str, str]]:
    request = _request(url, token, accept="application/vnd.github+json")
    try:
        with opener(request, timeout=30) as response:
            headers = response.headers
            declared_size = _content_length(headers)
            if declared_size is not None and declared_size > MAX_JSON_BYTES:
                raise ValueError("GitHub API JSON response exceeds size limit")
            payload = response.read(MAX_JSON_BYTES + 1)
            if len(payload) > MAX_JSON_BYTES:
                raise ValueError("GitHub API JSON response exceeds size limit")
    except HTTPError as error:
        raise ValueError(f"GitHub API request failed with HTTP {error.code}") from None
    except URLError:
        raise ValueError("GitHub API request failed") from None
    try:
        return json.loads(payload.decode("utf-8")), headers
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("GitHub API returned invalid JSON") from None


def _link_header(headers: Mapping[str, str]) -> str | None:
    values = [value for key, value in headers.items() if key.lower() == "link"]
    if len(values) > 1:
        raise ValueError("GitHub API returned duplicate pagination Link headers")
    return values[0] if values else None


def _content_length(headers: Mapping[str, str]) -> int | None:
    values = [value for key, value in headers.items() if key.lower() == "content-length"]
    if not values:
        return None
    if len(values) != 1 or not re.fullmatch(r"0|[1-9][0-9]*", values[0]):
        raise ValueError("GitHub response has malformed Content-Length")
    return int(values[0])


def _next_link(headers: Mapping[str, str]) -> str | None:
    link = _link_header(headers)
    if link is None:
        return None
    next_urls: list[str] = []
    for segment in link.split(","):
        match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="([^"]+)"\s*', segment)
        if match is None:
            raise ValueError("GitHub API returned a malformed pagination Link header")
        if "next" in match.group(2).split():
            next_urls.append(match.group(1))
    if len(next_urls) > 1:
        raise ValueError("GitHub API returned multiple next pagination links")
    return next_urls[0] if next_urls else None


def _validate_next_url(url: str, repository: str, release_id: int) -> str:
    parts = urlsplit(url)
    expected_path = f"/repos/{repository}/releases/{release_id}/assets"
    try:
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ValueError("untrusted GitHub pagination URL") from None
    if (
        parts.scheme != "https"
        or parts.netloc != "api.github.com"
        or parts.username is not None
        or parts.password is not None
        or parts.port is not None
        or parts.path != expected_path
        or parts.fragment
        or len(query) != 2
        or len({key for key, _ in query}) != 2
        or {key for key, _ in query} != {"per_page", "page"}
    ):
        raise ValueError("untrusted GitHub pagination URL")
    values = dict(query)
    if values["per_page"] != "100" or not POSITIVE_INTEGER.fullmatch(values["page"]):
        raise ValueError("untrusted GitHub pagination URL")
    return url


def collect_release_assets(
    repository: str,
    release_id: int,
    token: str,
    *,
    fetch_json: JsonFetcher = _fetch_json,
) -> list[dict[str, Any]]:
    """Enumerate every release asset while validating each authenticated page URL."""
    normalized_repository = _require_repository(repository)
    normalized_release_id = _require_positive_int(release_id, "GitHub Release ID")
    url: str | None = (
        f"{API_ROOT}/repos/{normalized_repository}/releases/"
        f"{normalized_release_id}/assets?per_page=100"
    )
    seen: set[str] = set()
    collected: list[dict[str, Any]] = []
    while url is not None:
        if url in seen:
            raise ValueError("GitHub Release asset pagination cycle detected")
        if len(seen) >= MAX_PAGES:
            raise ValueError("GitHub Release asset pagination exceeds page limit")
        seen.add(url)
        payload, headers = fetch_json(url, token)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("GitHub Release assets response must be an array of objects")
        if len(collected) + len(payload) > MAX_ASSETS:
            raise ValueError("GitHub Release asset collection exceeds item limit")
        collected.extend(payload)
        next_url = _next_link(headers)
        url = (
            _validate_next_url(next_url, normalized_repository, normalized_release_id)
            if next_url is not None
            else None
        )
    return collected


def _download_asset(
    repository: str,
    asset_id: int,
    destination: Path,
    token: str,
    expected: ExpectedAsset,
    *,
    opener: Callable[..., Any] = urlopen,
) -> None:
    normalized_repository = _require_repository(repository)
    normalized_asset_id = _require_positive_int(asset_id, "GitHub Release asset ID")
    url = f"{API_ROOT}/repos/{normalized_repository}/releases/assets/{normalized_asset_id}"
    request = _request(url, token, accept="application/octet-stream")
    if isinstance(expected.size, bool) or not isinstance(expected.size, int) or expected.size < 0:
        raise ValueError("expected GitHub Release asset size is invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        with opener(request, timeout=30) as response:
            declared_size = _content_length(response.headers)
            if declared_size is not None and declared_size != expected.size:
                raise ValueError("GitHub asset Content-Length does not match expected size")
            descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                written = 0
                while True:
                    remaining_with_sentinel = expected.size - written + 1
                    chunk = response.read(min(1024 * 1024, remaining_with_sentinel))
                    if not chunk:
                        break
                    if written + len(chunk) > expected.size:
                        raise ValueError("GitHub asset download exceeds expected size")
                    stream.write(chunk)
                    written += len(chunk)
                if written != expected.size:
                    raise ValueError("GitHub asset download is smaller than expected size")
    except HTTPError as error:
        destination.unlink(missing_ok=True)
        raise ValueError(f"GitHub asset request failed with HTTP {error.code}") from None
    except URLError:
        destination.unlink(missing_ok=True)
        raise ValueError("GitHub asset request failed") from None
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def verify_downloaded_assets(
    directory: Path,
    expected_assets: Mapping[str, ExpectedAsset],
) -> None:
    """Verify a closed download directory by exact name, type, size, and bytes."""
    _require_directory(directory, "GitHub Release download directory")
    entries = list(directory.iterdir())
    names = [path.name for path in entries]
    if len(names) != len(set(names)) or set(names) != set(expected_assets):
        raise ValueError("GitHub Release download directory lacks exact downloaded asset names")
    for path in entries:
        metadata = _require_regular_file(path, "downloaded release assets")
        expected = expected_assets[path.name]
        if metadata.st_size != expected.size:
            raise ValueError(f"downloaded size does not match for {path.name}")
        if _sha256(path) != expected.sha256:
            raise ValueError(f"downloaded SHA-256 does not match for {path.name}")


def _read_notes(path: Path) -> str:
    metadata = _require_regular_file(path, "release notes")
    if metadata.st_size > MAX_NOTES_BYTES:
        raise ValueError("release notes exceed size limit")
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_NOTES_BYTES + 1)
        if len(payload) > MAX_NOTES_BYTES:
            raise ValueError("release notes exceed size limit")
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("release notes are not valid UTF-8") from None


def verify_public_release(
    repository: str,
    tag: str,
    notes: Path,
    dist: Path,
    evidence: Path,
    download_root: Path,
    *,
    expected_prerelease: bool,
    token: str,
    attempts: int = 7,
    delay: float = 10,
    fetch_json: JsonFetcher = _fetch_json,
    download_asset: AssetDownloader = _download_asset,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Poll and verify one exact public release, including downloaded bytes."""
    normalized_repository = _require_repository(repository)
    normalized_tag = _require_tag(tag)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 100:
        raise ValueError("attempts must be between 1 and 100")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 3600:
        raise ValueError("delay must be between 0 and 3600 seconds")
    expected_assets = expected_release_assets(dist, evidence)
    expected_body = _read_notes(notes)
    if download_root.exists() or download_root.is_symlink():
        raise ValueError("GitHub Release download root must not already exist")
    download_root.mkdir(parents=True)
    release_url = (
        f"{API_ROOT}/repos/{normalized_repository}/releases/tags/{quote(normalized_tag, safe='')}"
    )

    try:
        last_error = "public release was not observable"
        for attempt in range(1, attempts + 1):
            attempt_dir: Path | None = None
            try:
                release, _ = fetch_json(release_url, token)
                if not isinstance(release, dict):
                    raise ValueError("GitHub Release response must be an object")
                release_id = _require_positive_int(release.get("id"), "GitHub Release ID")
                assets = collect_release_assets(
                    normalized_repository,
                    release_id,
                    token,
                    fetch_json=fetch_json,
                )
                asset_ids = verify_release_metadata(
                    release,
                    assets,
                    tag=normalized_tag,
                    expected_body=expected_body,
                    expected_prerelease=expected_prerelease,
                    expected_assets=expected_assets,
                )
                attempt_dir = Path(
                    tempfile.mkdtemp(prefix=f"attempt-{attempt}-", dir=download_root)
                )
                for name in sorted(expected_assets):
                    download_asset(
                        normalized_repository,
                        asset_ids[name],
                        attempt_dir / name,
                        token,
                        expected_assets[name],
                    )
                verify_downloaded_assets(attempt_dir, expected_assets)
                return attempt_dir
            except (OSError, ValueError) as error:
                last_error = str(error)
                if attempt_dir is not None:
                    try:
                        shutil.rmtree(attempt_dir)
                    except OSError:
                        raise ValueError(
                            "failed to clean GitHub Release verification attempt"
                        ) from None
                if attempt < attempts:
                    sleep(delay)
        try:
            download_root.rmdir()
        except OSError:
            raise ValueError("failed to clean GitHub Release verification root") from None
        raise ValueError(
            f"public GitHub Release did not match after {attempts} attempts: {last_error}"
        )
    except BaseException:
        if download_root.exists() or download_root.is_symlink():
            try:
                if download_root.is_dir() and not download_root.is_symlink():
                    shutil.rmtree(download_root)
                else:
                    download_root.unlink()
            except OSError as cleanup_error:
                raise ValueError(
                    "failed to clean GitHub Release verification root"
                ) from cleanup_error
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--notes", type=Path, required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--download-root", type=Path, required=True)
    parser.add_argument("--expected-prerelease", choices=("true", "false"), required=True)
    parser.add_argument("--attempts", type=int, default=7)
    parser.add_argument("--delay", type=float, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verified_dir = verify_public_release(
            args.repository,
            args.tag,
            args.notes,
            args.dist,
            args.evidence,
            args.download_root,
            expected_prerelease=args.expected_prerelease == "true",
            # This is deliberately unauthenticated: success proves the
            # repository, release metadata, and numeric-ID assets are public.
            token="",
            attempts=args.attempts,
            delay=args.delay,
        )
    except (OSError, ValueError) as error:
        print(f"GitHub Release verification failed: {error}", file=sys.stderr)
        return 1
    print(f"public GitHub Release verified in {verified_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
