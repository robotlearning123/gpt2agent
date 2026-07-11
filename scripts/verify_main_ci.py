#!/usr/bin/env python3
"""Require successful main-branch CI for one exact release commit."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any


_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PENDING_STATUSES = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})
_PERMANENT_HTTP_STATUSES = {401: "unauthorized", 403: "forbidden", 404: "not found"}
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_ARTIFACT_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
# GitHub may suffix the workflow path with its resolved ref in a run response.
_WORKFLOW_PATH_RE = re.compile(r"\.github/workflows/ci\.yml(?:@refs/heads/main)?\Z")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        fp.close()
        raise ValueError("GitHub workflow API redirects are forbidden")


def _github_opener(
    *handlers: urllib.request.BaseHandler,
) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(*handlers, _NoRedirectHandler())


def _bounded_positive_int(value: str, *, maximum: int) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > maximum:
        raise argparse.ArgumentTypeError(f"value must be from 1 through {maximum}")
    return parsed


def _bounded_delay(value: str) -> float:
    parsed = float(value)
    if parsed < 0 or parsed > 60:
        raise argparse.ArgumentTypeError("delay must be from 0 through 60 seconds")
    return parsed


def _run_order(run: dict[str, Any]) -> tuple[int, int]:
    attempt = run.get("run_attempt")
    run_id = run.get("id")
    return (
        run_id if isinstance(run_id, int) and not isinstance(run_id, bool) else -1,
        attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else -1,
    )


def _permanent_http_error(error: urllib.error.HTTPError) -> ValueError | None:
    label = _PERMANENT_HTTP_STATUSES.get(error.code)
    if label is None:
        return None
    return ValueError(f"GitHub workflow API returned HTTP {error.code} ({label})")


def _selected_exact_main_run(payload: Any, commit: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise ValueError("GitHub response must contain a workflow_runs list")
    matches = [
        run
        for run in payload["workflow_runs"]
        if isinstance(run, dict)
        and run.get("head_sha") == commit
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
    ]
    if not matches:
        return None

    return max(matches, key=_run_order)


def select_exact_main_run(payload: Any, commit: str) -> tuple[str, int | None]:
    """Classify the newest exact-SHA main/push run without branch guessing."""
    selected = _selected_exact_main_run(payload, commit)
    if selected is None:
        return "missing", None

    run_id = selected.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise ValueError("exact main CI run has an invalid id")
    status = selected.get("status")
    conclusion = selected.get("conclusion")
    if status == "completed":
        if conclusion == "success":
            return "success", run_id
        safe_conclusion = (
            conclusion
            if isinstance(conclusion, str) and re.fullmatch(r"[a-z_]{1,32}", conclusion)
            else "unknown"
        )
        raise ValueError(f"exact main CI concluded {safe_conclusion}")
    if status in _PENDING_STATUSES:
        return "pending", run_id
    raise ValueError("exact main CI returned an unknown nonterminal status")


def _github_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
        value,
    ) is None:
        raise ValueError("main CI artifact expiry is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("main CI artifact expiry is invalid") from None
    if parsed.tzinfo != timezone.utc:
        raise ValueError("main CI artifact expiry is invalid")
    return parsed


def select_exact_candidate_artifact(
    payload: Any,
    *,
    commit: str,
    run_id: int,
    run_attempt: int,
    now: datetime | None = None,
    minimum_lifetime_hours: int = 72,
) -> dict[str, Any]:
    """Select one immutable, unexpired package artifact for an exact CI attempt."""
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
        raise ValueError("GitHub response must contain an artifacts list")
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id < 1
        or isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or run_attempt < 1
        or isinstance(minimum_lifetime_hours, bool)
        or not isinstance(minimum_lifetime_hours, int)
        or minimum_lifetime_hours < 0
    ):
        raise ValueError("main CI artifact identity is invalid")
    expected_name = f"release-candidate-{commit}-{run_id}-{run_attempt}"
    matches = [
        artifact
        for artifact in payload["artifacts"]
        if isinstance(artifact, dict) and artifact.get("name") == expected_name
    ]
    if len(matches) != 1:
        raise ValueError("exact main CI candidate artifact is missing or duplicated")
    artifact = matches[0]
    artifact_id = artifact.get("id")
    artifact_size = artifact.get("size_in_bytes")
    digest = artifact.get("digest")
    expires_at = artifact.get("expires_at")
    workflow_run = artifact.get("workflow_run")
    if (
        isinstance(artifact_id, bool)
        or not isinstance(artifact_id, int)
        or artifact_id < 1
        or isinstance(artifact_size, bool)
        or not isinstance(artifact_size, int)
        or artifact_size < 1
        or not isinstance(digest, str)
        or _ARTIFACT_DIGEST_RE.fullmatch(digest) is None
        or not isinstance(workflow_run, dict)
        or workflow_run.get("id") != run_id
        or workflow_run.get("head_branch") != "main"
        or workflow_run.get("head_sha") != commit
    ):
        raise ValueError("exact main CI candidate artifact metadata is invalid")
    expiry = _github_timestamp(expires_at)
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo != timezone.utc:
        raise ValueError("main CI artifact reference time is invalid")
    if artifact.get("expired") is not False or expiry <= reference_time + timedelta(
        hours=minimum_lifetime_hours
    ):
        raise ValueError("exact main CI candidate artifact is expired or expires too soon")
    return {
        "artifact_digest": digest,
        "artifact_expires_at": expires_at,
        "artifact_id": artifact_id,
        "artifact_name": expected_name,
        "artifact_size": artifact_size,
        "run_attempt": run_attempt,
        "run_id": run_id,
    }


def _candidate_producing_attempts(
    payload: Any,
    *,
    commit: str,
    run_id: int,
    latest_run_attempt: int,
) -> list[int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
        raise ValueError("GitHub response must contain an artifacts list")
    if (
        isinstance(latest_run_attempt, bool)
        or not isinstance(latest_run_attempt, int)
        or latest_run_attempt < 1
    ):
        raise ValueError("main CI run attempt is invalid")
    prefix = f"release-candidate-{commit}-{run_id}-"
    attempts: list[int] = []
    for artifact in payload["artifacts"]:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("name"), str):
            continue
        match = re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)", artifact["name"])
        if match is None:
            continue
        producing_attempt = int(match.group(1))
        if producing_attempt > latest_run_attempt:
            raise ValueError("main CI candidate comes from a future run attempt")
        attempts.append(producing_attempt)
    if not attempts:
        raise ValueError(
            "successful main CI run has no candidate artifact; rerun all jobs"
        )
    if len(attempts) != len(set(attempts)):
        raise ValueError("main CI candidate artifact is duplicated")
    return attempts


def select_latest_candidate_artifact(
    payload: Any,
    *,
    commit: str,
    run_id: int,
    latest_run_attempt: int,
    now: datetime | None = None,
    minimum_lifetime_hours: int = 72,
) -> dict[str, Any]:
    """Select the newest candidate-producing attempt in one successful run."""
    attempts = _candidate_producing_attempts(
        payload,
        commit=commit,
        run_id=run_id,
        latest_run_attempt=latest_run_attempt,
    )
    return select_exact_candidate_artifact(
        payload,
        commit=commit,
        run_id=run_id,
        run_attempt=max(attempts),
        now=now,
        minimum_lifetime_hours=minimum_lifetime_hours,
    )


def _complete_artifact_page(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
        raise ValueError("GitHub artifact response is invalid")
    total_count = payload.get("total_count")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
    ):
        raise ValueError("GitHub artifact response pagination is invalid")
    if total_count != len(payload["artifacts"]):
        raise ValueError("GitHub artifact response is incomplete")
    return payload


def _fetch_runs(repository: str, token: str) -> Any:
    query = urllib.parse.urlencode({"branch": "main", "event": "push", "per_page": "100"})
    url = f"https://api.github.com/repos/{repository}/actions/workflows/ci.yml/runs?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "gpt2agent-release-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with _github_opener().open(request, timeout=30) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        permanent = _permanent_http_error(error)
        if permanent is not None:
            raise permanent from None
        raise
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError("GitHub workflow response exceeds 4 MiB")
    return json.loads(body)


def _fetch_run(repository: str, token: str, run_id: int) -> Any:
    url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "gpt2agent-release-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with _github_opener().open(request, timeout=30) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        permanent = _permanent_http_error(error)
        if permanent is not None:
            raise permanent from None
        raise
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError("GitHub workflow run response exceeds 4 MiB")
    return json.loads(body)


def _fetch_artifacts(repository: str, token: str, run_id: int) -> Any:
    url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "gpt2agent-release-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with _github_opener().open(request, timeout=30) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        permanent = _permanent_http_error(error)
        if permanent is not None:
            raise permanent from None
        raise
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError("GitHub artifact response exceeds 4 MiB")
    return _complete_artifact_page(json.loads(body))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument(
        "--attempts", type=lambda value: _bounded_positive_int(value, maximum=360), default=180
    )
    parser.add_argument("--delay", type=_bounded_delay, default=10.0)
    parser.add_argument("--print-candidate-json", action="store_true")
    parser.add_argument("--expected-run-id", type=lambda value: _bounded_positive_int(value, maximum=10**20))
    parser.add_argument(
        "--expected-run-attempt",
        type=lambda value: _bounded_positive_int(value, maximum=10**9),
    )
    parser.add_argument(
        "--expected-artifact-id",
        type=lambda value: _bounded_positive_int(value, maximum=10**20),
    )
    parser.add_argument("--expected-artifact-digest")
    parser.add_argument(
        "--expected-artifact-size",
        type=lambda value: _bounded_positive_int(value, maximum=10**12),
    )
    parser.add_argument("--expected-artifact-expires-at")
    parser.add_argument(
        "--minimum-artifact-lifetime-hours",
        type=lambda value: _bounded_positive_int(value, maximum=24 * 90),
        default=72,
    )
    args = parser.parse_args(argv)

    if not _REPOSITORY_RE.fullmatch(args.repository):
        parser.error("repository must be an owner/name pair")
    if not _COMMIT_RE.fullmatch(args.commit):
        parser.error("commit must be a full lowercase Git SHA")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GH_TOKEN or GITHUB_TOKEN is required")
    expected_values = (
        args.expected_run_id,
        args.expected_run_attempt,
        args.expected_artifact_id,
        args.expected_artifact_digest,
        args.expected_artifact_size,
        args.expected_artifact_expires_at,
    )
    if any(value is not None for value in expected_values) and not all(
        value is not None for value in expected_values
    ):
        parser.error("all expected candidate artifact fields must be provided together")
    expected_candidate = all(value is not None for value in expected_values)
    if args.expected_artifact_digest is not None and (
        not isinstance(args.expected_artifact_digest, str)
        or _ARTIFACT_DIGEST_RE.fullmatch(args.expected_artifact_digest) is None
    ):
        parser.error("expected artifact digest must be sha256: followed by 64 lowercase hex")
    if args.expected_artifact_expires_at is not None:
        try:
            _github_timestamp(args.expected_artifact_expires_at)
        except ValueError as error:
            parser.error(str(error))

    last_state = "missing"
    for attempt in range(1, args.attempts + 1):
        try:
            if expected_candidate:
                pinned_run = _fetch_run(args.repository, token, args.expected_run_id)
                if not isinstance(pinned_run, dict) or not isinstance(
                    pinned_run.get("path"), str
                ) or _WORKFLOW_PATH_RE.fullmatch(pinned_run["path"]) is None:
                    raise ValueError("pinned main CI run has the wrong workflow path")
                payload = {"workflow_runs": [pinned_run]}
            else:
                payload = _fetch_runs(args.repository, token)
            state, run_id = select_exact_main_run(payload, args.commit)
            selected = _selected_exact_main_run(payload, args.commit)
            if expected_candidate and (selected is None or run_id != args.expected_run_id):
                raise ValueError("pinned main CI run identity does not match the release tag")
            if selected is None:
                run_attempt = None
            else:
                candidate_attempt = selected.get("run_attempt")
                if (
                    isinstance(candidate_attempt, bool)
                    or not isinstance(candidate_attempt, int)
                    or candidate_attempt < 1
                ):
                    raise ValueError("exact main CI run has an invalid attempt")
                run_attempt = candidate_attempt
            candidate = None
            if state == "success" and (args.print_candidate_json or expected_candidate):
                if run_id is None or run_attempt is None:
                    raise ValueError("exact main CI run identity is incomplete")
                producing_attempt = (
                    args.expected_run_attempt if expected_candidate else run_attempt
                )
                if run_attempt < producing_attempt:
                    raise ValueError(
                        "pinned candidate comes from a future main CI run attempt"
                    )
                artifact_payload = _fetch_artifacts(args.repository, token, run_id)
                if expected_candidate:
                    candidate = select_exact_candidate_artifact(
                        artifact_payload,
                        commit=args.commit,
                        run_id=run_id,
                        run_attempt=producing_attempt,
                        minimum_lifetime_hours=args.minimum_artifact_lifetime_hours,
                    )
                    candidate_attempts = _candidate_producing_attempts(
                        artifact_payload,
                        commit=args.commit,
                        run_id=run_id,
                        latest_run_attempt=run_attempt,
                    )
                    if max(candidate_attempts) > producing_attempt:
                        raise ValueError(
                            "a newer candidate-producing main CI attempt invalidates "
                            "the pinned account evidence; run a new account gate"
                        )
                else:
                    candidate = select_latest_candidate_artifact(
                        artifact_payload,
                        commit=args.commit,
                        run_id=run_id,
                        latest_run_attempt=run_attempt,
                        minimum_lifetime_hours=args.minimum_artifact_lifetime_hours,
                    )
                if expected_candidate:
                    expected = {
                        "artifact_digest": args.expected_artifact_digest,
                        "artifact_expires_at": args.expected_artifact_expires_at,
                        "artifact_id": args.expected_artifact_id,
                        "artifact_name": (
                            f"release-candidate-{args.commit}-{args.expected_run_id}-"
                            f"{args.expected_run_attempt}"
                        ),
                        "artifact_size": args.expected_artifact_size,
                        "run_attempt": args.expected_run_attempt,
                        "run_id": args.expected_run_id,
                    }
                    if candidate != expected:
                        raise ValueError("exact main CI candidate artifact identity does not match")
        except json.JSONDecodeError:
            state, run_id = "temporarily_unavailable", None
        except ValueError as exc:
            print(f"release gate failed: {exc}", file=sys.stderr)
            return 1
        except urllib.error.HTTPError as exc:
            permanent = _permanent_http_error(exc)
            if permanent is not None:
                print(f"release gate failed: {permanent}", file=sys.stderr)
                return 1
            state, run_id = "temporarily_unavailable", None
        except (OSError, urllib.error.URLError):
            state, run_id = "temporarily_unavailable", None

        last_state = state
        if state == "success":
            if args.print_candidate_json:
                if candidate is None:
                    raise AssertionError("candidate identity was not loaded")
                print(json.dumps(candidate, sort_keys=True, separators=(",", ":")))
            else:
                print(
                    f"exact main CI verified: commit={args.commit} "
                    f"run_id={run_id} run_attempt={run_attempt}"
                )
            return 0
        if attempt < args.attempts:
            time.sleep(args.delay)

    print(
        f"release gate failed: exact main CI did not succeed after {args.attempts} attempts "
        f"(last_state={last_state})",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
