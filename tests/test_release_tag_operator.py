"""Behavioral tests for the closed pre-tag coordinator."""

from __future__ import annotations

import os
import signal
import shlex
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="the immutable release-tag coordinator requires Linux GNU userland",
)


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


def _real_git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _create_real_checkout(tmp_path: Path) -> tuple[Path, str, str]:
    checkout = tmp_path / "real-checkout"
    checkout.mkdir()
    _real_git(checkout, "init", "--quiet")
    _real_git(checkout, "config", "user.name", "Release Tests")
    _real_git(checkout, "config", "user.email", "release-tests@example.invalid")
    (checkout / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (checkout / "reviewed.py").write_text("VALUE = 1\n", encoding="utf-8")
    _real_git(checkout, "add", ".")
    _real_git(checkout, "commit", "--quiet", "-m", "fixture")
    return (
        checkout,
        _real_git(checkout, "rev-parse", "HEAD"),
        _real_git(checkout, "rev-parse", "HEAD^{tree}"),
    )


def _copy_real_checkout_tree(checkout: Path, destination: Path) -> None:
    destination.mkdir()
    for name in (".gitignore", "reviewed.py"):
        (destination / name).write_bytes((checkout / name).read_bytes())


def _run_real_checkout_probe(
    tmp_path: Path,
    checkout: Path,
    commit: str,
    tree: str,
) -> subprocess.CompletedProcess[str]:
    stat_probe = subprocess.run(
        ["/usr/bin/stat", "--format=%u", str(checkout)],
        check=False,
        capture_output=True,
        text=True,
    )
    if stat_probe.returncode != 0:
        pytest.skip("release coordinator requires GNU stat")

    support = tmp_path / f"shell-probe-{checkout.name}"
    support.mkdir()
    fake_python = support / "python"
    fake_gh = support / "gh"
    _write_executable(fake_python, "#!/bin/sh\nexit 97\n")
    _write_executable(fake_gh, "#!/bin/sh\nexit 98\n")
    policy = support / "policy.json"
    receipt = support / "receipt.json"
    dist = support / "dist"
    policy.write_text("{}\n", encoding="utf-8")
    receipt.write_text("{}\n", encoding="utf-8")
    dist.mkdir()

    source = COORDINATOR.read_text(encoding="utf-8")
    exact_guard = "if [[ $GH_BIN != /usr/bin/gh || $GIT_BIN != /usr/bin/git ]]; then usage; fi"
    tool_checks = (
        'require_root_protected_tool "$GH_BIN" gh\n'
        'require_root_protected_tool "$GIT_BIN" git'
    )
    first_boundary = (
        "verify_checkout_state\n\n"
        'run_python_clean "$CHECKOUT/scripts/verify_release_tools.py"'
    )
    assert source.count(exact_guard) == 1
    assert source.count(tool_checks) == 1
    assert source.count(first_boundary) == 1
    source = source.replace(exact_guard, ": # test-only injected tools", 1)
    source = source.replace(tool_checks, ": # test-only trusted tools", 1)
    source = source.replace(
        first_boundary,
        "verify_checkout_state\nexit 0\n\n"
        'run_python_clean "$CHECKOUT/scripts/verify_release_tools.py"',
        1,
    )
    probe = support / "checkout-probe.sh"
    _write_executable(probe, source)
    command = [
        str(probe),
        "--python",
        str(fake_python),
        "--gh",
        str(fake_gh),
        "--git",
        "/usr/bin/git",
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
        commit,
        "--tree",
        tree,
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
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env={"HOME": str(tmp_path), "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def _make_harness(tmp_path: Path) -> tuple[list[str], dict[str, str], list[str]]:
    trusted_bin = tmp_path / "trusted-bin"
    poison_bin = tmp_path / "poison-bin"
    trusted_bin.mkdir()
    poison_bin.mkdir()
    event_log = tmp_path / "events"
    poison_log = tmp_path / "poison-events"
    mode_file = tmp_path / "mode"
    operator_home = tmp_path / "operator-home"
    operator_home.mkdir(mode=0o700)
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
    if [[ " $* " == *' --print-pin '* ]]; then
      printf '%s\n' '15f56b2c16c5923e81df9428c69256237a004c20'
    fi
    ;;
  *'verify_account_receipt.py prepare-tag'*)
    printf 'python-prepare token=%s\n' "${GH_TOKEN-unset}" >> "$EVENT_LOG"
    output=''
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --output ]; then output=$2; shift 2; else shift; fi
    done
    printf 'python-prepare-output=%s\n' "$output" >> "$EVENT_LOG"
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
CHECKOUT={shlex.quote(str(tmp_path / "checkout"))}
CHECKOUT_SELECTED=0
CHECKOUT_PINNED=0
arguments=("$@")
for ((index = 0; index < ${{#arguments[@]}}; index++)); do
  if [[ ${{arguments[index]}} = -C && ${{arguments[index + 1]-}} = "$CHECKOUT" ]]; then
    CHECKOUT_SELECTED=1
  fi
  if [[ ${{arguments[index]}} = "--work-tree=$CHECKOUT" ]]; then
    CHECKOUT_PINNED=1
  fi
done
if (( CHECKOUT_SELECTED == 1 && CHECKOUT_PINNED == 0 )) && \
  [[ " $* " != *' rev-parse --absolute-git-dir '* ]]; then
  printf '%s\n' 'git-checkout-unpinned' >> "$EVENT_LOG"
fi
printf 'git-env token=%s dir=%s worktree=%s ssh=%s path=%s global=%s nosystem=%s system=%s\n' "${{GH_TOKEN-unset}}" "${{GIT_DIR-unset}}" "${{GIT_WORK_TREE-unset}}" "${{GIT_SSH_COMMAND-unset}}" "${{PATH-unset}}" "${{GIT_CONFIG_GLOBAL-unset}}" "${{GIT_CONFIG_NOSYSTEM-unset}}" "${{GIT_CONFIG_SYSTEM-unset}}" >> "$EVENT_LOG"
if [[ " $* " != *' -c core.fsmonitor=false '* || \
  " $* " != *' -c core.hooksPath=/dev/null '* ]]; then
  printf '%s\n' 'git-missing-safe-config' >> "$EVENT_LOG"
  exit 100
fi
case "$*" in
  *'init --bare --quiet'*) ;;
  *'fetch --force --no-tags --no-write-fetch-head'*'+refs/heads/main:refs/remotes/origin/main'*)
    ref=${{@: -1}}
    printf 'git-main-fetch %s\n' "${{ref#*:}}" >> "$EVENT_LOG"
    ;;
  *'merge-base --is-ancestor 15f56b2c16c5923e81df9428c69256237a004c20 refs/remotes/origin/main'*)
    if [ "${{FAKE_MODE-}}" = action-not-on-main ]; then exit 1; fi
    ;;
  *'rev-parse --absolute-git-dir'*)
    printf '%s\n' 'git-checkout-admin' >> "$EVENT_LOG"
    printf '%s\n' "$CHECKOUT/.git"
    ;;
  *'rev-parse --show-toplevel'*)
    printf '%s\n' 'git-checkout-root' >> "$EVENT_LOG"
    if (( CHECKOUT_PINNED == 0 )); then
      printf '%s\n' '/attacker/worktree'
    else
      printf '%s\n' "$CHECKOUT"
    fi
    ;;
  *'ls-files -v'*)
    printf '%s\n' 'git-checkout-index' >> "$EVENT_LOG"
    case "${{FAKE_MODE-}}" in
      assume-unchanged) printf '%s\n' 'h reviewed.py' ;;
      skip-worktree) printf '%s\n' 'S reviewed.py' ;;
      *) printf '%s\n' 'H reviewed.py' ;;
    esac
    ;;
  *'rev-parse HEAD^{{tree}}'*) printf '%s\n' '{TREE}' ;;
  *'rev-parse HEAD'*)
    checks=$(/usr/bin/grep -c '^git-checkout-head$' "$EVENT_LOG" || true)
    printf '%s\n' 'git-checkout-head' >> "$EVENT_LOG"
    if [[ "${{FAKE_MODE-}}" = checkout-drift && $checks -gt 0 ]] || \
      [[ "${{FAKE_MODE-}}" = checkout-third-drift && $checks -gt 1 ]]; then
      printf '%s\n' '{MISMATCH_SHA}'
    else
      printf '%s\n' '{COMMIT}'
    fi
    ;;
  *'status --porcelain=v1'*)
    checks=$(/usr/bin/grep -c '^git-checkout-status$' "$EVENT_LOG" || true)
    printf '%s\n' 'git-checkout-status' >> "$EVENT_LOG"
    if [[ "${{FAKE_MODE-}}" = core-worktree-redirect && $CHECKOUT_PINNED = 1 ]] || \
      [[ "${{FAKE_MODE-}}" = checkout-dirty && $checks -gt 0 ]]; then
      printf '%s\n' ' M reviewed.py'
    fi
    ;;
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
    (checkout / ".git").mkdir()
    dist.mkdir()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n", encoding="utf-8")
    policy = tmp_path / "reviewed-policy.json"
    policy.write_text("{}\n", encoding="utf-8")
    irreversible_state = tmp_path / "irreversible-state"
    harness_coordinator = tmp_path / "create-release-tag-test-harness.sh"
    harness_source = COORDINATOR.read_text(encoding="utf-8")
    exact_guard = "if [[ $GH_BIN != /usr/bin/gh || $GIT_BIN != /usr/bin/git ]]; then usage; fi"
    tool_checks = (
        'require_root_protected_tool "$GH_BIN" gh\n'
        'require_root_protected_tool "$GIT_BIN" git'
    )
    assert harness_source.count(exact_guard) == 1
    assert harness_source.count(tool_checks) == 1
    harness_source = harness_source.replace(exact_guard, ": # test-only injected tools", 1)
    harness_source = harness_source.replace(tool_checks, ": # test-only trusted tools", 1)
    _write_executable(harness_coordinator, harness_source)
    command = [
        str(harness_coordinator),
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
        "--irreversible-state-file",
        str(irreversible_state),
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
        "HOME": str(operator_home),
        "GH_HOST": "attacker.invalid",
        "GH_CONFIG_DIR": str(tmp_path / "attacker-gh-config"),
        "GH_DEBUG": "api",
        "GIT_DIR": str(tmp_path / "attacker-git-dir"),
        "GIT_WORK_TREE": str(tmp_path / "attacker-worktree"),
        "GIT_SSH_COMMAND": "attacker-git-ssh-command",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "attacker-global-gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "0",
        "GIT_CONFIG_SYSTEM": str(tmp_path / "attacker-system-gitconfig"),
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
    app_token: str = APP_TOKEN,
    tmpdir: str | None = None,
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
    if tmpdir is not None:
        environment["TMPDIR"] = tmpdir
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
            input=app_token + "\n",
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


def test_real_shell_checkout_rejects_nested_exact_tree_without_git_binding(
    tmp_path: Path,
) -> None:
    checkout, commit, tree = _create_real_checkout(tmp_path)
    nested_snapshot = checkout / "snapshot"
    _copy_real_checkout_tree(checkout, nested_snapshot)

    result = _run_real_checkout_probe(tmp_path, nested_snapshot, commit, tree)

    assert result.returncode != 0
    assert "Git administration binding" in result.stderr


@pytest.mark.parametrize("binding", ("symlink", "unbound-gitfile"))
def test_real_shell_checkout_rejects_forged_git_binding(
    tmp_path: Path,
    binding: str,
) -> None:
    checkout, commit, tree = _create_real_checkout(tmp_path)
    nested_snapshot = checkout / "snapshot"
    _copy_real_checkout_tree(checkout, nested_snapshot)
    marker = nested_snapshot / ".git"
    if binding == "symlink":
        marker.symlink_to(checkout / ".git", target_is_directory=True)
    else:
        marker.write_text(f"gitdir: {checkout / '.git'}\n", encoding="utf-8")

    result = _run_real_checkout_probe(tmp_path, nested_snapshot, commit, tree)

    assert result.returncode != 0
    assert "Git administration binding" in result.stderr


def test_real_shell_checkout_accepts_normal_clone_and_linked_worktree(tmp_path: Path) -> None:
    checkout, commit, tree = _create_real_checkout(tmp_path)
    linked = tmp_path / "linked"
    _real_git(checkout, "worktree", "add", "--quiet", "--detach", str(linked), commit)

    clone_result = _run_real_checkout_probe(tmp_path, checkout, commit, tree)
    linked_result = _run_real_checkout_probe(tmp_path, linked, commit, tree)

    assert clone_result.returncode == 0, clone_result.stderr
    assert linked_result.returncode == 0, linked_result.stderr


def test_real_shell_checkout_rejects_linked_worktree_with_wrong_admin_backlink(
    tmp_path: Path,
) -> None:
    checkout, commit, tree = _create_real_checkout(tmp_path)
    linked = tmp_path / "linked"
    _real_git(checkout, "worktree", "add", "--quiet", "--detach", str(linked), commit)
    marker = linked / ".git"
    git_dir = Path(marker.read_text(encoding="utf-8").removeprefix("gitdir: ").strip())
    (git_dir / "gitdir").write_text(f"{tmp_path / 'wrong' / '.git'}\n", encoding="utf-8")

    result = _run_real_checkout_probe(tmp_path, linked, commit, tree)

    assert result.returncode != 0
    assert "Git administration binding" in result.stderr


def test_real_shell_checkout_ignores_hostile_local_core_worktree(tmp_path: Path) -> None:
    checkout, commit, tree = _create_real_checkout(tmp_path)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    _real_git(checkout, "config", "core.worktree", str(redirected))
    assert Path(_real_git(checkout, "rev-parse", "--show-toplevel")) == redirected

    result = _run_real_checkout_probe(tmp_path, checkout, commit, tree)

    assert result.returncode == 0, result.stderr


def test_coordinator_closes_governance_action_and_tag_sequence(tmp_path: Path) -> None:
    events, result, poison = _run_harness(tmp_path)

    assert result["returncode"] == "0", result["stderr"]
    significant = [
        event
        for event in events
        if not event.startswith(
            (
                "gh-env",
                "git-env",
                "git-checkout-",
                "git-main-fetch",
                "python-prepare-output=",
            )
        )
    ]
    assert significant[:11] == [
        "python-tools token=unset",
        "python-governance token=unset",
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
    assert list((tmp_path / "operator-home").glob(".gpt2agent-release-tag.*")) == []
    assert poison == []
    assert (tmp_path / "irreversible-state").read_text(encoding="utf-8") == (
        f"attempted refs/tags/v0.0.12 object={TAG_OBJECT_SHA} commit={COMMIT}\n"
    )


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


def test_every_git_call_disables_executable_local_config(tmp_path: Path) -> None:
    events, result, _ = _run_harness(tmp_path)

    assert result["returncode"] == "0", result["stderr"]
    assert "git-missing-safe-config" not in events
    assert "git-checkout-unpinned" not in events
    admin_checks = [index for index, event in enumerate(events) if event == "git-checkout-admin"]
    root_checks = [index for index, event in enumerate(events) if event == "git-checkout-root"]
    assert len(admin_checks) == len(root_checks) == 3
    assert all(admin < root for admin, root in zip(admin_checks, root_checks, strict=True))
    git_environments = [event for event in events if event.startswith("git-env")]
    assert git_environments
    assert all(
        "global=/dev/null nosystem=1 system=/dev/null" in event
        for event in git_environments
    )


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
    assert not (tmp_path / "irreversible-state").exists()
    if mode.startswith("action-"):
        assert not any(event.startswith("python-ci") for event in events)


def test_reviewed_governance_policy_is_mandatory(tmp_path: Path) -> None:
    events, result, _ = _run_harness(tmp_path, omit_policy=True)

    assert result["returncode"] == "2"
    assert events == []


def test_production_coordinator_rejects_injected_gh_and_git_before_execution(
    tmp_path: Path,
) -> None:
    command, environment, logs = _make_harness(tmp_path)
    event_log, _poison_log, mode_file = map(Path, logs)
    mode_file.write_text("ok\n", encoding="utf-8")
    command[0] = str(COORDINATOR)

    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        input=APP_TOKEN + "\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert not event_log.exists()


def test_ambient_tmpdir_is_ignored_for_owner_private_scratch(tmp_path: Path) -> None:
    ambient = tmp_path / "ambient-tmp"
    ambient.mkdir(mode=0o777)
    ambient.chmod(0o777)

    events, result, _ = _run_harness(tmp_path, tmpdir=str(ambient))

    assert result["returncode"] == "0", result["stderr"]
    assert list(ambient.iterdir()) == []
    output_events = [event for event in events if event.startswith("python-prepare-output=")]
    assert len(output_events) == 1
    assert not output_events[0].startswith(f"python-prepare-output={ambient}/")


def test_identical_operator_and_release_app_tokens_fail_before_mutation(tmp_path: Path) -> None:
    events, result, _ = _run_harness(tmp_path, app_token=OPERATOR_TOKEN)

    assert result["returncode"] != "0"
    assert "must be distinct" in result["stderr"]
    assert not any(event.startswith(("gh-tag", "gh-ref")) for event in events)


def test_action_pin_must_be_on_fetched_origin_main_before_mutation(tmp_path: Path) -> None:
    events, result, _ = _run_harness(tmp_path, mode="action-not-on-main")

    assert result["returncode"] != "0"
    assert any(event.startswith("git-main-fetch") for event in events)
    assert "not an ancestor" in result["stderr"]
    assert not any(event.startswith(("gh-tag", "gh-ref")) for event in events)


@pytest.mark.parametrize("mode", ("checkout-drift", "checkout-dirty"))
def test_checkout_identity_and_cleanliness_are_rechecked_before_ref_mutation(
    tmp_path: Path,
    mode: str,
) -> None:
    events, result, _ = _run_harness(tmp_path, mode=mode)

    assert result["returncode"] != "0"
    assert sum(event == "git-checkout-head" for event in events) >= 2
    assert not any(event.startswith("gh-ref") for event in events)


def test_third_checkout_drift_stops_after_tag_object_before_ref_post(tmp_path: Path) -> None:
    events, result, _ = _run_harness(tmp_path, mode="checkout-third-drift")

    assert result["returncode"] != "0"
    assert sum(event == "git-checkout-head" for event in events) == 3
    assert f"gh-tag token={APP_TOKEN}" in events
    assert not any(event.startswith("gh-ref") for event in events)
    assert "release checkout commit does not match" in result["stderr"]


@pytest.mark.parametrize("mode", ("core-worktree-redirect", "assume-unchanged", "skip-worktree"))
def test_checkout_redirection_and_hidden_index_flags_fail_before_mutation(
    tmp_path: Path,
    mode: str,
) -> None:
    events, result, _ = _run_harness(tmp_path, mode=mode)

    assert result["returncode"] != "0"
    assert not any(event.startswith(("python-tools", "gh-tag", "gh-ref")) for event in events)
    if mode == "core-worktree-redirect":
        assert "release checkout must be clean" in result["stderr"]
        assert "git-checkout-status" in events
    else:
        assert "release checkout index contains hidden paths" in result["stderr"]


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
    assert (tmp_path / "irreversible-state").is_file()
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


def test_coordinator_source_never_updates_or_deletes_a_remote_ref() -> None:
    source = COORDINATOR.read_text(encoding="utf-8")

    prompt = source.index("Short-lived release App installation token")
    final_ci = source.index("scripts/verify_main_ci.py")
    governance_import_preflight = source.index(
        'run_python_clean "$CHECKOUT/scripts/audit_release_governance.py" --help'
    )
    first_checkout_state = source.index(
        "verify_checkout_state\n\nrun_python_clean \"$CHECKOUT/scripts/verify_release_tools.py\""
    )
    tools = source.index("scripts/verify_release_tools.py")
    governance = source.index(
        'run_python_operator "$CHECKOUT/scripts/audit_release_governance.py"'
    )
    action_pin = source.index("scripts/verify_remote_action_pin.py")
    prepare = source.index('verify_account_receipt.py" prepare-tag')
    tag_post = source.index('"repos/$REPOSITORY/git/tags"')
    ref_post = source.index('"repos/$REPOSITORY/git/refs"')
    fetch = source.index("fetch --no-tags --no-write-fetch-head")
    assert (
        first_checkout_state
        < tools
        < governance_import_preflight
        < action_pin
        < prompt
        < final_ci
        < governance
        < prepare
        < tag_post
        < ref_post
        < fetch
    )
    assert "export GPT2AGENT_RELEASE_APP_TOKEN" not in source
    assert "git push" not in source
    assert "git tag" not in source
    assert "--method DELETE" not in source
    assert "--method PATCH" not in source
    assert "${TMPDIR" not in source
    assert '[[ $GH_BIN != /usr/bin/gh || $GIT_BIN != /usr/bin/git ]]' in source


def test_coordinator_ignores_bash_startup_environment(tmp_path: Path) -> None:
    marker = tmp_path / "bash-env-ran"
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(
        f": > {shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["BASH_ENV"] = str(bash_env)

    result = subprocess.run(
        [str(COORDINATOR)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert not marker.exists()


def test_final_checkout_check_is_immediately_before_ref_post() -> None:
    source = COORDINATOR.read_text(encoding="utf-8")
    ref_post = source.index('if gh_app --method POST \\\n  "repos/$REPOSITORY/git/refs"')
    final_check = source.rindex("verify_checkout_state", 0, ref_post)
    boundary = source[final_check:ref_post]

    assert source.count("\nverify_checkout_state\n") == 3
    assert "merge-base" not in boundary
    assert "run_git" not in boundary
    assert "run_python" not in boundary


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
    assert list((tmp_path / "operator-home").glob(".gpt2agent-release-tag.*")) == []


def test_signal_after_ref_attempt_reports_irreversible_state_and_cleans_scratch(
    tmp_path: Path,
) -> None:
    events, result, _ = _run_harness(tmp_path, mode="signal-ref")

    assert result["returncode"] == "130"
    assert any(event.startswith("gh-ref") for event in events)
    assert "IRREVERSIBLE POST-REF STATE" in result["stderr"]
    assert (tmp_path / "irreversible-state").is_file()
    assert list((tmp_path / "operator-home").glob(".gpt2agent-release-tag.*")) == []
