"""Regression tests for local config and bearer-token persistence."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest


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
