"""Tests for exact trusted tools and the remote publication-action gate."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scripts.verify_remote_action_pin import ActionVerificationError, _extract_pin


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_CHECK = PROJECT_ROOT / "scripts" / "verify_release_tools.py"
ACTION_CHECK = PROJECT_ROOT / "scripts" / "verify_remote_action_pin.py"
ACTION_DIRECTORY = PROJECT_ROOT / ".github" / "actions" / "publish-exact-github-release"
ACTION_PIN = "530db492615ad42a1dde733de89d8893e05ede49"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_tool_check_accepts_canonical_root_owned_usr_binaries() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(TOOL_CHECK),
            "check",
            "--gh",
            "/usr/bin/gh",
            "--git",
            "/usr/bin/git",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_tool_check_rejects_user_owned_or_noncanonical_tools(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh"
    fake_git = tmp_path / "git"
    _write_executable(fake_gh, "#!/bin/sh\nexit 0\n")
    _write_executable(fake_git, "#!/bin/sh\nexit 0\n")

    for gh, git in ((fake_gh, fake_git), (Path("/bin/gh"), Path("/bin/git"))):
        result = subprocess.run(
            [sys.executable, str(TOOL_CHECK), "check", "--gh", str(gh), "--git", str(git)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert result.stdout == ""


def test_tool_check_accepts_only_exact_usr_bin_gh_and_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.verify_release_tools as tools

    metadata = os.stat("/usr/bin/git")
    monkeypatch.setattr(tools, "_canonical", lambda path, _label: (path, metadata))

    with pytest.raises(tools.ToolValidationError, match="trusted system executable"):
        tools.validate_tool(Path("/usr/local/bin/git"), "git")


def test_tool_check_requires_a_protected_nonlinked_reviewed_policy() -> None:
    with tempfile.TemporaryDirectory(
        prefix=".gpt2agent-policy-test-",
        dir=Path.home(),
    ) as temporary:
        private_root = Path(temporary)
        private_root.chmod(0o700)
        policy = private_root / "policy.json"
        policy.write_text("{}\n", encoding="utf-8")
        policy.chmod(0o600)
        command = [
            sys.executable,
            str(TOOL_CHECK),
            "check",
            "--gh",
            "/usr/bin/gh",
            "--git",
            "/usr/bin/git",
            "--policy",
            str(policy),
        ]

        accepted = subprocess.run(command, capture_output=True, text=True, check=False)
        assert accepted.returncode == 0, accepted.stderr

        policy.chmod(0o622)
        writable = subprocess.run(command, capture_output=True, text=True, check=False)
        assert writable.returncode != 0

        policy.chmod(0o600)
        linked = private_root / "policy-link.json"
        linked.symlink_to(policy)
        symbolic = subprocess.run(
            [*command[:-1], str(linked)], capture_output=True, text=True, check=False
        )
        assert symbolic.returncode != 0


def test_tool_check_rejects_policy_below_a_writable_ancestor() -> None:
    with tempfile.TemporaryDirectory(
        prefix=".gpt2agent-policy-test-",
        dir=Path.home(),
    ) as temporary:
        unsafe_root = Path(temporary)
        unsafe_root.chmod(0o770)
        private_child = unsafe_root / "private"
        private_child.mkdir(mode=0o700)
        policy = private_child / "policy.json"
        policy.write_text("{}\n", encoding="utf-8")
        policy.chmod(0o600)

        result = subprocess.run(
            [
                sys.executable,
                str(TOOL_CHECK),
                "check",
                "--gh",
                "/usr/bin/gh",
                "--git",
                "/usr/bin/git",
                "--policy",
                str(policy),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "parent path is not protected" in result.stderr


def _action_fixture(tmp_path: Path, *, mode: str = "ok") -> tuple[Path, Path, dict[str, str]]:
    fake_gh = tmp_path / "gh"
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    for name in ("action.yml", "publish.py"):
        payload = (ACTION_DIRECTORY / name).read_bytes()
        if mode == "mismatch" and name == "publish.py":
            payload += b"\n# remote mismatch\n"
        (remote_dir / name).write_bytes(payload)
    event_log = tmp_path / "events"
    mode_file = tmp_path / "mode"
    mode_file.write_text(mode + "\n", encoding="utf-8")
    remote_literal = shlex.quote(str(remote_dir))
    event_literal = shlex.quote(str(event_log))
    mode_literal = shlex.quote(str(mode_file))
    _write_executable(
        fake_gh,
        f"""#!/bin/bash
set -euo pipefail
EVENT_LOG={event_literal}
REMOTE_DIR={remote_literal}
FAKE_MODE=$(/usr/bin/cat {mode_literal})
printf 'host=%s config=%s debug=%s token=%s path=%s\n' "${{GH_HOST-unset}}" "${{GH_CONFIG_DIR-unset}}" "${{GH_DEBUG-unset}}" "${{GH_TOKEN-unset}}" "${{PATH-unset}}" >> "$EVENT_LOG"
endpoint=${{@: -1}}
printf 'endpoint=%s\n' "$endpoint" >> "$EVENT_LOG"
commit_endpoint=repos/robotlearning123/gpt2agent/commits/{ACTION_PIN}
action_endpoint=repos/robotlearning123/gpt2agent/contents/.github/actions/publish-exact-github-release/action.yml?ref={ACTION_PIN}
publish_endpoint=repos/robotlearning123/gpt2agent/contents/.github/actions/publish-exact-github-release/publish.py?ref={ACTION_PIN}
if [[ $endpoint == "$commit_endpoint" ]]; then
  case "${{FAKE_MODE-}}" in
    unresolved) exit 22;;
    redirect)
      printf '%s\n' 'HTTP/2.0 302 Found' 'Content-Type: application/json' 'Location: https://attacker.invalid/action' '' '{{"sha":"{ACTION_PIN}"}}'
      exit 0;;
    auth-fail)
      printf '%s\n' 'HTTP/2.0 401 Unauthorized' 'Content-Type: application/json' '' '{{"message":"bad credentials"}}'
      exit 0;;
  esac
  printf '%s\n' 'HTTP/2.0 200 OK' 'Content-Type: application/json; charset=utf-8' '' '{{"sha":"{ACTION_PIN}"}}'
  exit 0
fi
if [[ $endpoint != "$action_endpoint" && $endpoint != "$publish_endpoint" ]]; then
  exit 64
fi
name=${{endpoint%%[?]*}}
name=${{name##*/}}
printf '%s\n' 'HTTP/2.0 200 OK' 'Content-Type: application/json; charset=utf-8' ''
python3 - "$REMOTE_DIR/$name" <<'PY'
import base64, json, pathlib, sys
payload = pathlib.Path(sys.argv[1]).read_bytes()
print(json.dumps({{"content": base64.b64encode(payload).decode(), "encoding": "base64", "size": len(payload)}}))
PY
""",
    )
    workflow = tmp_path / "release.yml"
    workflow.write_bytes((PROJECT_ROOT / ".github/workflows/release.yml").read_bytes())
    environment = {
        **os.environ,
        "GH_TOKEN": "operator-token",
        "GH_HOST": "attacker.invalid",
        "GH_CONFIG_DIR": str(tmp_path / "attacker-config"),
        "GH_DEBUG": "api",
        "PATH": "/attacker/bin",
        "EVENT_LOG": str(event_log),
        "REMOTE_DIR": str(remote_dir),
        "FAKE_MODE": mode,
    }
    return fake_gh, workflow, environment


def test_remote_action_pin_verifies_full_sha_and_exact_bytes(tmp_path: Path) -> None:
    fake_gh, workflow, environment = _action_fixture(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(ACTION_CHECK),
            "--gh",
            str(fake_gh),
            "--repository",
            "robotlearning123/gpt2agent",
            "--workflow",
            str(workflow),
            "--action-directory",
            str(ACTION_DIRECTORY),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    events = Path(environment["EVENT_LOG"]).read_text(encoding="utf-8")
    assert (
        "host=unset config=/nonexistent debug=unset token=operator-token path=/usr/bin:/bin"
        in events
    )
    endpoints = [
        line.removeprefix("endpoint=")
        for line in events.splitlines()
        if line.startswith("endpoint=")
    ]
    assert endpoints == [
        f"repos/robotlearning123/gpt2agent/commits/{ACTION_PIN}",
        "repos/robotlearning123/gpt2agent/contents/.github/actions/"
        f"publish-exact-github-release/action.yml?ref={ACTION_PIN}",
        "repos/robotlearning123/gpt2agent/contents/.github/actions/"
        f"publish-exact-github-release/publish.py?ref={ACTION_PIN}",
    ]
    assert result.stdout == ""


def test_remote_action_pin_can_return_the_exact_reviewed_pin(tmp_path: Path) -> None:
    fake_gh, workflow, environment = _action_fixture(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(ACTION_CHECK),
            "--gh",
            str(fake_gh),
            "--repository",
            "robotlearning123/gpt2agent",
            "--workflow",
            str(workflow),
            "--action-directory",
            str(ACTION_DIRECTORY),
            "--print-pin",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{ACTION_PIN}\n"


def test_repository_release_workflow_uses_one_reviewed_pin_for_both_modes() -> None:
    workflow = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
    payload = workflow.read_bytes()

    assert _extract_pin(payload, "robotlearning123/gpt2agent") == ACTION_PIN
    assert payload.count(f"publish-exact-github-release@{ACTION_PIN}".encode()) == 2


@pytest.mark.parametrize(
    "mutation",
    ["append-comment", "change-title-byte", "change-action-pin", "append-decoy"],
)
def test_action_pin_extractor_rejects_any_workflow_byte_mutation_by_digest(
    mutation: str,
) -> None:
    source = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_bytes()
    if mutation == "append-comment":
        mutated = source + b"# byte mutation\n"
    elif mutation == "change-title-byte":
        mutated = source.replace(b"name: Release", b"name: release", 1)
    elif mutation == "change-action-pin":
        mutated = source.replace(ACTION_PIN.encode(), b"f" * 40)
    else:
        mutated = source + b"jobs: {}\n"

    with pytest.raises(
        ActionVerificationError,
        match="release workflow differs from the reviewed exact bytes",
    ):
        _extract_pin(mutated, "robotlearning123/gpt2agent")


def test_remote_action_pin_fails_closed_for_unresolved_or_mismatched_remote(
    tmp_path: Path,
) -> None:
    for mode in ("unresolved", "mismatch", "redirect", "auth-fail"):
        case = tmp_path / mode
        case.mkdir()
        fake_gh, workflow, environment = _action_fixture(case, mode=mode)
        result = subprocess.run(
            [
                sys.executable,
                str(ACTION_CHECK),
                "--gh",
                str(fake_gh),
                "--repository",
                "robotlearning123/gpt2agent",
                "--workflow",
                str(workflow),
                "--action-directory",
                str(ACTION_DIRECTORY),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert result.stdout == ""
        assert "publication action verification failed" in result.stderr


def test_remote_action_gate_reports_digest_error_for_mutated_workflow_bytes(
    tmp_path: Path,
) -> None:
    fake_gh, workflow, environment = _action_fixture(tmp_path)
    exact = workflow.read_text(encoding="utf-8")
    symbolic = exact.replace(ACTION_PIN, "main")
    for source in ("name: missing\n", exact * 2, symbolic, exact + "# mutation\n"):
        workflow.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(ACTION_CHECK),
                "--gh",
                str(fake_gh),
                "--repository",
                "robotlearning123/gpt2agent",
                "--workflow",
                str(workflow),
                "--action-directory",
                str(ACTION_DIRECTORY),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert result.stdout == ""
        assert "release workflow differs from the reviewed exact bytes" in result.stderr
