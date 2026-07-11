"""Complete-tree trust contract for the account-gate CPython runtime."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HASHER = PROJECT_ROOT / "scripts" / "hash_runtime_tree.sh"


def _run(tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HASHER), str(tree.resolve())],
        capture_output=True,
        text=True,
        check=False,
    )


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "cpython"
    (tree / "bin").mkdir(parents=True, mode=0o700)
    (tree / "lib" / "python3.12").mkdir(parents=True, mode=0o700)
    tree.chmod(0o700)
    (tree / "lib").chmod(0o700)
    python = tree / "bin" / "python3.12"
    python.write_bytes(b"synthetic-cpython")
    python.chmod(0o700)
    stdlib = tree / "lib" / "python3.12" / "json.py"
    stdlib.write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    stdlib.chmod(0o600)
    return tree


def test_runtime_tree_hash_binds_every_file_and_internal_symlink(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    os.symlink("python3.12", tree / "bin" / "python3")

    first = _run(tree)
    assert first.returncode == 0, first.stderr
    assert len(first.stdout.strip()) == 64

    (tree / "lib" / "python3.12" / "json.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    second = _run(tree)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() != first.stdout.strip()


def test_runtime_tree_hash_rejects_external_symlink(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    os.symlink("/etc/passwd", tree / "lib" / "python3.12" / "escape")

    result = _run(tree)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "symlink escapes" in result.stderr


def test_runtime_tree_hash_rejects_writable_runtime_entry(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    target = tree / "lib" / "python3.12" / "json.py"
    target.chmod(0o666)

    result = _run(tree)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "group- or world-writable" in result.stderr


def test_runtime_tree_hash_rejects_unreadable_subtree_without_partial_digest(
    tmp_path: Path,
) -> None:
    tree = _tree(tmp_path)
    hidden = tree / "lib" / "python3.12" / "hidden"
    hidden.mkdir(mode=0o700)
    (hidden / "hidden.py").write_text("SECRET = True\n", encoding="utf-8")
    hidden.chmod(0o000)
    try:
        result = _run(tree)
    finally:
        hidden.chmod(0o700)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "traversal failed" in result.stderr
