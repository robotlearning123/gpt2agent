#!/usr/bin/env python3
"""Render and verify the closed metadata carried by an annotated release tag."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_METADATA_BYTES = 4096
MAX_TAG_MESSAGE_BYTES = 8192
MAX_TAG_OBJECT_BYTES = 64 * 1024
MAX_GITHUB_OUTPUT_BYTES = 1024 * 1024

_SCHEMA_VERSION = "1"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_VERSION_TEXT = (
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:alpha|beta|rc)[0-9]+)?"
)
_VERSION = re.compile(_VERSION_TEXT + r"\Z")
_TAG = re.compile(r"v" + _VERSION_TEXT + r"\Z")
_ARTIFACT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EXPIRES_AT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z"
)
_TAGGER = re.compile(r"tagger [\x20-\x7e]{1,512} [0-9]{1,20} [+-](?:0[0-9]|1[0-4])[0-5][0-9]\Z")
_OUTPUT_FIELDS = (
    "receipt_sha256",
    "account_artifact_set_sha256",
    "candidate_run_id",
    "candidate_run_attempt",
    "candidate_artifact_id",
    "candidate_artifact_digest",
    "candidate_artifact_size",
    "candidate_artifact_expires_at",
)


class TagMetadataError(ValueError):
    """The annotated release-tag metadata contract was violated."""


def _exact_object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise TagMetadataError("release tag metadata schema is invalid")
    return value


def _require_pattern(pattern: re.Pattern[str], value: Any, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise TagMetadataError(f"release tag {label} is invalid")
    return value


def _positive_int(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise TagMetadataError(f"release tag {label} is invalid")
    return value


def _timestamp(value: Any) -> str:
    normalized = _require_pattern(_EXPIRES_AT, value, "candidate artifact expiry")
    try:
        parsed = datetime.fromisoformat(normalized[:-1] + "+00:00")
    except ValueError:
        raise TagMetadataError("release tag candidate artifact expiry is invalid") from None
    if parsed.tzinfo != timezone.utc:
        raise TagMetadataError("release tag candidate artifact expiry is invalid")
    return normalized


def _canonical_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    try:
        payload = encoded.encode("ascii")
    except UnicodeEncodeError:
        raise TagMetadataError("release tag metadata is not ASCII") from None
    if len(payload) > MAX_METADATA_BYTES:
        raise TagMetadataError("release tag metadata is too large")
    return encoded


def build_metadata(
    *,
    repository: str,
    tag: str,
    version: str,
    commit: str,
    tree: str,
    receipt_sha256: str,
    artifact_set_sha256: str,
    candidate_run_id: int,
    candidate_run_attempt: int,
    candidate_artifact_id: int,
    candidate_artifact_digest: str,
    candidate_artifact_size: int,
    candidate_artifact_expires_at: str,
) -> dict[str, object]:
    """Build and validate one closed release-tag metadata object."""
    metadata: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "repository": repository,
        "tag": tag,
        "version": version,
        "source": {"commit": commit, "tree": tree},
        "account": {
            "receipt_sha256": receipt_sha256,
            "artifact_set_sha256": artifact_set_sha256,
        },
        "candidate": {
            "run_id": candidate_run_id,
            "run_attempt": candidate_run_attempt,
            "artifact_id": candidate_artifact_id,
            "artifact_digest": candidate_artifact_digest,
            "artifact_size": candidate_artifact_size,
            "artifact_expires_at": candidate_artifact_expires_at,
        },
    }
    return validate_metadata(metadata)


def validate_metadata(value: object) -> dict[str, object]:
    """Reject unknown fields and return a plain, normalized metadata object."""
    document = _exact_object(
        value,
        {"schema_version", "repository", "tag", "version", "source", "account", "candidate"},
    )
    if document["schema_version"] != _SCHEMA_VERSION:
        raise TagMetadataError("release tag metadata schema version is invalid")
    repository = _require_pattern(_REPOSITORY, document["repository"], "repository")
    tag = _require_pattern(_TAG, document["tag"], "tag")
    version = _require_pattern(_VERSION, document["version"], "version")
    if tag != f"v{version}":
        raise TagMetadataError("release tag and version do not match")

    source = _exact_object(document["source"], {"commit", "tree"})
    normalized_source = {
        "commit": _require_pattern(_HEX40, source["commit"], "source commit"),
        "tree": _require_pattern(_HEX40, source["tree"], "source tree"),
    }
    account = _exact_object(document["account"], {"receipt_sha256", "artifact_set_sha256"})
    normalized_account = {
        "receipt_sha256": _require_pattern(
            _HEX64,
            account["receipt_sha256"],
            "account receipt SHA-256",
        ),
        "artifact_set_sha256": _require_pattern(
            _HEX64,
            account["artifact_set_sha256"],
            "account artifact-set SHA-256",
        ),
    }
    candidate = _exact_object(
        document["candidate"],
        {
            "run_id",
            "run_attempt",
            "artifact_id",
            "artifact_digest",
            "artifact_size",
            "artifact_expires_at",
        },
    )
    normalized_candidate = {
        "run_id": _positive_int(candidate["run_id"], "candidate run ID", 10**20),
        "run_attempt": _positive_int(
            candidate["run_attempt"],
            "candidate run attempt",
            10**9,
        ),
        "artifact_id": _positive_int(
            candidate["artifact_id"],
            "candidate artifact ID",
            10**20,
        ),
        "artifact_digest": _require_pattern(
            _ARTIFACT_DIGEST,
            candidate["artifact_digest"],
            "candidate artifact digest",
        ),
        "artifact_size": _positive_int(
            candidate["artifact_size"],
            "candidate artifact size",
            10**12,
        ),
        "artifact_expires_at": _timestamp(candidate["artifact_expires_at"]),
    }
    normalized: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "repository": repository,
        "tag": tag,
        "version": version,
        "source": normalized_source,
        "account": normalized_account,
        "candidate": normalized_candidate,
    }
    _canonical_json(normalized)
    return normalized


def render_tag_message(metadata: object) -> str:
    """Render the byte-exact, one-line canonical JSON tag message."""
    normalized = validate_metadata(metadata)
    message = f"gpt2agent {normalized['version']}\n\n{_canonical_json(normalized)}\n"
    if len(message.encode("ascii")) > MAX_TAG_MESSAGE_BYTES:
        raise TagMetadataError("release tag message is too large")
    return message


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise TagMetadataError("release tag metadata contains a duplicate JSON key")
        result[key] = item
    return result


def _reject_json_constant(_value: str) -> None:
    raise TagMetadataError("release tag metadata JSON is invalid")


def parse_tag_message(message: str | bytes) -> dict[str, object]:
    """Parse only the exact message emitted by :func:`render_tag_message`."""
    if isinstance(message, bytes):
        if len(message) > MAX_TAG_MESSAGE_BYTES:
            raise TagMetadataError("release tag message is too large")
        try:
            text = message.decode("ascii")
        except UnicodeDecodeError:
            raise TagMetadataError("release tag message is not ASCII") from None
    elif isinstance(message, str):
        try:
            encoded = message.encode("ascii")
        except UnicodeEncodeError:
            raise TagMetadataError("release tag message is not ASCII") from None
        if len(encoded) > MAX_TAG_MESSAGE_BYTES:
            raise TagMetadataError("release tag message is too large")
        text = message
    else:
        raise TagMetadataError("release tag message is invalid")
    if "\x00" in text or "\r" in text:
        raise TagMetadataError("release tag message is invalid")
    match = re.fullmatch(rf"gpt2agent ({_VERSION_TEXT})\n\n([^\n]+)\n", text)
    if match is None:
        raise TagMetadataError("release tag message envelope is invalid")
    title_version, payload = match.groups()
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except TagMetadataError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise TagMetadataError("release tag metadata JSON is invalid") from None
    normalized = validate_metadata(decoded)
    if title_version != normalized["version"]:
        raise TagMetadataError("release tag message title does not match metadata")
    if payload != _canonical_json(normalized):
        raise TagMetadataError("release tag metadata JSON is not canonical")
    if text != render_tag_message(normalized):
        raise TagMetadataError("release tag message is not canonical")
    return normalized


def build_github_tag_request(metadata: object) -> dict[str, object]:
    """Return exactly the four fields accepted by GitHub's annotated-tag API."""
    normalized = validate_metadata(metadata)
    source = normalized["source"]
    if not isinstance(source, dict):  # Defensive; validation already enforces this.
        raise TagMetadataError("release tag metadata schema is invalid")
    return {
        "message": render_tag_message(normalized),
        "object": source["commit"],
        "tag": normalized["tag"],
        "type": "commit",
    }


def render_github_tag_request(metadata: object) -> bytes:
    """Render an exact canonical request body suitable for ``gh api --input``."""
    request = build_github_tag_request(metadata)
    return (_canonical_json(request) + "\n").encode("ascii")


def _read_bounded_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= MAX_TAG_OBJECT_BYTES:
            raise TagMetadataError("annotated tag object file is invalid")
        payload = bytearray()
        while len(payload) <= MAX_TAG_OBJECT_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_TAG_OBJECT_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
    except TagMetadataError:
        raise
    except OSError:
        raise TagMetadataError("annotated tag object file could not be read") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not payload or len(payload) > MAX_TAG_OBJECT_BYTES:
        raise TagMetadataError("annotated tag object file is invalid")
    return bytes(payload)


def _parse_raw_tag_object(payload: bytes) -> tuple[str, str, dict[str, object]]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        raise TagMetadataError("annotated tag object is not ASCII") from None
    if "\x00" in text or "\r" in text:
        raise TagMetadataError("annotated tag object is invalid")
    headers, separator, message = text.partition("\n\n")
    lines = headers.split("\n")
    if separator != "\n\n" or len(lines) != 4:
        raise TagMetadataError("annotated tag object headers are invalid")
    object_match = re.fullmatch(r"object ([0-9a-f]{40})", lines[0])
    tag_match = re.fullmatch(r"tag (v[^\n]+)", lines[2])
    if (
        object_match is None
        or lines[1] != "type commit"
        or tag_match is None
        or _TAGGER.fullmatch(lines[3]) is None
    ):
        raise TagMetadataError("annotated tag object headers are invalid")
    metadata = parse_tag_message(message)
    return object_match.group(1), tag_match.group(1), metadata


def verify_tag_object_file(
    *,
    tag_object_file: Path,
    expected_repository: str,
    expected_tag: str,
    expected_commit: str,
    expected_tree: str,
) -> dict[str, str]:
    """Verify a raw annotated tag object and return eight closed CI scalars."""
    repository = _require_pattern(_REPOSITORY, expected_repository, "expected repository")
    tag = _require_pattern(_TAG, expected_tag, "expected tag")
    commit = _require_pattern(_HEX40, expected_commit, "expected commit")
    tree = _require_pattern(_HEX40, expected_tree, "expected tree")
    object_commit, object_tag, metadata = _parse_raw_tag_object(
        _read_bounded_regular_file(Path(tag_object_file))
    )
    source = metadata["source"]
    account = metadata["account"]
    candidate = metadata["candidate"]
    if not all(isinstance(item, dict) for item in (source, account, candidate)):
        raise TagMetadataError("release tag metadata schema is invalid")
    if (
        object_commit != commit
        or object_tag != tag
        or metadata["repository"] != repository
        or metadata["tag"] != tag
        or source != {"commit": commit, "tree": tree}
    ):
        raise TagMetadataError("annotated tag object does not match expected release identity")
    return {
        "receipt_sha256": str(account["receipt_sha256"]),
        "account_artifact_set_sha256": str(account["artifact_set_sha256"]),
        "candidate_run_id": str(candidate["run_id"]),
        "candidate_run_attempt": str(candidate["run_attempt"]),
        "candidate_artifact_id": str(candidate["artifact_id"]),
        "candidate_artifact_digest": str(candidate["artifact_digest"]),
        "candidate_artifact_size": str(candidate["artifact_size"]),
        "candidate_artifact_expires_at": str(candidate["artifact_expires_at"]),
    }


def _append_github_outputs(path: Path, outputs: dict[str, str]) -> None:
    if tuple(outputs) != _OUTPUT_FIELDS or any(
        not isinstance(value, str) or not value or "\n" in value or "\r" in value
        for value in outputs.values()
    ):
        raise TagMetadataError("validated GitHub output fields are invalid")
    payload = "".join(f"{key}={outputs[key]}\n" for key in _OUTPUT_FIELDS).encode("ascii")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > MAX_GITHUB_OUTPUT_BYTES
            or metadata.st_size + len(payload) > MAX_GITHUB_OUTPUT_BYTES
        ):
            raise TagMetadataError("GITHUB_OUTPUT file is invalid")
        offset = 0
        while offset < len(payload):
            try:
                written = os.write(descriptor, payload[offset:])
            except InterruptedError:
                continue
            if written < 1:
                raise TagMetadataError("GITHUB_OUTPUT could not be written completely")
            offset += written
    except TagMetadataError:
        raise
    except OSError:
        raise TagMetadataError("GITHUB_OUTPUT could not be written") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    """Run the fail-closed release tag verifier."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify-tag-object")
    verify.add_argument("--tag-object-file", required=True, type=Path)
    verify.add_argument("--repository", required=True)
    verify.add_argument("--tag", required=True)
    verify.add_argument("--commit", required=True)
    verify.add_argument("--tree", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command != "verify-tag-object":
            raise TagMetadataError("release tag metadata command is invalid")
        output_path = os.environ.get("GITHUB_OUTPUT")
        if not output_path or "\x00" in output_path:
            raise TagMetadataError("GITHUB_OUTPUT is required")
        outputs = verify_tag_object_file(
            tag_object_file=args.tag_object_file,
            expected_repository=args.repository,
            expected_tag=args.tag,
            expected_commit=args.commit,
            expected_tree=args.tree,
        )
        _append_github_outputs(Path(output_path), outputs)
    except TagMetadataError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
