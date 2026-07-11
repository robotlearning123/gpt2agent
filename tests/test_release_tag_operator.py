"""Behavioral tests for the closed pre-tag coordinator."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = PROJECT_ROOT / "scripts" / "create_release_tag.sh"
COMMIT = "1" * 40
TREE = "2" * 40
RECEIPT_SHA256 = "3" * 64
ARTIFACT_DIGEST = "sha256:" + "4" * 64
TAG_OBJECT_SHA = "5" * 40
APP_TOKEN = "app-installation-token-canary"
OPERATOR_TOKEN = "operator-read-token-canary"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _harness(
    tmp_path: Path,
    *,
    mode: str = "ok",
    token_timeout: str | None = None,
    terminate_at_prompt: bool = False,
) -> tuple[list[str], dict[str, str]]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    event_log = tmp_path / "events"
    fake_python = fakebin / "trusted-python"
    _write_executable(
        fake_python,
        """#!/bin/bash
set -euo pipefail
case "$*" in
  *verify_main_ci.py*)
    printf 'python-ci token=%s\n' "${GH_TOKEN-unset}" >> "$EVENT_LOG"
    if [ "${FAKE_MODE-}" = ci-fail ]; then exit 19; fi
    ;;
  *'verify_account_receipt.py prepare-tag'*)
    printf 'python-prepare token=%s\n' "${GH_TOKEN-unset}" >> "$EVENT_LOG"
    output=''
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --output ]; then output=$2; shift 2; else shift; fi
    done
    printf '%s\n' '{"message":"closed","object":"1111111111111111111111111111111111111111","tag":"v0.0.12","type":"commit"}' > "$output"
    ;;
  *) exit 97 ;;
esac
""",
    )
    _write_executable(
        fakebin / "gh",
        f"""#!/bin/bash
set -euo pipefail
if [ "$1 $2" = 'auth token' ]; then
  printf 'gh-auth token=%s\n' "${{GH_TOKEN-unset}}" >> "$EVENT_LOG"
  printf '%s\n' '{OPERATOR_TOKEN}'
  exit 0
fi
case "$*" in
  *matching-refs*)
    printf 'gh-absence token=%s\n' "${{GH_TOKEN-unset}}" >> "$EVENT_LOG"
    if [ "${{FAKE_MODE-}}" = existing ]; then printf '%s\n' 'refs/tags/v0.0.12'; fi
    ;;
  *'--method POST'*git/tags*)
    printf 'gh-tag token=%s\n' "${{GH_TOKEN-unset}}" >> "$EVENT_LOG"
    printf '%s\n' '{TAG_OBJECT_SHA}'
    ;;
  *'--method POST'*git/refs*)
    printf 'gh-ref token=%s\n' "${{GH_TOKEN-unset}}" >> "$EVENT_LOG"
    if [ "${{FAKE_MODE-}}" = ref-race ]; then exit 22; fi
    ;;
  *git/ref/tags*)
    printf 'gh-readback token=%s\n' "${{GH_TOKEN-unset}}" >> "$EVENT_LOG"
    printf '%s\n' '{TAG_OBJECT_SHA}'
    ;;
  *) exit 98 ;;
esac
""",
    )
    _write_executable(
        fakebin / "git",
        f"""#!/bin/bash
set -euo pipefail
case "$*" in
  *'rev-parse HEAD^{{tree}}'*) printf '%s\n' '{TREE}' ;;
  *'rev-parse HEAD'*) printf '%s\n' '{COMMIT}' ;;
  *'status --porcelain=v1'*) exit 0 ;;
  *'show-ref --verify --quiet refs/release-verification/created-tag'*) exit 1 ;;
  *'fetch --force --no-tags'*) printf 'git-fetch\n' >> "$EVENT_LOG" ;;
  *'cat-file -t refs/release-verification/created-tag'*) printf 'tag\n' ;;
  *'rev-parse refs/release-verification/created-tag^{{}}'*) printf '%s\n' '{COMMIT}' ;;
  *'rev-parse refs/release-verification/created-tag'*) printf '%s\n' '{TAG_OBJECT_SHA}' ;;
  *'update-ref -d refs/release-verification/created-tag'*) printf 'git-clean\n' >> "$EVENT_LOG" ;;
  *) exit 99 ;;
esac
""",
    )
    checkout = tmp_path / "checkout"
    dist = tmp_path / "dist"
    checkout.mkdir()
    dist.mkdir()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n", encoding="utf-8")
    environment = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "EVENT_LOG": str(event_log),
        "FAKE_MODE": mode,
        "TMPDIR": str(tmp_path),
    }
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GPT2AGENT_RELEASE_APP_TOKEN"):
        environment.pop(key, None)
    if token_timeout is not None:
        environment["GPT2AGENT_RELEASE_TOKEN_TIMEOUT_SECONDS"] = token_timeout
    command = [
        str(COORDINATOR),
        "--python",
        str(fake_python),
        "--checkout",
        str(checkout),
        "--dist",
        str(dist),
        "--receipt",
        str(receipt),
        "--receipt-sha256",
        RECEIPT_SHA256,
        "--repository",
        "robotlearning123/gpt2agent",
        "--tag",
        "v0.0.12",
        "--commit",
        COMMIT,
        "--tree",
        TREE,
        "--ci-run-id",
        "12345",
        "--ci-run-attempt",
        "2",
        "--ci-artifact-id",
        "67890",
        "--ci-artifact-digest",
        ARTIFACT_DIGEST,
        "--ci-artifact-size",
        "31415",
        "--ci-artifact-expires-at",
        "2099-07-10T13:17:42Z",
    ]
    if terminate_at_prompt:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if event_log.exists() and "gh-auth" in event_log.read_text(encoding="utf-8"):
                break
            time.sleep(0.01)
        else:
            process.kill()
            process.communicate(timeout=5)
            raise AssertionError("coordinator did not reach the token prompt")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    else:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            input=APP_TOKEN + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
    events = event_log.read_text(encoding="utf-8").splitlines() if event_log.exists() else []
    return events, {"returncode": str(result.returncode), "stdout": result.stdout, "stderr": result.stderr}


def test_coordinator_closes_the_final_check_to_ref_sequence(tmp_path: Path) -> None:
    events, result = _harness(tmp_path)

    assert result["returncode"] == "0", result["stderr"]
    assert events[:7] == [
        "gh-auth token=unset",
        f"python-ci token={OPERATOR_TOKEN}",
        "python-prepare token=unset",
        f"gh-absence token={APP_TOKEN}",
        f"gh-tag token={APP_TOKEN}",
        f"gh-ref token={APP_TOKEN}",
        f"gh-readback token={APP_TOKEN}",
    ]
    assert "git-fetch" in events
    assert APP_TOKEN not in "\n".join(
        event for event in events if event.startswith("python-")
    )


@pytest.mark.parametrize("mode", ("ci-fail", "existing", "ref-race"))
def test_coordinator_failures_never_fall_back_or_overwrite_refs(
    tmp_path: Path,
    mode: str,
) -> None:
    events, result = _harness(tmp_path, mode=mode)

    assert result["returncode"] != "0"
    if mode == "ci-fail":
        assert not any(event.startswith("gh-absence") for event in events)
    if mode == "existing":
        assert not any(event.startswith("gh-tag") for event in events)
    if mode == "ref-race":
        assert any(event.startswith("gh-tag") for event in events)
        assert any(event.startswith("gh-ref") for event in events)
    rendered = "\n".join(events + [result["stdout"], result["stderr"]])
    assert "--force" not in rendered
    assert "update-ref refs/tags" not in rendered


def test_coordinator_source_orders_token_before_final_checks_and_never_exports_it() -> None:
    source = COORDINATOR.read_text(encoding="utf-8")

    prompt = source.index("Short-lived release App installation token")
    final_ci = source.index("scripts/verify_main_ci.py")
    prepare = source.index('verify_account_receipt.py" prepare-tag')
    absence = source.index("git/matching-refs/tags")
    tag_post = source.index('"repos/$REPOSITORY/git/tags"')
    ref_post = source.index('"repos/$REPOSITORY/git/refs"')
    assert prompt < final_ci < prepare < absence < tag_post < ref_post
    assert "export GPT2AGENT_RELEASE_APP_TOKEN" not in source
    assert "--force-with-lease" not in source
    assert "git push" not in source
    assert "git tag" not in source


@pytest.mark.parametrize("timeout", ("", "0", "901", "-1", "1.5", "not-a-number"))
def test_coordinator_rejects_unbounded_or_malformed_token_timeout(
    tmp_path: Path,
    timeout: str,
) -> None:
    events, result = _harness(tmp_path, token_timeout=timeout)

    assert result["returncode"] != "0"
    assert "release App token timeout is invalid" in result["stderr"]
    assert not any(event.startswith("gh-auth") for event in events)


def test_coordinator_signal_during_token_prompt_fails_and_cleans_up(tmp_path: Path) -> None:
    events, result = _harness(tmp_path, terminate_at_prompt=True)

    assert result["returncode"] == "130"
    assert "git-clean" in events
    assert not any(event.startswith("python-") for event in events)
