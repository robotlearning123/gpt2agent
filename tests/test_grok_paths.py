from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gpt2agent.errors import InputValidationError
from gpt2agent.grok_paths import RootPolicy


_CWD_INVARIANT = (
    "grok_build.cwd must be an existing directory under a configured root"
)


def _assert_cwd_rejected(policy: RootPolicy, value: str | Path | None) -> None:
    with pytest.raises(InputValidationError) as caught:
        policy.directory(value)

    assert caught.value.invariant == _CWD_INVARIANT


def test_root_policy_defaults_to_disabled(tmp_path: Path) -> None:
    _assert_cwd_rejected(RootPolicy(()), tmp_path)


def test_root_policy_rejects_non_path_values_with_the_fixed_invariant(
    tmp_path: Path,
) -> None:
    _assert_cwd_rejected(RootPolicy((tmp_path,)), 42)  # type: ignore[arg-type]


def test_root_policy_returns_canonical_contained_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "source" / "package"
    nested.mkdir(parents=True)

    result = RootPolicy((root,)).directory(root / "source" / ".." / "source" / "package")

    assert result == nested.resolve()
    assert result.is_absolute()


def test_root_policy_uses_process_cwd_only_when_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    nested = root / "repo"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert RootPolicy((root,)).directory(None) == nested.resolve()

    monkeypatch.chdir(tmp_path)
    _assert_cwd_rejected(RootPolicy((root,)), None)


@pytest.mark.parametrize("kind", ["missing", "file", "parent_traversal"])
def test_root_policy_rejects_non_directories_and_escaped_paths(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    if kind == "missing":
        candidate = root / "missing"
    elif kind == "file":
        candidate = root / "file.txt"
        candidate.write_text("planted")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        candidate = root / ".." / "outside"

    _assert_cwd_rejected(RootPolicy((root,)), candidate)


@pytest.mark.parametrize("symlink_location", ["parent", "leaf"])
def test_root_policy_rejects_parent_and_leaf_symlink_escapes(
    tmp_path: Path, symlink_location: str
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    if symlink_location == "parent":
        link = root / "linked-parent"
        link.symlink_to(outside, target_is_directory=True)
        candidate = link / "child"
        (outside / "child").mkdir()
    else:
        link = root / "linked-leaf"
        link.symlink_to(outside, target_is_directory=True)
        candidate = link

    _assert_cwd_rejected(RootPolicy((root,)), candidate)


def test_root_policy_fails_closed_when_root_disappears_after_configuration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    nested = root / "repo"
    nested.mkdir(parents=True)
    policy = RootPolicy((root,))
    shutil.rmtree(root)

    _assert_cwd_rejected(policy, nested)
