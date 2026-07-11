"""Safe extraction contract for the pinned CPython account-gate runtime."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest

from scripts.extract_trusted_python import (
    ArchivePolicy,
    PINNED_POLICY,
    RuntimeArchiveError,
    _tree_digest,
    install_reviewed_runtime,
)


def _add_file(archive: tarfile.TarFile, name: str, body: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.size = len(body)
    archive.addfile(info, io.BytesIO(body))


def _add_symlink(archive: tarfile.TarFile, name: str, target: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    archive.addfile(info)


def _fixture_archive(tmp_path: Path) -> tuple[Path, ArchivePolicy]:
    archive_path = tmp_path / "runtime.tar.gz"
    python_body = b"synthetic-cpython-3.12.13"
    stdlib_body = b"VALUE = 1\n"
    with tarfile.open(archive_path, "w:gz") as archive:
        _add_file(archive, "python/bin/python3.12", python_body, 0o755)
        _add_file(archive, "python/lib/python3.12/json.py", stdlib_body, 0o666)
        _add_symlink(archive, "python/bin/python3", "python3.12")
    archive_path.chmod(0o600)

    expected = tmp_path / "expected"
    (expected / "bin").mkdir(parents=True, mode=0o700)
    (expected / "lib" / "python3.12").mkdir(parents=True, mode=0o700)
    expected.chmod(0o700)
    (expected / "lib").chmod(0o700)
    python = expected / "bin" / "python3.12"
    python.write_bytes(python_body)
    python.chmod(0o700)
    stdlib = expected / "lib" / "python3.12" / "json.py"
    stdlib.write_bytes(stdlib_body)
    stdlib.chmod(0o600)
    os.symlink("python3.12", expected / "bin" / "python3")

    symlink_map = hashlib.sha256(
        b"python/bin/python3\0python3.12\0"
    ).hexdigest()
    policy = ArchivePolicy(
        source_url="https://example.invalid/reviewed-runtime.tar.gz",
        archive_size=archive_path.stat().st_size,
        archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        executable_sha256=hashlib.sha256(python_body).hexdigest(),
        runtime_tree_sha256=_tree_digest(expected),
        symlink_map_sha256=symlink_map,
        regular_files=2,
        symlinks=1,
        directories=0,
    )
    return archive_path, policy


def test_installer_normalizes_and_authenticates_complete_runtime(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    archive, policy = _fixture_archive(tmp_path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    destination = private / "runtime"

    install_reviewed_runtime(archive, destination, policy)

    assert _tree_digest(destination) == policy.runtime_tree_sha256
    assert (destination / "bin" / "python3.12").stat().st_mode & 0o777 == 0o700
    assert (destination / "lib" / "python3.12" / "json.py").stat().st_mode & 0o777 == 0o600
    assert os.readlink(destination / "bin" / "python3") == "python3.12"
    assert not (private / ".reviewed-cpython-runtime.tar.gz").exists()


def test_installer_rejects_archive_byte_change_and_cleans_snapshot(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    archive, policy = _fixture_archive(tmp_path)
    with archive.open("ab") as stream:
        stream.write(b"tamper")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    destination = private / "runtime"

    with pytest.raises(RuntimeArchiveError, match="size does not match"):
        install_reviewed_runtime(archive, destination, policy)

    assert not destination.exists()
    assert not (private / ".reviewed-cpython-runtime.tar.gz").exists()


def test_installer_rejects_escape_before_extraction(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    archive, policy = _fixture_archive(tmp_path)
    with tarfile.open(archive, "w:gz") as writer:
        _add_file(writer, "python/../escape", b"escape", 0o644)
    archive.chmod(0o600)
    malicious_policy = ArchivePolicy(
        **{
            **policy.__dict__,
            "archive_size": archive.stat().st_size,
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "regular_files": 1,
            "symlinks": 0,
        }
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)

    with pytest.raises(RuntimeArchiveError, match="not canonical"):
        install_reviewed_runtime(archive, private / "runtime", malicious_policy)

    assert not (tmp_path / "escape").exists()
    assert not (private / "runtime").exists()


def test_installer_rejects_writable_destination_ancestor(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    archive, policy = _fixture_archive(tmp_path)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    private = unsafe / "private"
    private.mkdir(mode=0o700)
    unsafe.chmod(0o777)

    with pytest.raises(RuntimeArchiveError, match="ancestry is writable"):
        install_reviewed_runtime(archive, private / "runtime", policy)

    assert not (private / "runtime").exists()


def test_installer_rejects_symlinked_destination_parent(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    archive, policy = _fixture_archive(tmp_path)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)

    with pytest.raises(RuntimeArchiveError, match="path is not canonical"):
        install_reviewed_runtime(archive, alias / "runtime", policy)

    assert not (private / "runtime").exists()


def test_pinned_policy_names_exact_reviewed_astral_asset() -> None:
    assert "astral-sh/python-build-standalone" in PINNED_POLICY.source_url
    assert "20260510" in PINNED_POLICY.source_url
    assert PINNED_POLICY.archive_size == 111_027_266
    assert PINNED_POLICY.archive_sha256 == (
        "e7332b4b4bb85006deb48d251c786a04c14de104c9b3a006b33457a4a604b8bc"
    )
    assert PINNED_POLICY.executable_sha256 == (
        "f7014f68e3c8f180811740735cf1dd5c28be6cff84db11d0ced2a8cd039670a0"
    )
    assert PINNED_POLICY.runtime_tree_sha256 == (
        "d3a6bd32b73612fce20dbfe1eebd33f2b6ebd1b42b13aa8b1fd1549065be2cc0"
    )
