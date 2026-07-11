"""Security contract for the end-to-end trusted account release operator."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="the trusted account release operator is Linux-only by contract",
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPERATOR = PROJECT_ROOT / "scripts" / "run_account_release.sh"


def test_operator_starts_privileged_and_reexecutes_with_a_closed_environment() -> None:
    source = OPERATOR.read_text(encoding="utf-8")

    assert source.startswith("#!/usr/bin/bash -p\n")
    reexec = source.index("exec /usr/bin/env -i")
    argument_parsing = source.index("while (($#)); do")
    assert reexec < argument_parsing
    assert "/usr/bin/bash -p \"$0\" \"$@\"" in source
    assert "/usr/bin/bash -p \"$CHECKOUT/scripts/bootstrap_account_gate.sh\"" in source
    assert "/usr/bin/bash -p \"$CHECKOUT/scripts/create_release_tag.sh\"" in source
    assert "run_git" in source
    assert "run_gh" in source
    assert "run_python_account" in source
    assert "--trusted-python-archive" in source
    assert "--trusted-python-base" not in source
    assert "--trusted-python-sha256" not in source
    assert "--trusted-python-tree-sha256" not in source
    assert "install_account_gate_runtime.sh" in source
    assert "extract_trusted_python.py" not in source
    assert "/usr/bin/python3.12" not in source
    assert '"$CHECKOUT/scripts/hash_runtime_tree.sh"' in source
    assert "9544d2a29138833e6177d45dbc57468d37710b5080c901fbb579d53f251cdd6f" in source
    assert "7df598dcc28ad5583fd65f49da6a2ff6460030441070d5c7a105df7dd5294f79" in source
    assert "20260510" not in source
    assert '"$GH_BIN" --repo "$REPOSITORY" "$@"' not in source
    assert "local status=0" in source


def test_operator_preflights_pypi_and_publishes_evidence_only_after_tag() -> None:
    source = OPERATOR.read_text(encoding="utf-8")

    pypi_check = source.index('"$CHECKOUT/scripts/verify_pypi_artifacts.py"')
    receipt_create = source.index('"$CHECKOUT/scripts/verify_account_receipt.py" create')
    tag_create = source.index('"$CHECKOUT/scripts/create_release_tag.sh"')
    publish_dist = source.index('/usr/bin/mv -T -- "$STAGED_DIST" "$DIST"')
    assert pypi_check < receipt_create < publish_dist < tag_create
    assert "--require-absent" in source[pypi_check:receipt_create]
    assert '"$EVIDENCE_DIRECTORY/.gpt2agent-release-evidence.XXXXXXXX"' in source
    assert "DIST_MOVED=1" in source
    assert "RECEIPT_MOVED=1" in source
    assert '--irreversible-state-file "$IRREVERSIBLE_STATE_FILE"' in source
    assert (
        'IRREVERSIBLE_STATE_FILE="$EVIDENCE_DIRECTORY/'
        'gpt2agent-$TAG-$COMMIT.irreversible-ref-state"' in source
    )
    assert 'IRREVERSIBLE_STATE_FILE="$RUNTIME_ROOT/' not in source
    assert "preserve_evidence=1" in source
    assert "IRREVERSIBLE POST-REF STATE: preserving release evidence" in source


def test_operator_cleanup_preserves_durable_ref_marker_and_evidence(
    tmp_path: Path,
) -> None:
    source = OPERATOR.read_text(encoding="utf-8")
    function_start = source.index("cleanup_release_runtime() {")
    function_end = source.index("trap cleanup_release_runtime EXIT", function_start)
    cleanup_function = source[function_start:function_end]

    operator_home = tmp_path / "home"
    evidence_directory = operator_home / "evidence"
    runtime_root = operator_home / ".gpt2agent-account-release.fixture"
    dist = evidence_directory / "dist"
    receipt = evidence_directory / "receipt.json"
    marker = evidence_directory / "irreversible-ref-state"
    for directory in (operator_home, evidence_directory, runtime_root, dist):
        directory.mkdir(mode=0o700)
    receipt.write_text("{}\n", encoding="utf-8")
    marker.write_text("tag=v0.0.12\n", encoding="utf-8")
    harness = tmp_path / "cleanup-harness.sh"
    harness.write_text(
        "#!/usr/bin/bash -p\n"
        "set -euo pipefail\n"
        f"OPERATOR_HOME={shlex.quote(str(operator_home))}\n"
        f"EVIDENCE_DIRECTORY={shlex.quote(str(evidence_directory))}\n"
        f"RUNTIME_ROOT={shlex.quote(str(runtime_root))}\n"
        f"DIST={shlex.quote(str(dist))}\n"
        f"RECEIPT={shlex.quote(str(receipt))}\n"
        f"IRREVERSIBLE_STATE_FILE={shlex.quote(str(marker))}\n"
        "WORKTREE_CREATED=0\n"
        "DIST_MOVED=1\n"
        "RECEIPT_MOVED=1\n"
        "EVIDENCE_STAGE=\n"
        "CHECKOUT=unused\n"
        "FETCH_REPOSITORY=unused\n"
        "run_git() { :; }\n"
        f"{cleanup_function}\n"
        "cleanup_release_runtime\n",
        encoding="utf-8",
    )
    harness.chmod(0o700)

    result = subprocess.run(
        [str(harness)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not runtime_root.exists()
    assert marker.read_text(encoding="utf-8") == "tag=v0.0.12\n"
    assert receipt.is_file()
    assert dist.is_dir()
    assert "IRREVERSIBLE POST-REF STATE" in result.stderr


def test_operator_ignores_bash_startup_environment(tmp_path: Path) -> None:
    marker = tmp_path / "bash-env-ran"
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(
        f": > {shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["BASH_ENV"] = str(bash_env)

    result = subprocess.run(
        [str(OPERATOR)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert not marker.exists()


def test_readme_delegates_account_release_to_the_reviewed_operator() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "scripts/run_account_release.sh" in readme
    assert "verify_account_receipt.py create" not in readme


def test_operator_rejects_untrusted_gh_before_execution(tmp_path: Path) -> None:
    marker = tmp_path / "untrusted-gh-ran"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(marker))}\n"
        "exit 91\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)

    operator_home = tmp_path / "home"
    codex_home = operator_home / ".codex"
    evidence = operator_home / "evidence"
    for directory in (operator_home, codex_home, evidence):
        directory.mkdir(mode=0o700)
    runtime_archive = operator_home / "runtime.tar.gz"
    runtime_archive.write_bytes(b"not-the-reviewed-runtime")
    runtime_archive.chmod(0o600)
    policy = operator_home / "policy.json"
    policy.write_text("{}\n", encoding="utf-8")
    policy.chmod(0o600)

    result = subprocess.run(
        [
            str(OPERATOR),
            "--repository",
            "owner/repository",
            "--pr",
            "1",
            "--operator-home",
            str(operator_home),
            "--codex-home",
            str(codex_home),
            "--evidence-directory",
            str(evidence),
            "--trusted-python-archive",
            str(runtime_archive),
            "--governance-policy",
            str(policy),
            "--gh",
            str(fake_gh),
            "--git",
            "/usr/bin/git",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists()
