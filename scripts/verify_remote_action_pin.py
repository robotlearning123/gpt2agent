#!/usr/bin/env python3
"""Verify that the exact publication action pin is remotely reproducible."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


_MAX_BYTES = 1024 * 1024
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_ACTION_PATH = ".github/actions/publish-exact-github-release"
_ACTION_FILES = ("action.yml", "publish.py")
_PUBLISH_JOB = "github-release"
_PUBLISH_STEP_NAME = "Validate and publish the exact draft"


class ActionVerificationError(ValueError):
    """The publication action pin cannot be reproduced exactly from GitHub."""


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _loads_json(payload: bytes) -> Any:
    try:
        return json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        RecursionError,
    ):
        raise ActionVerificationError("GitHub returned invalid action metadata") from None


def _response_body(payload: bytes) -> bytes:
    separator = b"\r\n\r\n" if b"\r\n\r\n" in payload else b"\n\n"
    if separator not in payload:
        raise ActionVerificationError("GitHub action response lacks exact HTTP metadata")
    header_block, body = payload.split(separator, 1)
    try:
        header_lines = header_block.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise ActionVerificationError("GitHub action response metadata is invalid") from None
    if (
        not header_lines
        or re.fullmatch(r"HTTP/(?:1\.[01]|2(?:\.0)?) 200(?: .*)?", header_lines[0]) is None
    ):
        raise ActionVerificationError("GitHub action request was redirected or unsuccessful")
    fields: dict[str, list[str]] = {}
    for line in header_lines[1:]:
        if ":" not in line:
            raise ActionVerificationError("GitHub action response metadata is invalid")
        name, value = line.split(":", 1)
        normalized = name.strip().lower()
        if not normalized or re.fullmatch(r"[a-z0-9-]+", normalized) is None:
            raise ActionVerificationError("GitHub action response metadata is invalid")
        fields.setdefault(normalized, []).append(value.strip())
    content_types = fields.get("content-type", [])
    if (
        "location" in fields
        or len(content_types) != 1
        or not content_types[0].lower().startswith("application/json")
    ):
        raise ActionVerificationError("GitHub action request was redirected or non-JSON")
    return body


def _canonical_regular_file(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ActionVerificationError(f"{label} path is not absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError:
        raise ActionVerificationError(f"{label} is unavailable") from None
    if (
        resolved != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > _MAX_BYTES
    ):
        raise ActionVerificationError(f"{label} is not a canonical regular file")
    return resolved


def _read_file(path: Path, label: str) -> bytes:
    canonical = _canonical_regular_file(path, label)
    try:
        payload = canonical.read_bytes()
    except OSError:
        raise ActionVerificationError(f"{label} is unavailable") from None
    if len(payload) > _MAX_BYTES:
        raise ActionVerificationError(f"{label} is too large")
    return payload


def _extract_pin(workflow: bytes, repository: str) -> str:
    try:
        source = workflow.decode("utf-8")
    except UnicodeDecodeError:
        raise ActionVerificationError("release workflow is not UTF-8") from None

    # This gate runs under stdlib-only ``python -I -S``. Rather than pretend a
    # regex is a YAML parser, accept only the repository's canonical block-style
    # path: jobs -> github-release -> steps -> the named, unconditional action
    # step. Exact indentation keeps block-scalar text and unrelated mappings
    # from masquerading as an executable ``uses`` key.
    if "\r" in source or "\t" in source or "\x00" in source:
        raise ActionVerificationError("release workflow is not canonical block YAML")
    lines = source.split("\n")
    action_use_lines = [
        line
        for line in lines
        if _ACTION_PATH in line
        and (
            re.fullmatch(r"      - uses:.*", line) is not None
            or re.fullmatch(r"        uses:.*", line) is not None
        )
    ]
    if len(action_use_lines) != 1:
        raise ActionVerificationError("release workflow must invoke the publication action once")

    def significant(index: int) -> bool:
        stripped = lines[index].lstrip(" ")
        return bool(stripped) and not stripped.startswith("#")

    jobs = [index for index, line in enumerate(lines) if line == "jobs:"]
    if len(jobs) != 1:
        raise ActionVerificationError("release workflow lacks one canonical jobs mapping")
    jobs_start = jobs[0]
    jobs_end = len(lines)
    for index in range(jobs_start + 1, len(lines)):
        if significant(index) and len(lines[index]) - len(lines[index].lstrip(" ")) == 0:
            jobs_end = index
            break

    job_header = f"  {_PUBLISH_JOB}:"
    job_indices = [
        index
        for index in range(jobs_start + 1, jobs_end)
        if lines[index] == job_header
    ]
    if len(job_indices) != 1:
        raise ActionVerificationError("release workflow lacks one canonical publication job")
    job_start = job_indices[0]
    job_end = jobs_end
    for index in range(job_start + 1, jobs_end):
        if not significant(index):
            continue
        indentation = len(lines[index]) - len(lines[index].lstrip(" "))
        if indentation <= 2:
            job_end = index
            break
    for index in range(job_start + 1, job_end):
        if not significant(index):
            continue
        line = lines[index]
        indentation = len(line) - len(line.lstrip(" "))
        if indentation != 4:
            continue
        property_match = re.fullmatch(r"    ([a-z][a-z0-9-]*):(.*)", line)
        if property_match is None:
            raise ActionVerificationError("publication job is not canonical block YAML")
        if property_match.group(1) == "if":
            raise ActionVerificationError("publication job must be unconditional")

    step_mappings = [
        index
        for index in range(job_start + 1, job_end)
        if lines[index] == "    steps:"
    ]
    if len(step_mappings) != 1:
        raise ActionVerificationError("publication job lacks one canonical steps sequence")
    steps_start = step_mappings[0]
    steps_end = job_end
    for index in range(steps_start + 1, job_end):
        if not significant(index):
            continue
        indentation = len(lines[index]) - len(lines[index].lstrip(" "))
        if indentation <= 4:
            steps_end = index
            break

    target_header = f"      - name: {_PUBLISH_STEP_NAME}"
    target_indices = [
        index
        for index in range(steps_start + 1, steps_end)
        if lines[index] == target_header
    ]
    if len(target_indices) != 1:
        raise ActionVerificationError("release workflow lacks one canonical publication step")
    target_start = target_indices[0]
    target_end = steps_end
    for index in range(target_start + 1, steps_end):
        if not significant(index):
            continue
        line = lines[index]
        indentation = len(line) - len(line.lstrip(" "))
        if indentation <= 6:
            target_end = index
            break

    properties: dict[str, list[str]] = {}
    for index in range(target_start + 1, target_end):
        line = lines[index]
        if not significant(index):
            continue
        indentation = len(line) - len(line.lstrip(" "))
        if indentation != 8:
            continue
        match = re.fullmatch(r"        ([a-z][a-z0-9-]*):(.*)", line)
        if match is None:
            raise ActionVerificationError("publication step is not canonical block YAML")
        properties.setdefault(match.group(1), []).append(match.group(2).strip())
    if set(properties) != {"uses", "with"} or any(
        len(values) != 1 for values in properties.values()
    ):
        raise ActionVerificationError("publication step must be one unconditional action call")
    if properties["with"] != [""]:
        raise ActionVerificationError("publication action inputs are not a canonical mapping")

    expected_prefix = f"{repository}/{_ACTION_PATH}@"
    uses_value = properties["uses"][0]
    uses_match = re.fullmatch(
        rf"{re.escape(expected_prefix)}([^\s#]+)\s*(?:#.*)?",
        uses_value,
    )
    if uses_match is None or not _SHA_RE.fullmatch(uses_match.group(1)):
        raise ActionVerificationError("release workflow lacks one exact publication action pin")
    return uses_match.group(1)


def _token() -> str:
    token = os.environ.get("GH_TOKEN")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 4096
        or any(character.isspace() for character in token)
    ):
        raise ActionVerificationError("operator GitHub token is unavailable")
    return token


def _gh_json(gh: Path, endpoint: str, token: str) -> Any:
    environment = {
        "GH_TOKEN": token,
        "GH_CONFIG_DIR": "/nonexistent",
        "GH_PROMPT_DISABLED": "1",
        "HOME": "/nonexistent",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    try:
        result = subprocess.run(
            [
                str(gh),
                "api",
                "--hostname",
                "github.com",
                "--method",
                "GET",
                "--include",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "X-GitHub-Api-Version: 2026-03-10",
                endpoint,
            ],
            env=environment,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ActionVerificationError("GitHub action metadata is unavailable") from None
    if result.returncode != 0 or not result.stdout or len(result.stdout) > _MAX_BYTES:
        raise ActionVerificationError("GitHub action metadata is unavailable")
    return _loads_json(_response_body(result.stdout))


def _remote_file(payload: Any, expected_size: int) -> bytes:
    if not isinstance(payload, dict):
        raise ActionVerificationError("GitHub returned invalid action file metadata")
    content = payload.get("content")
    size = payload.get("size")
    if (
        payload.get("encoding") != "base64"
        or not isinstance(content, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size != expected_size
    ):
        raise ActionVerificationError("GitHub returned invalid action file metadata")
    compact = "".join(content.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        raise ActionVerificationError("GitHub returned invalid action file content") from None
    if len(decoded) != size:
        raise ActionVerificationError("GitHub returned invalid action file size")
    return decoded


def verify_action(
    *,
    gh: Path,
    repository: str,
    workflow: Path,
    action_directory: Path,
) -> None:
    if not _REPOSITORY_RE.fullmatch(repository):
        raise ActionVerificationError("repository must be an owner/name pair")
    if not gh.is_absolute():
        raise ActionVerificationError("gh path is not absolute")
    workflow_payload = _read_file(workflow, "release workflow")
    pin = _extract_pin(workflow_payload, repository)
    try:
        resolved_action_directory = action_directory.resolve(strict=True)
        directory_metadata = action_directory.lstat()
    except OSError:
        raise ActionVerificationError("publication action directory is unavailable") from None
    if (
        not action_directory.is_absolute()
        or resolved_action_directory != action_directory
        or stat.S_ISLNK(directory_metadata.st_mode)
        or not stat.S_ISDIR(directory_metadata.st_mode)
    ):
        raise ActionVerificationError("publication action directory is not canonical")

    token = _token()
    commit = _gh_json(gh, f"repos/{repository}/commits/{pin}", token)
    if not isinstance(commit, dict) or commit.get("sha") != pin:
        raise ActionVerificationError("publication action commit did not resolve exactly")
    for name in _ACTION_FILES:
        local = _read_file(action_directory / name, f"local {name}")
        remote_payload = _gh_json(
            gh,
            f"repos/{repository}/contents/{_ACTION_PATH}/{name}?ref={pin}",
            token,
        )
        if _remote_file(remote_payload, len(local)) != local:
            raise ActionVerificationError(f"remote {name} does not match the reviewed local file")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gh", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--action-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        verify_action(
            gh=args.gh,
            repository=args.repository,
            workflow=args.workflow,
            action_directory=args.action_directory,
        )
    except ActionVerificationError as exc:
        print(f"publication action verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
