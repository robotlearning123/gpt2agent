"""Regression tests for local config and bearer-token persistence."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("path_kind", ["bare", "absolute-cwd", "traversal", "root"])
def test_private_json_rejects_paths_without_a_dedicated_parent(
    path_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpt2agent import _secure_file

    path = Path("auth.json")
    if path_kind == "absolute-cwd":
        path = Path.cwd() / "auth.json"
    elif path_kind == "traversal":
        path = Path("nested") / ".." / "auth.json"
    elif path_kind == "root":
        path = Path(Path.cwd().anchor or "/") / "auth.json"

    monkeypatch.setattr(
        _secure_file,
        "_open_private_directory",
        lambda parent: pytest.fail(f"must not open or chmod unsafe parent {parent}"),
    )

    with pytest.raises(ValueError, match="dedicated non-root parent"):
        _secure_file.write_private_json(path, {"access_token": "synthetic"})


def test_private_json_preserves_valid_nested_relative_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpt2agent._secure_file import write_private_json

    monkeypatch.chdir(tmp_path)
    destination = Path("private") / "auth.json"

    write_private_json(destination, {"access_token": "synthetic"})

    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "access_token": "synthetic"
    }
    if os.name != "nt":
        assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def _save_token(kind: str, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(home))
    token_path = home / ".gpt2agent" / "token.json"

    if kind == "setup":
        from gpt2agent import setup

        setup.save_token("secret-bearer")
    else:
        from gpt2agent import auth

        monkeypatch.setattr(auth, "_from_codex", lambda: None)
        monkeypatch.setattr(auth, "_from_saved", lambda: None)
        monkeypatch.setattr(auth, "_from_browser_use", lambda: None)
        monkeypatch.setattr(
            auth,
            "_from_browser",
            lambda: {"access_token": "secret-bearer", "source": "browser"},
        )
        assert auth.get_token(interactive=True) == "secret-bearer"

    return token_path


def test_backup_atomically_replaces_attacker_symlink(tmp_path: Path) -> None:
    from gpt2agent.install import _backup

    config = tmp_path / "config.json"
    config.write_text('{"secret": "config"}')
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch")
    backup = config.with_name(config.name + ".bak-gpt2agent")
    backup.symlink_to(victim)

    result = _backup(config)

    assert result == backup
    assert victim.read_text() == "do not touch"
    assert not backup.is_symlink()
    assert backup.read_text() == '{"secret": "config"}'


def test_backup_binds_symlink_content_and_mode_to_one_open_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gpt2agent.install import _backup

    private = tmp_path / "private.json"
    private.write_text("TOP-SECRET", encoding="utf-8")
    private.chmod(0o600)
    public = tmp_path / "public.json"
    public.write_text("public", encoding="utf-8")
    public.chmod(0o644)
    config = tmp_path / "config.json"
    config.symlink_to(private)
    original_read_bytes = Path.read_bytes
    original_open = os.open
    swapped = False

    def swap_config_target() -> None:
        nonlocal swapped
        if not swapped:
            config.unlink()
            config.symlink_to(public)
            swapped = True

    def read_then_swap(path: Path) -> bytes:
        content = original_read_bytes(path)
        if path == config:
            swap_config_target()
        return content

    def open_then_swap(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            descriptor = original_open(path, flags, mode)
        else:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if Path(path) == config:
            swap_config_target()
        return descriptor

    monkeypatch.setattr(Path, "read_bytes", read_then_swap)
    monkeypatch.setattr(os, "open", open_then_swap)

    backup = _backup(config)

    assert backup is not None
    assert original_read_bytes(backup) == b"TOP-SECRET"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_backup_rejects_non_regular_source(tmp_path: Path) -> None:
    from gpt2agent.install import _backup

    config = tmp_path / "config.json"
    config.mkdir()

    with pytest.raises(RuntimeError, match="must resolve to a regular file"):
        _backup(config)

    assert not config.with_name(config.name + ".bak-gpt2agent").exists()


def test_backup_is_private_even_when_source_is_group_or_world_readable(tmp_path: Path) -> None:
    from gpt2agent.install import _backup

    config = tmp_path / "config.json"
    config.write_text('{"secret": "config"}', encoding="utf-8")
    config.chmod(0o644)

    backup = _backup(config)

    assert backup is not None
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_backup_acl_cannot_widen_effective_group_access(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX ACLs are unavailable")
    setfacl = shutil.which("setfacl")
    getfacl = shutil.which("getfacl")
    if setfacl is None or getfacl is None:
        pytest.skip("setfacl/getfacl are unavailable")

    import grp

    from gpt2agent.install import _backup

    config = tmp_path / "config.json"
    config.write_text('{"secret": "config"}', encoding="utf-8")
    config.chmod(0o600)
    source_gid = config.stat().st_gid
    named_group = next(
        (group.gr_gid for group in grp.getgrall() if group.gr_gid != source_gid), None
    )
    if named_group is None:
        pytest.skip("no distinct group is available for an ACL entry")

    subprocess.run(
        [setfacl, "-m", f"g::---,g:{named_group}:r--,m::r--", str(config)],
        check=True,
        capture_output=True,
        text=True,
    )
    source_acl = subprocess.run(
        [getfacl, "-cpn", str(config)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"group:{named_group}:r--" in source_acl
    assert "group::---" in source_acl

    backup = _backup(config)

    assert backup is not None
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    backup_acl = subprocess.run(
        [getfacl, "-cpn", str(backup)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert all(
        line.endswith("---")
        for line in backup_acl.splitlines()
        if line.startswith(("group:", "mask:"))
    )


@pytest.mark.parametrize(
    ("installer_name", "original_content", "replacement_content"),
    [
        ("install_claude_code", '{"original": true}\n', '{"replacement": true}\n'),
        ("install_codex", "original = true\n", "replacement = true\n"),
        ("install_cursor", '{"original": true}\n', '{"replacement": true}\n'),
    ],
)
def test_config_install_aborts_if_symlink_target_changes_after_backup(
    installer_name: str,
    original_content: str,
    replacement_content: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gpt2agent import install

    original = tmp_path / "original-config"
    original.write_text(original_content, encoding="utf-8")
    replacement = tmp_path / "replacement-config"
    replacement.write_text(replacement_content, encoding="utf-8")
    config = tmp_path / "config"
    config.symlink_to(original)
    backup = config.with_name(config.name + ".bak-gpt2agent")
    real_atomic_replace = install.atomic_replace_bytes
    swapped = False

    def replace_then_swap(path: Path, content: bytes, **kwargs) -> None:
        nonlocal swapped
        real_atomic_replace(path, content, **kwargs)
        if Path(path) == backup and not swapped:
            config.unlink()
            config.symlink_to(replacement)
            swapped = True

    monkeypatch.setattr(install, "atomic_replace_bytes", replace_then_swap)

    with pytest.raises(RuntimeError, match="changed during installation"):
        getattr(install, installer_name)(config_path=config)

    assert swapped
    assert original.read_text(encoding="utf-8") == original_content
    assert replacement.read_text(encoding="utf-8") == replacement_content
    assert backup.read_text(encoding="utf-8") == original_content
    assert not list(tmp_path.glob(".original-config.tmp-*"))
    assert "wrote" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("installer_name", "original_content"),
    [
        ("install_claude_code", '{"original": true}\n'),
        ("install_codex", "original = true\n"),
        ("install_cursor", '{"original": true}\n'),
    ],
)
def test_config_install_preserves_stable_symlink(
    installer_name: str,
    original_content: str,
    tmp_path: Path,
) -> None:
    from gpt2agent import install

    target = tmp_path / "target-config"
    target.write_text(original_content, encoding="utf-8")
    config = tmp_path / "config"
    config.symlink_to(target)

    result = getattr(install, installer_name)(config_path=config)

    assert result["changed"] is True
    assert config.is_symlink()
    assert "gpt2agent" in target.read_text(encoding="utf-8")
    assert "original" in target.read_text(encoding="utf-8")


def test_config_install_aborts_if_stable_target_inode_changes_after_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpt2agent import install

    original_content = '{"original": true}\n'
    replacement_content = '{"replacement": true}\n'
    target = tmp_path / "target-config"
    target.write_text(original_content, encoding="utf-8")
    replacement = tmp_path / "replacement-config"
    replacement.write_text(replacement_content, encoding="utf-8")
    config = tmp_path / "config"
    config.symlink_to(target)
    backup = config.with_name(config.name + ".bak-gpt2agent")
    real_atomic_replace = install.atomic_replace_bytes

    def replace_then_change_inode(path: Path, content: bytes, **kwargs) -> None:
        real_atomic_replace(path, content, **kwargs)
        if Path(path) == backup:
            os.replace(replacement, target)

    monkeypatch.setattr(install, "atomic_replace_bytes", replace_then_change_inode)

    with pytest.raises(RuntimeError, match="changed during installation"):
        install.install_claude_code(config_path=config)

    assert config.is_symlink()
    assert target.read_text(encoding="utf-8") == replacement_content
    assert backup.read_text(encoding="utf-8") == original_content
    assert not list(tmp_path.glob(".target-config.tmp-*"))


def test_setup_config_aborts_if_symlink_target_changes_after_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gpt2agent import install, setup

    original_content = "# original\n"
    replacement_content = "# replacement\n"
    original = tmp_path / "original-config"
    original.write_text(original_content, encoding="utf-8")
    replacement = tmp_path / "replacement-config"
    replacement.write_text(replacement_content, encoding="utf-8")
    config = tmp_path / "config"
    config.symlink_to(original)
    backup = config.with_name(config.name + ".bak-gpt2agent")
    monkeypatch.setattr(setup, "MCP_CONFIG_PATH", config)
    real_atomic_replace = install.atomic_replace_bytes

    def replace_then_swap(path: Path, content: bytes, **kwargs) -> None:
        real_atomic_replace(path, content, **kwargs)
        if Path(path) == backup:
            config.unlink()
            config.symlink_to(replacement)

    monkeypatch.setattr(install, "atomic_replace_bytes", replace_then_swap)

    with pytest.raises(RuntimeError, match="changed during installation"):
        setup.write_mcp_config("pro")

    assert original.read_text(encoding="utf-8") == original_content
    assert replacement.read_text(encoding="utf-8") == replacement_content
    assert backup.read_text(encoding="utf-8") == original_content


def test_atomic_write_does_not_follow_predictable_temp_symlink(tmp_path: Path) -> None:
    from gpt2agent.install import _atomic_write

    config = tmp_path / "config.toml"
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch")
    planted = config.with_name(config.name + f".tmp-{os.getpid()}")
    planted.symlink_to(victim)

    _atomic_write(config, "new = true\n")

    assert config.read_text() == "new = true\n"
    assert victim.read_text() == "do not touch"
    assert planted.is_symlink(), "do not delete an unrelated attacker-created path"


def test_atomic_write_cleans_random_temp_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gpt2agent.install import _atomic_write

    config = tmp_path / "config.toml"
    config.write_text("old = true\n")

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        _atomic_write(config, "new = true\n")

    assert config.read_text() == "old = true\n"
    assert list(tmp_path.glob(".config.toml.tmp-*")) == []


@pytest.mark.parametrize("kind", ["setup", "auth"])
def test_token_save_rejects_symlink_directory(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".gpt2agent").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match=r"\.gpt2agent.*symbolic link"):
        _save_token(kind, home, monkeypatch)

    assert not (outside / "token.json").exists()


@pytest.mark.parametrize("kind", ["setup", "auth"])
def test_token_save_rejects_target_symlink_without_touching_victim(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    token_dir = home / ".gpt2agent"
    token_dir.mkdir(parents=True, mode=0o700)
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch")
    (token_dir / "token.json").symlink_to(victim)

    with pytest.raises(RuntimeError, match=r"token\.json.*symbolic link"):
        _save_token(kind, home, monkeypatch)

    assert victim.read_text() == "do not touch"


@pytest.mark.parametrize("kind", ["setup", "auth"])
def test_token_save_rejects_non_regular_target(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    token = home / ".gpt2agent" / "token.json"
    token.mkdir(parents=True, mode=0o700)

    with pytest.raises(RuntimeError, match=r"token\.json.*regular file"):
        _save_token(kind, home, monkeypatch)


@pytest.mark.parametrize("kind", ["setup", "auth"])
def test_token_save_enforces_private_directory_and_file_modes(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    token_dir = home / ".gpt2agent"
    token_dir.mkdir(parents=True, mode=0o777)
    token_dir.chmod(0o777)

    token = _save_token(kind, home, monkeypatch)

    assert stat.S_IMODE(token_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert json.loads(token.read_text())["access_token"] == "secret-bearer"


@pytest.mark.parametrize("kind", ["setup", "auth"])
def test_token_save_replaces_existing_hardlink_without_modifying_peer(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    token_dir = home / ".gpt2agent"
    token_dir.mkdir(parents=True, mode=0o700)
    peer = tmp_path / "peer.txt"
    peer.write_text("do not touch")
    token = token_dir / "token.json"
    os.link(peer, token)

    saved = _save_token(kind, home, monkeypatch)

    assert peer.read_text() == "do not touch"
    assert json.loads(saved.read_text())["access_token"] == "secret-bearer"
    assert saved.stat().st_ino != peer.stat().st_ino


@pytest.mark.parametrize("kind", ["setup", "auth"])
def test_token_save_cleans_temp_when_replace_fails(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        _save_token(kind, home, monkeypatch)

    token_dir = home / ".gpt2agent"
    assert not (token_dir / "token.json").exists()
    assert list(token_dir.glob(".token.json.tmp-*")) == []


def test_token_save_falls_back_when_directory_fds_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gpt2agent import _secure_file

    home = tmp_path / "home"
    home.mkdir()

    def no_directory_fd(_path: Path) -> int:
        raise NotImplementedError

    monkeypatch.setattr(_secure_file, "_open_private_directory", no_directory_fd)

    token = _save_token("setup", home, monkeypatch)

    assert json.loads(token.read_text())["access_token"] == "secret-bearer"
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
