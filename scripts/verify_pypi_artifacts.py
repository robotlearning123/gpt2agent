#!/usr/bin/env python3
"""Verify that PyPI artifacts match the locally built release files."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def artifact_hashes(dist: Path) -> dict[str, str]:
    """Return SHA-256 hashes for wheel and sdist files in *dist*."""
    entries = set(dist.iterdir())
    wheels = sorted(path for path in entries if path.name.endswith(".whl"))
    sdists = sorted(path for path in entries if path.name.endswith(".tar.gz"))
    artifacts = [*sdists, *wheels]
    if len(wheels) != 1 or len(sdists) != 1 or entries != set(artifacts):
        raise ValueError(
            f"expected exactly one wheel and one sdist and no other entries in {dist}"
        )
    if any(path.is_symlink() or not path.is_file() for path in artifacts):
        raise ValueError("release artifacts must be regular files, not symlinks")

    hashes: dict[str, str] = {}
    for path in artifacts:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[path.name] = digest.hexdigest()
    return hashes


def compare_artifacts(
    local: dict[str, str],
    remote: dict[str, str] | None,
    *,
    require_complete: bool,
    require_absent: bool = False,
) -> list[str]:
    """Return mismatches between local files and a PyPI release snapshot."""
    if require_complete and require_absent:
        raise ValueError("require_complete and require_absent are mutually exclusive")
    if require_absent:
        if remote is not None:
            return ["PyPI release already exists; refusing rebuilt artifacts"]
        return []
    if remote is None:
        return ["PyPI release does not exist"] if require_complete else []

    errors: list[str] = []
    for filename, remote_digest in sorted(remote.items()):
        local_digest = local.get(filename)
        if local_digest is None:
            errors.append(f"PyPI has unexpected artifact {filename}")
        elif local_digest != remote_digest:
            errors.append(f"SHA-256 mismatch for {filename}")

    if require_complete:
        for filename in sorted(local.keys() - remote.keys()):
            errors.append(f"PyPI is missing artifact {filename}")
    return errors


def pypi_hashes(project: str, version: str, *, timeout: float = 20.0) -> dict[str, str] | None:
    """Read artifact SHA-256 hashes from the PyPI JSON API."""
    quoted_project = urllib.parse.quote(project, safe="")
    quoted_version = urllib.parse.quote(version, safe="")
    url = f"https://pypi.org/pypi/{quoted_project}/{quoted_version}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "gpt2agent-release-verifier"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    urls = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(urls, list):
        raise ValueError("PyPI JSON response has no urls list")

    result: dict[str, str] = {}
    for entry in urls:
        if not isinstance(entry, dict):
            raise ValueError("PyPI JSON response has a malformed artifact entry")
        filename = entry.get("filename")
        digests = entry.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if not isinstance(filename, str) or not isinstance(digest, str):
            raise ValueError("PyPI artifact entry has no filename or SHA-256 digest")
        if filename in result:
            raise ValueError(f"PyPI JSON response repeats artifact {filename}")
        result[filename] = digest
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="gpt2agent")
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    release_state = parser.add_mutually_exclusive_group()
    release_state.add_argument("--require-complete", action="store_true")
    release_state.add_argument("--require-absent", action="store_true")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args(argv)

    if args.attempts < 1 or args.delay < 0:
        parser.error("--attempts must be positive and --delay must be nonnegative")

    try:
        local = artifact_hashes(args.dist.resolve())
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    last_errors: list[str] = []
    for attempt in range(1, args.attempts + 1):
        try:
            remote = pypi_hashes(args.project, args.version)
            last_errors = compare_artifacts(
                local,
                remote,
                require_complete=args.require_complete,
                require_absent=args.require_absent,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_errors = [f"PyPI verification failed: {exc}"]

        if not last_errors:
            state = "matching release" if remote is not None else "no existing release"
            print(f"PyPI artifact verification passed: {state} for {args.project} {args.version}")
            return 0
        if attempt < args.attempts:
            time.sleep(args.delay)

    for error in last_errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
