"""Regression tests for deterministic source-distribution metadata."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NORMALIZER = PROJECT_ROOT / "scripts" / "normalize_sdist.py"


def _load_normalizer():
    spec = importlib.util.spec_from_file_location("normalize_sdist", NORMALIZER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_source_archive(
    path: Path,
    *,
    gzip_mtime: int,
    member_mtime: float,
    unsafe_symlink: bool = False,
    regular_mode: int = 0o644,
    executable_mode: int = 0o755,
) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename=f"nondeterministic-{gzip_mtime}.tar",
            mode="wb",
            fileobj=raw,
            mtime=gzip_mtime,
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                root = tarfile.TarInfo("gpt2agent-1.2.3")
                root.type = tarfile.DIRTYPE
                root.mode = 0o755
                root.mtime = member_mtime
                root.pax_headers = {"mtime": str(member_mtime)}
                archive.addfile(root)

                payload = b"[project]\nname = 'gpt2agent'\n"
                metadata = tarfile.TarInfo("gpt2agent-1.2.3/pyproject.toml")
                metadata.mode = regular_mode
                metadata.mtime = member_mtime + 0.25
                metadata.size = len(payload)
                metadata.pax_headers = {"mtime": str(member_mtime + 0.25)}
                archive.addfile(metadata, io.BytesIO(payload))

                executable_payload = b"#!/bin/sh\nexit 0\n"
                executable = tarfile.TarInfo("gpt2agent-1.2.3/install.sh")
                executable.mode = executable_mode
                executable.mtime = member_mtime + 0.5
                executable.size = len(executable_payload)
                executable.pax_headers = {"mtime": str(member_mtime + 0.5)}
                archive.addfile(executable, io.BytesIO(executable_payload))

                if unsafe_symlink:
                    link = tarfile.TarInfo("gpt2agent-1.2.3/unsafe-link")
                    link.type = tarfile.SYMTYPE
                    link.linkname = "../../outside"
                    link.mode = 0o777
                    link.mtime = member_mtime
                    archive.addfile(link)


def _write_custom_archive(
    path: Path,
    members: list[tuple[tarfile.TarInfo, bytes | None]],
) -> None:
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for member, payload in members:
            if payload is not None:
                member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload) if payload is not None else None)


def _directory(name: str) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o755
    return member


def _file(name: str, payload: bytes = b"payload") -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.mode = 0o644
    return member, payload


def test_normalizer_makes_metadata_distinct_archives_byte_identical(tmp_path: Path) -> None:
    normalizer = _load_normalizer()
    first = tmp_path / "first" / "gpt2agent-1.2.3.tar.gz"
    second = tmp_path / "second" / "gpt2agent-1.2.3.tar.gz"
    first.parent.mkdir()
    second.parent.mkdir()
    _write_source_archive(first, gzip_mtime=1_700_000_001, member_mtime=1_700_000_010.25)
    _write_source_archive(
        second,
        gzip_mtime=1_700_000_099,
        member_mtime=1_700_000_200.75,
        regular_mode=0o600,
        executable_mode=0o700,
    )

    normalizer.normalize_sdist(first, epoch=1_700_000_000)
    normalizer.normalize_sdist(second, epoch=1_700_000_000)

    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes()[3] & 0x08 == 0  # gzip FNAME is absent
    assert int.from_bytes(first.read_bytes()[4:8], "little") == 1_700_000_000

    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [
            "gpt2agent-1.2.3",
            "gpt2agent-1.2.3/pyproject.toml",
            "gpt2agent-1.2.3/install.sh",
        ]
        assert [member.mode for member in members] == [0o755, 0o644, 0o755]
        assert all(member.mtime == 1_700_000_000 for member in members)
        assert all(member.uid == 0 and member.gid == 0 for member in members)
        assert all(member.uname == "" and member.gname == "" for member in members)
        assert all("mtime" not in member.pax_headers for member in members)
        extracted = archive.extractfile("gpt2agent-1.2.3/pyproject.toml")
        assert extracted is not None
        assert extracted.read() == b"[project]\nname = 'gpt2agent'\n"

    normalized = first.read_bytes()
    normalizer.normalize_sdist(first, epoch=1_700_000_000)
    assert first.read_bytes() == normalized


def test_normalizer_rejects_links_without_replacing_the_input(tmp_path: Path) -> None:
    normalizer = _load_normalizer()
    archive = tmp_path / "gpt2agent-1.2.3.tar.gz"
    _write_source_archive(
        archive,
        gzip_mtime=1_700_000_001,
        member_mtime=1_700_000_010.25,
        unsafe_symlink=True,
    )
    original = archive.read_bytes()

    with pytest.raises(ValueError, match="regular files and directories"):
        normalizer.normalize_sdist(archive, epoch=1_700_000_000)

    assert archive.read_bytes() == original
    assert list(tmp_path.glob(".gpt2agent-1.2.3.tar.gz.*.tmp")) == []


def test_normalizer_replace_failure_is_atomic_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalizer = _load_normalizer()
    archive = tmp_path / "gpt2agent-1.2.3.tar.gz"
    _write_source_archive(
        archive,
        gzip_mtime=1_700_000_001,
        member_mtime=1_700_000_010.25,
    )
    original = archive.read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(normalizer.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        normalizer.normalize_sdist(archive, epoch=1_700_000_000)

    assert archive.read_bytes() == original
    assert list(tmp_path.glob(".gpt2agent-1.2.3.tar.gz.*.tmp")) == []


def test_normalizer_rejects_a_symlink_archive(tmp_path: Path) -> None:
    normalizer = _load_normalizer()
    target = tmp_path / "target.tar.gz"
    target.write_bytes(b"not an archive")
    archive = tmp_path / "gpt2agent-1.2.3.tar.gz"
    archive.symlink_to(target)

    with pytest.raises(ValueError, match="regular non-symlink"):
        normalizer.normalize_sdist(archive, epoch=1_700_000_000)


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "/absolute",
        "gpt2agent-1.2.3/../escape",
        "gpt2agent-1.2.3\\windows-path",
        "different-root/file",
    ],
)
def test_normalizer_rejects_noncanonical_or_mismatched_paths(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    normalizer = _load_normalizer()
    archive = tmp_path / "gpt2agent-1.2.3.tar.gz"
    _write_custom_archive(
        archive,
        [
            (_directory("gpt2agent-1.2.3"), None),
            _file(unsafe_name),
        ],
    )

    with pytest.raises(ValueError, match="path|root"):
        normalizer.normalize_sdist(archive, epoch=1_700_000_000)


def test_normalizer_rejects_duplicate_members(tmp_path: Path) -> None:
    normalizer = _load_normalizer()
    archive = tmp_path / "gpt2agent-1.2.3.tar.gz"
    duplicate = "gpt2agent-1.2.3/duplicate.txt"
    _write_custom_archive(
        archive,
        [
            (_directory("gpt2agent-1.2.3"), None),
            _file(duplicate, b"first"),
            _file(duplicate, b"second"),
        ],
    )

    with pytest.raises(ValueError, match="duplicate member"):
        normalizer.normalize_sdist(archive, epoch=1_700_000_000)


@pytest.mark.parametrize(
    "member_type",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE],
)
def test_normalizer_rejects_every_special_member_type(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    normalizer = _load_normalizer()
    archive = tmp_path / "gpt2agent-1.2.3.tar.gz"
    special = tarfile.TarInfo("gpt2agent-1.2.3/special")
    special.type = member_type
    special.linkname = "gpt2agent-1.2.3/target"
    _write_custom_archive(
        archive,
        [
            (_directory("gpt2agent-1.2.3"), None),
            (special, None),
        ],
    )

    with pytest.raises(ValueError, match="regular files and directories"):
        normalizer.normalize_sdist(archive, epoch=1_700_000_000)


def test_normalizer_preserves_a_long_pax_path_without_volatile_metadata(
    tmp_path: Path,
) -> None:
    normalizer = _load_normalizer()
    archive = tmp_path / "gpt2agent-1.2.3.tar.gz"
    long_name = "gpt2agent-1.2.3/" + "a" * 120
    _write_custom_archive(
        archive,
        [
            (_directory("gpt2agent-1.2.3"), None),
            _file(long_name),
        ],
    )

    normalizer.normalize_sdist(archive, epoch=1_700_000_000)

    with tarfile.open(archive, "r:gz") as normalized:
        member = normalized.getmember(long_name)
        assert member.pax_headers == {"path": long_name}
        assert member.mtime == 1_700_000_000


@pytest.mark.parametrize("epoch", [-1, 2**32, True, "1700000000"])
def test_normalizer_rejects_noncanonical_gzip_epochs(epoch: object) -> None:
    normalizer = _load_normalizer()

    with pytest.raises((TypeError, ValueError), match="epoch"):
        normalizer.validate_epoch(epoch)


def test_normalizer_accepts_gzip_epoch_boundaries() -> None:
    normalizer = _load_normalizer()

    assert normalizer.validate_epoch(0) == 0
    assert normalizer.validate_epoch(2**32 - 1) == 2**32 - 1
