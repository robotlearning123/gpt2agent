#!/usr/bin/env python3
"""Normalize one setuptools source distribution into deterministic bytes."""

from __future__ import annotations

import argparse
import errno
import gzip
import hashlib
import os
import re
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Sequence


_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_MEMBER_BYTES = 256 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_MEMBERS = 10_000
_MAX_NAME_BYTES = 4_096
_COPY_CHUNK = 1024 * 1024
_VOLATILE_PAX_FIELDS = frozenset({"atime", "ctime", "mtime"})


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class _Member:
    name: str
    kind: str
    size: int
    sha256: str | None
    executable: bool


def validate_epoch(epoch: object) -> int:
    """Validate an epoch representable by the gzip header."""
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        raise TypeError("epoch must be an integer")
    if not 0 <= epoch <= 2**32 - 1:
        raise ValueError("epoch must be between 0 and 2**32 - 1")
    return epoch


def _identity(metadata: os.stat_result) -> _Identity:
    return _Identity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
    )


def _validate_archive_path(path: Path) -> tuple[str, _Identity]:
    if not path.name.endswith(".tar.gz"):
        raise ValueError("sdist path must end in .tar.gz")
    expected_root = path.name[: -len(".tar.gz")]
    if not expected_root:
        raise ValueError("sdist filename must contain a root name")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise ValueError("sdist must be an existing regular non-symlink file") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("sdist must be an existing regular non-symlink file")
    if not 0 < metadata.st_size <= _MAX_ARCHIVE_BYTES:
        raise ValueError("sdist compressed size is outside the supported bound")
    return expected_root, _identity(metadata)


def _open_regular(path: Path, expected: _Identity | None = None) -> tuple[BinaryIO, _Identity]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT}:
            raise ValueError("sdist must remain a regular non-symlink file") from None
        raise
    try:
        metadata = os.fstat(descriptor)
        observed = _identity(metadata)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("sdist must remain a regular non-symlink file")
        if not 0 < metadata.st_size <= _MAX_ARCHIVE_BYTES:
            raise ValueError("sdist compressed size is outside the supported bound")
        if expected is not None and observed != expected:
            raise ValueError("sdist changed while it was being normalized")
        return os.fdopen(descriptor, "rb"), observed
    except BaseException:
        os.close(descriptor)
        raise


def _validate_member_name(name: object, expected_root: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("sdist member name must be a non-empty string")
    if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
        raise ValueError("sdist member name exceeds the supported bound")
    if name.startswith("/") or "\\" in name:
        raise ValueError("sdist member path must be canonical and relative")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("sdist member path must be canonical and relative")
    if parts[0] != expected_root:
        raise ValueError("sdist members must use the filename-matched root directory")
    return name


def _validate_pax_headers(member: tarfile.TarInfo) -> None:
    for key, value in member.pax_headers.items():
        if key in _VOLATILE_PAX_FIELDS:
            continue
        if key == "path" and value == member.name:
            continue
        raise ValueError(f"sdist member {member.name!r} has unsupported PAX metadata")


def _hash_payload(archive: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"sdist member {member.name!r} has no readable payload")
    digest = hashlib.sha256()
    observed = 0
    while True:
        chunk = stream.read(_COPY_CHUNK)
        if not chunk:
            break
        observed += len(chunk)
        if observed > member.size:
            raise ValueError(f"sdist member {member.name!r} exceeds its declared size")
        digest.update(chunk)
    if observed != member.size:
        raise ValueError(f"sdist member {member.name!r} is truncated")
    return digest.hexdigest()


def _scan_archive(
    path: Path,
    expected_root: str,
    *,
    expected_identity: _Identity | None = None,
    normalized_epoch: int | None = None,
) -> tuple[_Identity, tuple[_Member, ...]]:
    raw, observed_identity = _open_regular(path, expected_identity)
    with raw:
        try:
            archive_context = tarfile.open(fileobj=raw, mode="r:gz")
        except (OSError, tarfile.TarError, EOFError):
            raise ValueError("sdist is not a valid gzip-compressed tar archive") from None
        with archive_context as archive:
            if archive.pax_headers:
                raise ValueError("sdist global PAX metadata is unsupported")
            try:
                members = archive.getmembers()
            except (OSError, tarfile.TarError, EOFError):
                raise ValueError("sdist member table is invalid") from None
            if not 0 < len(members) <= _MAX_MEMBERS:
                raise ValueError("sdist member count is outside the supported bound")

            names: set[str] = set()
            types: dict[str, str] = {}
            total_size = 0
            records: list[_Member] = []
            for member in members:
                name = _validate_member_name(member.name, expected_root)
                if name in names:
                    raise ValueError(f"sdist contains duplicate member {name!r}")
                names.add(name)
                _validate_pax_headers(member)
                if getattr(member, "issparse", lambda: False)():
                    raise ValueError("sdist permits only regular files and directories")
                if member.isdir():
                    if member.size != 0:
                        raise ValueError(f"sdist directory {name!r} has a payload")
                    kind = "directory"
                    digest = None
                    executable = True
                elif member.isreg():
                    if not 0 <= member.size <= _MAX_MEMBER_BYTES:
                        raise ValueError(f"sdist member {name!r} exceeds the supported bound")
                    total_size += member.size
                    if total_size > _MAX_TOTAL_BYTES:
                        raise ValueError("sdist payload exceeds the supported total bound")
                    kind = "file"
                    digest = _hash_payload(archive, member)
                    executable = bool(member.mode & 0o111)
                else:
                    raise ValueError("sdist permits only regular files and directories")

                if normalized_epoch is not None:
                    expected_mode = 0o755 if kind == "directory" or executable else 0o644
                    if (
                        member.uid != 0
                        or member.gid != 0
                        or member.uname != ""
                        or member.gname != ""
                        or member.mtime != normalized_epoch
                        or stat.S_IMODE(member.mode) != expected_mode
                        or any(
                            key in _VOLATILE_PAX_FIELDS for key in member.pax_headers
                        )
                    ):
                        raise ValueError(f"normalized sdist metadata drifted for {name!r}")

                types[name] = kind
                records.append(_Member(name, kind, member.size, digest, executable))

            if types.get(expected_root) != "directory":
                raise ValueError("sdist root must be an explicit directory")
            for name in names:
                if name == expected_root:
                    continue
                parent = name.rsplit("/", 1)[0]
                if types.get(parent) != "directory":
                    raise ValueError(f"sdist member {name!r} has a missing or non-directory parent")
            return observed_identity, tuple(records)


def _canonical_tarinfo(member: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
    canonical = tarfile.TarInfo(member.name)
    canonical.type = tarfile.DIRTYPE if member.isdir() else tarfile.REGTYPE
    executable = member.isdir() or bool(member.mode & 0o111)
    canonical.mode = 0o755 if executable else 0o644
    canonical.uid = 0
    canonical.gid = 0
    canonical.uname = ""
    canonical.gname = ""
    canonical.mtime = epoch
    canonical.size = 0 if member.isdir() else member.size
    canonical.pax_headers = {}
    return canonical


def _write_normalized(
    source_path: Path,
    source_identity: _Identity,
    raw_destination: BinaryIO,
    epoch: int,
) -> None:
    raw_source, _ = _open_regular(source_path, source_identity)
    with raw_source:
        try:
            source_context = tarfile.open(fileobj=raw_source, mode="r:gz")
        except (OSError, tarfile.TarError, EOFError):
            raise ValueError("sdist changed into an invalid archive") from None
        with source_context as source:
            members = source.getmembers()
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_destination,
                compresslevel=9,
                mtime=epoch,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w|",
                    format=tarfile.PAX_FORMAT,
                ) as output:
                    for member in members:
                        canonical = _canonical_tarinfo(member, epoch)
                        payload = source.extractfile(member) if member.isreg() else None
                        if member.isreg() and payload is None:
                            raise ValueError(
                                f"sdist member {member.name!r} has no readable payload"
                            )
                        output.addfile(canonical, payload)
            raw_destination.flush()
            os.fsync(raw_destination.fileno())


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EBADF, errno.EINVAL, errno.EROFS}:
                raise
    finally:
        os.close(descriptor)


def normalize_sdist(archive: Path, *, epoch: int) -> None:
    """Validate and atomically normalize one ``.tar.gz`` source distribution."""
    archive = Path(archive)
    normalized_epoch = validate_epoch(epoch)
    expected_root, initial_identity = _validate_archive_path(archive)
    source_identity, source_manifest = _scan_archive(
        archive,
        expected_root,
        expected_identity=initial_identity,
    )

    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{archive.name}.",
        suffix=".tmp",
        dir=archive.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            _write_normalized(archive, source_identity, destination, normalized_epoch)
            os.fchmod(destination.fileno(), 0o644)
            os.utime(destination.fileno(), (normalized_epoch, normalized_epoch))
        _, normalized_manifest = _scan_archive(
            temp_path,
            expected_root,
            normalized_epoch=normalized_epoch,
        )
        if normalized_manifest != source_manifest:
            raise ValueError("normalized sdist changed member names, types, or payloads")

        current = archive.lstat()
        if stat.S_ISLNK(current.st_mode) or _identity(current) != source_identity:
            raise ValueError("sdist changed before atomic replacement")
        os.replace(temp_path, archive)
        _sync_directory(archive.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _parse_epoch(value: str) -> int:
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is None:
        raise argparse.ArgumentTypeError("epoch must be a canonical unsigned decimal integer")
    try:
        return validate_epoch(int(value))
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epoch", required=True, type=_parse_epoch)
    parser.add_argument("archive", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    normalize_sdist(arguments.archive, epoch=arguments.epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
