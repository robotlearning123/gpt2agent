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
from typing import Any


_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PENDING_STATUSES = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


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
        attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else -1,
        run_id if isinstance(run_id, int) and not isinstance(run_id, bool) else -1,
    )


def select_exact_main_run(payload: Any, commit: str) -> tuple[str, int | None]:
    """Classify the newest exact-SHA main/push run without branch guessing."""
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
        return "missing", None

    selected = max(matches, key=_run_order)
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
    with _github_opener().open(request, timeout=30) as response:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError("GitHub workflow response exceeds 4 MiB")
    return json.loads(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument(
        "--attempts", type=lambda value: _bounded_positive_int(value, maximum=360), default=180
    )
    parser.add_argument("--delay", type=_bounded_delay, default=10.0)
    args = parser.parse_args(argv)

    if not _REPOSITORY_RE.fullmatch(args.repository):
        parser.error("repository must be an owner/name pair")
    if not _COMMIT_RE.fullmatch(args.commit):
        parser.error("commit must be a full lowercase Git SHA")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GH_TOKEN or GITHUB_TOKEN is required")

    last_state = "missing"
    for attempt in range(1, args.attempts + 1):
        try:
            payload = _fetch_runs(args.repository, token)
            state, run_id = select_exact_main_run(payload, args.commit)
        except json.JSONDecodeError:
            state, run_id = "temporarily_unavailable", None
        except ValueError as exc:
            print(f"release gate failed: {exc}", file=sys.stderr)
            return 1
        except (OSError, urllib.error.URLError):
            state, run_id = "temporarily_unavailable", None

        last_state = state
        if state == "success":
            print(f"exact main CI verified: commit={args.commit} run_id={run_id}")
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
