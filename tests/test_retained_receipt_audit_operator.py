"""Security contract for the post-publication retained-receipt audit."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPERATOR = PROJECT_ROOT / "scripts" / "audit_retained_receipt.sh"


def test_audit_operator_uses_closed_environment_and_complete_runtime_tree() -> None:
    source = OPERATOR.read_text(encoding="utf-8")

    assert source.startswith("#!/bin/bash -p\n")
    assert source.index("exec /usr/bin/env -i") < source.index("while (($#)); do")
    assert "/bin/bash -p \"$TAGGED_HASHER\"" in source
    assert "--trusted-python-archive" in source
    assert "extract_trusted_python.py" in source
    assert "RUNTIME_TREE_SHA256" in source
    assert "/usr/bin/python3.12" in source
    assert "/usr/bin/git" in source
    assert "run_git" in source
    assert "run_python_clean" in source
    assert "tagged verifier digest is not pinned" in source
    assert "tagged verifier size is not pinned" in source
    assert "-(alpha|beta|rc)[0-9]+" in source


def test_audit_operator_pins_every_executed_tagged_verifier() -> None:
    source = OPERATOR.read_text(encoding="utf-8")
    pins = {
        "TAGGED_EXTRACTOR_SHA256": PROJECT_ROOT / "scripts" / "extract_trusted_python.py",
        "TAGGED_HASHER_SHA256": PROJECT_ROOT / "scripts" / "hash_runtime_tree.sh",
        "TAGGED_TAG_VERIFIER_SHA256": PROJECT_ROOT / "scripts" / "release_tag_metadata.py",
    }

    for variable, path in pins.items():
        match = re.search(rf"^{variable}=([0-9a-f]{{64}})$", source, re.MULTILINE)
        assert match is not None
        assert match.group(1) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_audit_operator_ignores_bash_startup_environment(tmp_path: Path) -> None:
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


def test_audit_operator_rejects_untrusted_git_before_execution(tmp_path: Path) -> None:
    marker = tmp_path / "untrusted-git-ran"
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        f": > {shlex.quote(str(marker))}\n"
        "exit 91\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o700)

    operator_home = tmp_path / "home"
    evidence = operator_home / "evidence"
    for directory in (operator_home, evidence):
        directory.mkdir(mode=0o700)
    archive = operator_home / "runtime.tar.gz"
    archive.write_bytes(b"not-the-reviewed-runtime")
    archive.chmod(0o600)

    result = subprocess.run(
        [
            str(OPERATOR),
            "--repository",
            "owner/repository",
            "--tag",
            "v0.0.12",
            "--operator-home",
            str(operator_home),
            "--evidence-directory",
            str(evidence),
            "--trusted-python-archive",
            str(archive),
            "--git",
            str(fake_git),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists()


def test_readme_delegates_retained_receipt_audit_to_operator() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "scripts/audit_retained_receipt.sh" in readme
    assert "verify-tag-object --tag-object-file" not in readme
