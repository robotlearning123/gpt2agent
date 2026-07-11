#!/usr/bin/env python3
"""Create and verify canonical release-workflow provenance evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)[0-9]+)?\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
RUN_NUMBER = re.compile(r"[1-9][0-9]*\Z")


def _require(pattern: re.Pattern[str], value: str, label: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_build_job(value: str) -> str:
    if value != "build":
        raise ValueError("release evidence workflow job must be build")
    return value


def _artifact_records(dist: Path) -> list[dict[str, Any]]:
    if not dist.is_dir():
        raise ValueError(f"distribution directory does not exist: {dist}")
    wheels = sorted(dist.glob("*.whl"), key=lambda path: path.name)
    sdists = sorted(dist.glob("*.tar.gz"), key=lambda path: path.name)
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one sdist")
    paths = [*sdists, *wheels]
    if set(dist.iterdir()) != set(paths):
        raise ValueError("expected exactly one wheel and one sdist and no other entries")
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("release artifacts must be regular files, not symlinks")
    return [
        {
            "filename": path.name,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
        for path in paths
    ]


def build_manifest(
    dist: Path,
    *,
    tag: str,
    tag_object: str,
    commit: str,
    tree: str,
    receipt_sha256: str,
    repository: str,
    run_id: str,
    run_attempt: str,
    job: str,
) -> dict[str, Any]:
    """Return canonical-data-ready provenance for one immutable artifact set."""
    return {
        "schema_version": "1",
        "artifact_set": "release_workflow_artifacts",
        "tag": _require(TAG, tag, "tag"),
        "tag_object": _require(HEX40, tag_object, "tag object"),
        "source": {
            "commit": _require(HEX40, commit, "source commit"),
            "tree": _require(HEX40, tree, "source tree"),
        },
        "receipt_sha256": _require(HEX64, receipt_sha256, "account receipt SHA-256"),
        "workflow": {
            "repository": _require(REPOSITORY, repository, "repository"),
            "run_id": _require(RUN_NUMBER, run_id, "workflow run ID"),
            "run_attempt": _require(RUN_NUMBER, run_attempt, "workflow run attempt"),
            "job": _require_build_job(job),
        },
        "artifacts": _artifact_records(dist),
    }


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def write_manifest(path: Path, manifest: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(manifest)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def verify_manifest(
    manifest: dict[str, Any],
    dist: Path,
    *,
    tag: str,
    tag_object: str,
    commit: str,
    tree: str,
    receipt_sha256: str,
    repository: str,
    run_id: str,
    run_attempt: str,
) -> None:
    """Fail closed if evidence, expected workflow identity, or files drift."""
    if not isinstance(manifest, dict):
        raise ValueError("release evidence must be a JSON object")
    expected_keys = {
        "schema_version",
        "artifact_set",
        "tag",
        "tag_object",
        "source",
        "receipt_sha256",
        "workflow",
        "artifacts",
    }
    if set(manifest) != expected_keys:
        raise ValueError("release evidence has an unexpected schema")
    expected_identity = {
        "schema_version": "1",
        "artifact_set": "release_workflow_artifacts",
        "tag": _require(TAG, tag, "tag"),
        "tag_object": _require(HEX40, tag_object, "tag object"),
        "source": {
            "commit": _require(HEX40, commit, "source commit"),
            "tree": _require(HEX40, tree, "source tree"),
        },
        "receipt_sha256": _require(HEX64, receipt_sha256, "account receipt SHA-256"),
    }
    for key, expected in expected_identity.items():
        if manifest.get(key) != expected:
            raise ValueError(f"release evidence {key} does not match expected value")

    workflow = manifest.get("workflow")
    if not isinstance(workflow, dict) or set(workflow) != {
        "repository",
        "run_id",
        "run_attempt",
        "job",
    }:
        raise ValueError("release evidence workflow identity is malformed")
    expected_workflow = {
        "repository": _require(REPOSITORY, repository, "repository"),
        "run_id": _require(RUN_NUMBER, run_id, "workflow run ID"),
    }
    for key, expected in expected_workflow.items():
        if workflow.get(key) != expected:
            raise ValueError(f"release evidence workflow {key} does not match")
    evidence_attempt = _require(
        RUN_NUMBER, workflow.get("run_attempt", ""), "evidence workflow run attempt"
    )
    current_attempt = _require(RUN_NUMBER, run_attempt, "workflow run attempt")
    if int(evidence_attempt) > int(current_attempt):
        raise ValueError("release evidence comes from a future workflow run attempt")
    _require_build_job(workflow.get("job", ""))

    if manifest.get("artifacts") != _artifact_records(dist):
        raise ValueError("artifact metadata does not match release evidence")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--dist", type=Path, required=True)
        sub.add_argument("--tag", required=True)
        sub.add_argument("--tag-object", required=True)
        sub.add_argument("--commit", required=True)
        sub.add_argument("--tree", required=True)
        sub.add_argument("--receipt-sha256", required=True)
        sub.add_argument("--repository", required=True)
        sub.add_argument("--run-id", required=True)
        sub.add_argument("--run-attempt", required=True)
    create = subparsers.choices["create"]
    create.add_argument("--job", required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = subparsers.choices["verify"]
    verify.add_argument("--manifest", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    common = {
        "tag": args.tag,
        "tag_object": args.tag_object,
        "commit": args.commit,
        "tree": args.tree,
        "receipt_sha256": args.receipt_sha256,
        "repository": args.repository,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
    }
    try:
        if args.command == "create":
            if args.output.resolve().is_relative_to(args.dist.resolve()):
                raise ValueError("release evidence must be stored outside dist")
            manifest = build_manifest(args.dist, job=args.job, **common)
            digest = write_manifest(args.output, manifest)
            print(f"release evidence created: {args.output} sha256={digest}")
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            if canonical_json(manifest) != args.manifest.read_bytes():
                raise ValueError("release evidence is not canonical JSON")
            verify_manifest(manifest, args.dist, **common)
            print(f"release evidence verified: {args.manifest}")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"release evidence verification failed: {error}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
