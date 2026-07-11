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
ARTIFACT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
EXPIRES_AT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z"
)


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


def _positive_int(value: str, label: str, *, maximum: int = 10**20) -> int:
    normalized = _require(RUN_NUMBER, value, label)
    parsed = int(normalized)
    if parsed > maximum:
        raise ValueError(f"invalid {label}")
    return parsed


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


def account_artifact_set(
    dist: Path,
    *,
    source_commit: str,
    source_tree: str,
    repository: str,
    run_id: str,
    run_attempt: str,
    artifact_id: str,
    artifact_digest: str,
    artifact_size: str,
    artifact_expires_at: str,
) -> dict[str, Any]:
    """Reconstruct the closed account-tested artifact identity from exact files."""
    commit = _require(HEX40, source_commit, "source commit")
    tree = _require(HEX40, source_tree, "source tree")
    normalized_run_id = _positive_int(run_id, "candidate run ID")
    normalized_run_attempt = _positive_int(run_attempt, "candidate run attempt", maximum=10**9)
    normalized_artifact_id = _positive_int(artifact_id, "candidate artifact ID")
    normalized_artifact_size = _positive_int(
        artifact_size,
        "candidate artifact size",
        maximum=10**12,
    )
    records = _artifact_records(dist)
    wheel = next(record for record in records if record["filename"].endswith(".whl"))
    sdist = next(record for record in records if record["filename"].endswith(".tar.gz"))
    return {
        "build_origin": "main_ci_package_artifact",
        "source": {"commit": commit, "tree": tree},
        "workflow": {
            "artifact_digest": _require(
                ARTIFACT_DIGEST,
                artifact_digest,
                "candidate artifact digest",
            ),
            "artifact_expires_at": _require(
                EXPIRES_AT,
                artifact_expires_at,
                "candidate artifact expiry",
            ),
            "artifact_id": normalized_artifact_id,
            "artifact_name": (
                f"release-candidate-{commit}-{normalized_run_id}-{normalized_run_attempt}"
            ),
            "artifact_size": normalized_artifact_size,
            "event": "push",
            "job": "package",
            "ref": "refs/heads/main",
            "repository": _require(REPOSITORY, repository, "repository"),
            "run_attempt": normalized_run_attempt,
            "run_id": normalized_run_id,
            "workflow_file": ".github/workflows/ci.yml",
        },
        "wheel": {
            "filename": wheel["filename"],
            "sha256": wheel["sha256"],
            "size_bytes": wheel["size"],
        },
        "sdist": {
            "filename": sdist["filename"],
            "sha256": sdist["sha256"],
            "size_bytes": sdist["size"],
        },
    }


def account_artifact_set_sha256(dist: Path, **identity: str) -> str:
    return hashlib.sha256(canonical_json(account_artifact_set(dist, **identity))).hexdigest()


def verify_account_artifact_handoff(
    dist: Path,
    *,
    artifact_set_sha256: str,
    **identity: str,
) -> None:
    expected = _require(HEX64, artifact_set_sha256, "account artifact-set SHA-256")
    if account_artifact_set_sha256(dist, **identity) != expected:
        raise ValueError("account-tested artifact set does not match the release handoff")


def _account_evidence_snapshot(
    dist: Path,
    *,
    commit: str,
    tree: str,
    repository: str,
    candidate_run_id: str,
    candidate_run_attempt: str,
    candidate_artifact_id: str,
    candidate_artifact_digest: str,
    candidate_artifact_size: str,
    candidate_artifact_expires_at: str,
    account_artifact_set_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bind account handoff and manifest records to one dist snapshot."""
    artifact_set = account_artifact_set(
        dist,
        source_commit=commit,
        source_tree=tree,
        repository=repository,
        run_id=candidate_run_id,
        run_attempt=candidate_run_attempt,
        artifact_id=candidate_artifact_id,
        artifact_digest=candidate_artifact_digest,
        artifact_size=candidate_artifact_size,
        artifact_expires_at=candidate_artifact_expires_at,
    )
    expected = _require(
        HEX64,
        account_artifact_set_sha256,
        "account artifact-set SHA-256",
    )
    if hashlib.sha256(canonical_json(artifact_set)).hexdigest() != expected:
        raise ValueError("account-tested artifact set does not match the release handoff")
    records = [
        {
            "filename": artifact_set[kind]["filename"],
            "sha256": artifact_set[kind]["sha256"],
            "size": artifact_set[kind]["size_bytes"],
        }
        for kind in ("sdist", "wheel")
    ]
    return (
        {
            "artifact_set_sha256": expected,
            "candidate": artifact_set["workflow"],
        },
        records,
    )


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
    candidate_run_id: str,
    candidate_run_attempt: str,
    candidate_artifact_id: str,
    candidate_artifact_digest: str,
    candidate_artifact_size: str,
    candidate_artifact_expires_at: str,
    account_artifact_set_sha256: str,
) -> dict[str, Any]:
    """Return canonical-data-ready provenance for one immutable artifact set."""
    manifest = {
        "schema_version": "2",
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
    }
    account_handoff, artifacts = _account_evidence_snapshot(
        dist,
        commit=commit,
        tree=tree,
        repository=repository,
        candidate_run_id=candidate_run_id,
        candidate_run_attempt=candidate_run_attempt,
        candidate_artifact_id=candidate_artifact_id,
        candidate_artifact_digest=candidate_artifact_digest,
        candidate_artifact_size=candidate_artifact_size,
        candidate_artifact_expires_at=candidate_artifact_expires_at,
        account_artifact_set_sha256=account_artifact_set_sha256,
    )
    manifest["account_handoff"] = account_handoff
    manifest["artifacts"] = artifacts
    return manifest


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
    candidate_run_id: str,
    candidate_run_attempt: str,
    candidate_artifact_id: str,
    candidate_artifact_digest: str,
    candidate_artifact_size: str,
    candidate_artifact_expires_at: str,
    account_artifact_set_sha256: str,
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
        "account_handoff",
        "artifacts",
    }
    if set(manifest) != expected_keys:
        raise ValueError("release evidence has an unexpected schema")
    expected_identity = {
        "schema_version": "2",
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

    expected_handoff, expected_artifacts = _account_evidence_snapshot(
        dist,
        commit=commit,
        tree=tree,
        repository=repository,
        candidate_run_id=candidate_run_id,
        candidate_run_attempt=candidate_run_attempt,
        candidate_artifact_id=candidate_artifact_id,
        candidate_artifact_digest=candidate_artifact_digest,
        candidate_artifact_size=candidate_artifact_size,
        candidate_artifact_expires_at=candidate_artifact_expires_at,
        account_artifact_set_sha256=account_artifact_set_sha256,
    )
    if manifest.get("account_handoff") != expected_handoff:
        raise ValueError("release evidence account handoff does not match")

    if manifest.get("artifacts") != expected_artifacts:
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
        sub.add_argument("--candidate-run-id", required=True)
        sub.add_argument("--candidate-run-attempt", required=True)
        sub.add_argument("--candidate-artifact-id", required=True)
        sub.add_argument("--candidate-artifact-digest", required=True)
        sub.add_argument("--candidate-artifact-size", required=True)
        sub.add_argument("--candidate-artifact-expires-at", required=True)
        sub.add_argument("--account-artifact-set-sha256", required=True)
    create = subparsers.choices["create"]
    create.add_argument("--job", required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = subparsers.choices["verify"]
    verify.add_argument("--manifest", type=Path, required=True)
    handoff = subparsers.add_parser("verify-account-handoff")
    handoff.add_argument("--dist", type=Path, required=True)
    handoff.add_argument("--commit", required=True)
    handoff.add_argument("--tree", required=True)
    handoff.add_argument("--repository", required=True)
    handoff.add_argument("--run-id", required=True)
    handoff.add_argument("--run-attempt", required=True)
    handoff.add_argument("--artifact-id", required=True)
    handoff.add_argument("--artifact-digest", required=True)
    handoff.add_argument("--artifact-size", required=True)
    handoff.add_argument("--artifact-expires-at", required=True)
    handoff.add_argument("--artifact-set-sha256", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "verify-account-handoff":
        try:
            verify_account_artifact_handoff(
                args.dist,
                source_commit=args.commit,
                source_tree=args.tree,
                repository=args.repository,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                artifact_id=args.artifact_id,
                artifact_digest=args.artifact_digest,
                artifact_size=args.artifact_size,
                artifact_expires_at=args.artifact_expires_at,
                artifact_set_sha256=args.artifact_set_sha256,
            )
            print("account-tested release artifact handoff verified")
        except (OSError, ValueError) as error:
            print(f"release evidence verification failed: {error}", file=__import__("sys").stderr)
            return 1
        return 0
    common = {
        "tag": args.tag,
        "tag_object": args.tag_object,
        "commit": args.commit,
        "tree": args.tree,
        "receipt_sha256": args.receipt_sha256,
        "repository": args.repository,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "candidate_run_id": args.candidate_run_id,
        "candidate_run_attempt": args.candidate_run_attempt,
        "candidate_artifact_id": args.candidate_artifact_id,
        "candidate_artifact_digest": args.candidate_artifact_digest,
        "candidate_artifact_size": args.candidate_artifact_size,
        "candidate_artifact_expires_at": args.candidate_artifact_expires_at,
        "account_artifact_set_sha256": args.account_artifact_set_sha256,
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
