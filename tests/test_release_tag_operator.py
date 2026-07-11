"""Behavioral tests for the closed pre-tag coordinator."""

from __future__ import annotations

import os
import signal
import shlex
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = PROJECT_ROOT / "scripts" / "create_release_tag.sh"
COMMIT = "1" * 40
TREE = "2" * 40
RECEIPT_SHA256 = "3" * 64
ARTIFACT_DIGEST = "sha256:" + "4" * 64
TAG_OBJECT_SHA = "5" * 40
MISMATCH_SHA = "6" * 40
APP_TOKEN = "app-installation-token-canary"
OPERATOR_TOKEN = "operator-read-token-canary"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _make_harness(tmp_path: Path) -> tuple[list[str], dict[str, str], list[str]]:
    trusted_bin = tmp_path / "trusted-bin"
    poison_bin = tmp_path / "poison-bin"
    trusted_bin.mkdir()
    poison_bin.mkdir()
    event_log = tmp_path / "events"
    poison_log = tmp_path / "poison-events"
    mode_file = tmp_path / "mode"
    fake_python = trusted_bin / "trusted-python"
    fake_gh = trusted_bin / "gh"
    fake_git = trusted_bin / "git"
    python_header = (
        "#!/bin/bash\nset -euo pipefail\n"
        f"EVENT_LOG={shlex.quote(str(event_log))}\n"
        f"FAKE_MODE=$(/usr/bin/cat {shlex.quote(str(mode_file))})\n"
    )
    _write_executable(
        fake_python,
        python_header
        + """case "$*" in
  *verify_release_tools.py*)
    printf 'python-tools token=%s\n' "${GH_TOKEN-unset}" >> "$EVENT_LOG"
    if [ "${FAKE_MODE-}" = tool-invalid ]; then exit 17; fi
    ;;
  *verify_main_ci.py*)
    printf 'python-ci token=%s\n' "${GH_TOKEN-unset}" >> "$EVENT_LOG"
    if [ "${FAKE_MODE-}" = ci-fail ]; then exit 19; fi
    ;;
  *audit_release_governance.py*)
    printf 'python-governance token=%s\n' "${GH_TOKEN-unset}" >> "$EVENT_LOG"
    if [ "${FAKE_MODE-}" = governance-fail ]; then exit 20; fi
    ;;
  *verify_remote_action_pin.py*)
    printf 'python-action-pin token=%s\n' "${GH_TOKEN-unset}" >> "$EVENT_LOG"
    case "${FAKE_MODE-}" in
      action-unresolved|action-mismatch|action-redirect|action-auth-fail) exit 21;;
    esac
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
        fake_gh,
        f"""#!/bin/bash
set -euo pipefail
EVENT_LOG={shlex.quote(str(event_log))}
FAKE_MODE=$(/usr/bin/cat {shlex.quote(str(mode_file))})
printf 'gh-env token=%s host=%s config=%s debug=%s path=%s\n' "${{GH_TOKEN-unset}}" "${{GH_HOST-unset}}" "${{GH_CONFIG_DIR-unset}}" "${{GH_DEBUG-unset}}" "${{PATH-unset}}" >> "$EVENT_LOG"
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
    case "${{FAKE_MODE-}}" in
      ref-ambiguous-success|ref-absent) exit 22;;
      signal-ref) /bin/kill -TERM "$PPID"; /bin/sleep 0.1; exit 22;;
    esac
    ;;
  *git/ref/tags*)
    if [ "${{GH_TOKEN-unset}}" = '{APP_TOKEN}' ]; then
      printf 'gh-readback token=%s\n' "${{GH_TOKEN-unset}}" >> "$EVENT_LOG"
      case "${{FAKE_MODE-}}" in readback-fail|ref-absent) exit 23;; esac
      if [ "${{FAKE_MODE-}}" = mismatch ]; then printf '%s\n' '{MISMATCH_SHA}'; else printf '%s\n' '{TAG_OBJECT_SHA}'; fi
    else
      printf 'gh-independent-ref token=%s\n' "${{GH_TOKEN-unset}}" >> "$EVENT_LOG"
      if [ "${{FAKE_MODE-}}" = ref-absent ]; then exit 24; fi
      if [ "${{FAKE_MODE-}}" = mismatch ]; then
        printf '%s\n' $'refs/tags/v0.0.12\ttag\t{MISMATCH_SHA}'
      else
        printf '%s\n' $'refs/tags/v0.0.12\ttag\t{TAG_OBJECT_SHA}'
      fi
    fi
    ;;
  *'--method GET'*git/tags/{TAG_OBJECT_SHA}*)
    printf 'gh-independent-tag token=%s\n' "${{GH_TOKEN-unset}}" >> "$EVENT_LOG"
    if [ "${{FAKE_MODE-}}" = mismatch ]; then
      printf '%s\n' $'v0.0.12\t{TAG_OBJECT_SHA}\tcommit\t{MISMATCH_SHA}'
    else
      printf '%s\n' $'v0.0.12\t{TAG_OBJECT_SHA}\tcommit\t{COMMIT}'
    fi
    ;;
  *) exit 98 ;;
esac
""",
    )
    _write_executable(
        fake_git,
        f"""#!/bin/bash
set -euo pipefail
EVENT_LOG={shlex.quote(str(event_log))}
FAKE_MODE=$(/usr/bin/cat {shlex.quote(str(mode_file))})
printf 'git-env token=%s dir=%s worktree=%s ssh=%s path=%s\n' "${{GH_TOKEN-unset}}" "${{GIT_DIR-unset}}" "${{GIT_WORK_TREE-unset}}" "${{GIT_SSH_COMMAND-unset}}" "${{PATH-unset}}" >> "$EVENT_LOG"
case "$*" in
  *'rev-parse HEAD^{{tree}}'*) printf '%s\n' '{TREE}' ;;
  *'rev-parse HEAD'*) printf '%s\n' '{COMMIT}' ;;
  *'status --porcelain=v1'*) exit 0 ;;
  *'show-ref --verify --quiet refs/release-verification/'*) exit 1 ;;
  *'fetch --no-tags --no-write-fetch-head'*)
    ref=${{@: -1}}
    printf 'git-fetch %s\n' "${{ref#*:}}" >> "$EVENT_LOG"
    if [ "${{FAKE_MODE-}}" = ref-absent ]; then exit 24; fi
    ;;
  *'cat-file -t refs/release-verification/'*) printf 'tag\n' ;;
  *'rev-parse refs/release-verification/'*'^{{}}'*) printf '%s\n' '{COMMIT}' ;;
  *'rev-parse refs/release-verification/'*)
    if [ "${{FAKE_MODE-}}" = mismatch ]; then printf '%s\n' '{MISMATCH_SHA}'; else printf '%s\n' '{TAG_OBJECT_SHA}'; fi
    ;;
  *'show-ref --hash --verify refs/release-verification/'*)
    case "${{FAKE_MODE-}}" in
      mismatch|cleanup-race) printf '%s\n' '{MISMATCH_SHA}' ;;
      *) printf '%s\n' '{TAG_OBJECT_SHA}' ;;
    esac
    ;;
  *'update-ref -d refs/release-verification/'*) printf 'git-clean %s\n' "${{@: -2:1}}" >> "$EVENT_LOG" ;;
  *) exit 99 ;;
esac
""",
    )
    for name in ("gh", "git", "env", "rm", "rmdir", "mktemp", "chmod"):
        _write_executable(
            poison_bin / name,
            f"#!/bin/bash\nprintf '%s\\n' {name!r} >> {shlex.quote(str(poison_log))}\nexit 111\n",
        )
    checkout = tmp_path / "checkout"
    dist = tmp_path / "dist"
    checkout.mkdir()
    dist.mkdir()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n", encoding="utf-8")
    policy = tmp_path / "reviewed-policy.json"
    policy.write_text("{}\n", encoding="utf-8")
    command = [
        str(COORDINATOR),
        "--python",
        str(fake_python),
        "--gh",
        str(fake_gh),
        "--git",
        str(fake_git),
        "--governance-policy",
        str(policy),
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
    environment = {
        **os.environ,
        "PATH": str(poison_bin),
        "EVENT_LOG": str(event_log),
        "POISON_LOG": str(poison_log),
        "TMPDIR": str(tmp_path),
        "GH_HOST": "attacker.invalid",
        "GH_CONFIG_DIR": str(tmp_path / "attacker-gh-config"),
        "GH_DEBUG": "api",
        "GIT_DIR": str(tmp_path / "attacker-git-dir"),
        "GIT_WORK_TREE": str(tmp_path / "attacker-worktree"),
        "GIT_SSH_COMMAND": "attacker-git-ssh-command",
    }
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GPT2AGENT_RELEASE_APP_TOKEN"):
        environment.pop(key, None)
    return command, environment, [str(event_log), str(poison_log), str(mode_file)]


def _run_harness(
    tmp_path: Path,
    *,
    mode: str = "ok",
    token_timeout: str | None = None,
    terminate_at_prompt: bool = False,
    omit_policy: bool = False,
) -> tuple[list[str], dict[str, str], list[str]]:
    command, environment, logs = _make_harness(tmp_path)
    event_log, poison_log, mode_file = map(Path, logs)
    mode_file.write_text(mode + "\n", encoding="utf-8")
    environment["FAKE_MODE"] = mode
    if token_timeout is not None:
        environment["GPT2AGENT_RELEASE_TOKEN_TIMEOUT_SECONDS"] = token_timeout
    if omit_policy:
        index = command.index("--governance-policy")
        del command[index : index + 2]
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
    poison = poison_log.read_text(encoding="utf-8").splitlines() if poison_log.exists() else []
    return (
        events,
        {"returncode": str(result.returncode), "stdout": result.stdout, "stderr": result.stderr},
        poison,
    )


def test_coordinator_closes_governance_action_and_tag_sequence(tmp_path: Path) -> None:
    events, result, poison = _run_harness(tmp_path)

    assert result["returncode"] == "0", result["stderr"]
    significant = [event for event in events if not event.startswith(("gh-env", "git-env"))]
    assert significant[:10] == [
        "python-tools token=unset",
        "gh-auth token=unset",
        f"python-action-pin token={OPERATOR_TOKEN}",
        f"python-ci token={OPERATOR_TOKEN}",
        f"python-governance token={OPERATOR_TOKEN}",
        "python-prepare token=unset",
        f"gh-absence token={APP_TOKEN}",
        f"gh-tag token={APP_TOKEN}",
        f"gh-ref token={APP_TOKEN}",
        f"gh-readback token={APP_TOKEN}",
    ]
    assert f"gh-independent-ref token={OPERATOR_TOKEN}" in events
    assert f"gh-independent-tag token={OPERATOR_TOKEN}" in events
    assert any(event.startswith("git-fetch refs/release-verification/") for event in events)
    assert APP_TOKEN not in "\n".join(
        event for event in events if event.startswith(("python-", "git-env"))
    )
    assert poison == []


def test_app_token_calls_ignore_path_and_ambient_gh_or_git_environment(tmp_path: Path) -> None:
    events, result, poison = _run_harness(tmp_path)

    assert result["returncode"] == "0", result["stderr"]
    app_env = [event for event in events if event.startswith(f"gh-env token={APP_TOKEN}")]
    assert app_env
    assert all(
        "host=unset config=/nonexistent debug=unset path=/usr/bin:/bin" in event
        for event in app_env
    )
    assert all(
        "token=unset dir=unset worktree=unset ssh=unset path=/usr/bin:/bin" in event
        for event in events
        if event.startswith("git-env")
    )
    assert poison == []


@pytest.mark.parametrize(
    "mode",
    (
        "ci-fail",
        "governance-fail",
        "action-unresolved",
        "action-mismatch",
        "action-redirect",
        "action-auth-fail",
        "existing",
    ),
)
def test_pre_mutation_failures_never_create_a_tag_ref(
    tmp_path: Path,
    mode: str,
) -> None:
    events, result, _ = _run_harness(tmp_path, mode=mode)

    assert result["returncode"] != "0"
    assert not any(event.startswith("gh-tag") for event in events)
    assert not any(event.startswith("gh-ref") for event in events)
    if mode.startswith("action-"):
        assert not any(event.startswith("python-ci") for event in events)


def test_reviewed_governance_policy_is_mandatory(tmp_path: Path) -> None:
    events, result, _ = _run_harness(tmp_path, omit_policy=True)

    assert result["returncode"] == "2"
    assert events == []


@pytest.mark.parametrize("mode", ("ref-ambiguous-success", "readback-fail"))
def test_independent_fetch_recovers_ambiguous_ref_mutation_success(
    tmp_path: Path,
    mode: str,
) -> None:
    events, result, _ = _run_harness(tmp_path, mode=mode)

    assert result["returncode"] == "0", result["stderr"]
    assert any(event.startswith("gh-ref") for event in events)
    assert sum(event.startswith("gh-ref") for event in events) == 1
    assert f"gh-independent-ref token={OPERATOR_TOKEN}" in events
    assert f"gh-independent-tag token={OPERATOR_TOKEN}" in events
    assert any(event.startswith("git-fetch") for event in events)
    assert "independent remote verification recovered" in result["stdout"]


@pytest.mark.parametrize("mode", ("ref-absent", "mismatch"))
def test_post_ref_absence_or_mismatch_emits_irreversible_recovery_error(
    tmp_path: Path,
    mode: str,
) -> None:
    events, result, _ = _run_harness(tmp_path, mode=mode)

    assert result["returncode"] != "0"
    assert any(event.startswith("gh-ref") for event in events)
    assert any(event.startswith("git-fetch") for event in events)
    assert "IRREVERSIBLE POST-REF STATE" in result["stderr"]
    rendered = "\n".join(events + [result["stdout"], result["stderr"]])
    assert "update-ref refs/tags" not in rendered
    assert "git push" not in rendered


def test_concurrent_coordinators_use_distinct_verification_refs(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_run_harness, (first, second)))

    refs = []
    for events, result, _ in results:
        assert result["returncode"] == "0", result["stderr"]
        refs.extend(event.split(" ", 1)[1] for event in events if event.startswith("git-fetch "))
    assert len(refs) == 2
    assert refs[0] != refs[1]


def test_cleanup_does_not_delete_a_verification_ref_changed_by_another_process(
    tmp_path: Path,
) -> None:
    events, result, _ = _run_harness(tmp_path, mode="cleanup-race")

    assert result["returncode"] == "0", result["stderr"]
    assert not any(event.startswith("git-clean") for event in events)


def test_coordinator_source_never_updates_or_deletes_a_remote_ref() -> None:
    source = COORDINATOR.read_text(encoding="utf-8")

    prompt = source.index("Short-lived release App installation token")
    final_ci = source.index("scripts/verify_main_ci.py")
    governance = source.index("scripts/audit_release_governance.py")
    action_pin = source.index("scripts/verify_remote_action_pin.py")
    prepare = source.index('verify_account_receipt.py" prepare-tag')
    tag_post = source.index('"repos/$REPOSITORY/git/tags"')
    ref_post = source.index('"repos/$REPOSITORY/git/refs"')
    fetch = source.index("fetch --no-tags --no-write-fetch-head")
    assert action_pin < prompt < final_ci < governance < prepare < tag_post < ref_post < fetch
    assert "export GPT2AGENT_RELEASE_APP_TOKEN" not in source
    assert "git push" not in source
    assert "git tag" not in source
    assert "--method DELETE" not in source
    assert "--method PATCH" not in source


@pytest.mark.parametrize("timeout", ("", "0", "901", "-1", "1.5", "not-a-number"))
def test_coordinator_rejects_unbounded_or_malformed_token_timeout(
    tmp_path: Path,
    timeout: str,
) -> None:
    events, result, _ = _run_harness(tmp_path, token_timeout=timeout)

    assert result["returncode"] != "0"
    assert "release App token timeout is invalid" in result["stderr"]
    assert not any(event.startswith("gh-auth") for event in events)


def test_signal_during_token_prompt_removes_only_owned_scratch(tmp_path: Path) -> None:
    events, result, _ = _run_harness(tmp_path, terminate_at_prompt=True)

    assert result["returncode"] == "130"
    assert not any(event.startswith("python-ci") for event in events)
    assert list(tmp_path.glob("gpt2agent-release-tag.*")) == []


def test_signal_after_ref_attempt_reports_irreversible_state_and_cleans_scratch(
    tmp_path: Path,
) -> None:
    events, result, _ = _run_harness(tmp_path, mode="signal-ref")

    assert result["returncode"] == "130"
    assert any(event.startswith("gh-ref") for event in events)
    assert "IRREVERSIBLE POST-REF STATE" in result["stderr"]
    assert list(tmp_path.glob("gpt2agent-release-tag.*")) == []
