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
import sys
import tarfile
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Sequence


_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_MEMBER_BYTES = 256 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_MEMBERS = 10_000
_MAX_NAME_BYTES = 4_096
_MAX_DECOMPRESS_BYTES = 1024 * 1024
_MAX_TAR_BYTES = _MAX_TOTAL_BYTES + _MAX_MEMBERS * 8 * 1024 + 20 * 1024
_COPY_CHUNK = 1024 * 1024
_ARCHIVE_ROOT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_VOLATILE_PAX_FIELDS = frozenset({"mtime"})


class SdistNormalizationError(ValueError):
    """The input is not one bounded, canonical source distribution."""


@dataclass(frozen=True)
class _Identity:
    mode: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


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
        raise SdistNormalizationError("epoch must be an integer")
    if not 0 <= epoch <= 2**32 - 1:
        raise SdistNormalizationError("epoch must be between 0 and 2**32 - 1")
    return epoch


def _identity(metadata: os.stat_result) -> _Identity:
    return _Identity(
        mode=metadata.st_mode,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _validate_archive_path(path: Path) -> tuple[str, _Identity]:
    if not path.name.endswith(".tar.gz"):
        raise SdistNormalizationError("sdist path must end in .tar.gz")
    expected_root = path.name[: -len(".tar.gz")]
    if _ARCHIVE_ROOT.fullmatch(expected_root) is None or "-" not in expected_root:
        raise SdistNormalizationError("sdist filename must contain a canonical archive root")
    try:
        metadata = path.lstat()
    except OSError:
        raise SdistNormalizationError(
            "sdist must be an existing regular non-symlink file"
        ) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SdistNormalizationError("sdist must be an existing regular non-symlink file")
    if not 0 < metadata.st_size <= _MAX_ARCHIVE_BYTES:
        raise SdistNormalizationError("sdist compressed size is outside the supported bound")
    return expected_root, _identity(metadata)


def _open_regular(path: Path, expected: _Identity | None = None) -> tuple[BinaryIO, _Identity]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT}:
            raise SdistNormalizationError("sdist must remain a regular non-symlink file") from None
        raise SdistNormalizationError("sdist could not be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        observed = _identity(metadata)
        if not stat.S_ISREG(metadata.st_mode):
            raise SdistNormalizationError("sdist must remain a regular non-symlink file")
        if not 0 < metadata.st_size <= _MAX_ARCHIVE_BYTES:
            raise SdistNormalizationError("sdist compressed size is outside the supported bound")
        if expected is not None and observed != expected:
            raise SdistNormalizationError("sdist changed while it was being normalized")
        return os.fdopen(descriptor, "rb"), observed
    except BaseException:
        os.close(descriptor)
        raise


def _validate_complete_gzip(stream: BinaryIO) -> None:
    """Require one complete bounded gzip member with no trailing bytes."""
    expanded = 0
    decoder = zlib.decompressobj(wbits=31)
    try:
        stream.seek(0)
        while chunk := stream.read(16 * 1024):
            pending = chunk
            while pending:
                remaining = _MAX_TAR_BYTES - expanded
                output_limit = min(_MAX_DECOMPRESS_BYTES, remaining + 1)
                output = decoder.decompress(pending, output_limit)
                expanded += len(output)
                if expanded > _MAX_TAR_BYTES:
                    raise SdistNormalizationError(
                        "sdist expanded archive is outside the supported bound"
                    )
                if decoder.eof:
                    if decoder.unused_data or stream.read(1):
                        raise SdistNormalizationError(
                            "sdist must contain exactly one gzip member without trailing data"
                        )
                    return
                next_pending = decoder.unconsumed_tail
                if next_pending == pending and not output:
                    raise SdistNormalizationError("sdist gzip decoder made no progress")
                pending = next_pending
        if not decoder.eof:
            raise SdistNormalizationError("sdist gzip stream is truncated")
    except (OSError, zlib.error) as error:
        raise SdistNormalizationError("sdist gzip stream is malformed") from error
    finally:
        stream.seek(0)


def _validate_tar_tail(stream: BinaryIO, parsed_offset: int) -> None:
    """Reject unparsed data hidden after tar's logical end-of-archive marker."""
    try:
        stream.seek(parsed_offset)
    except (OSError, ValueError) as error:
        raise SdistNormalizationError("sdist tar stream could not be verified") from error
    observed = 0
    while chunk := stream.read(_COPY_CHUNK):
        observed += len(chunk)
        if any(chunk):
            raise SdistNormalizationError("sdist contains nonzero data after tar EOF")
    if observed < 1024 or observed % 512 != 0:
        raise SdistNormalizationError("sdist tar EOF padding is incomplete or noncanonical")


def _validate_header_string_fields(
    stream: BinaryIO,
    members: Sequence[tarfile.TarInfo],
) -> None:
    """Reject bytes hidden after NUL terminators in fixed tar header strings."""
    fields = (
        (0, 100),
        (157, 100),
        (265, 32),
        (297, 32),
        (345, 155),
    )
    for member in members:
        header_offset = member.offset_data - 512
        if header_offset < 0 or header_offset % 512 != 0:
            raise SdistNormalizationError("sdist member header offset is noncanonical")
        stream.seek(header_offset)
        header = stream.read(512)
        if len(header) != 512:
            raise SdistNormalizationError("sdist member header is truncated")
        for start, length in fields:
            value = header[start : start + length]
            separator = value.find(b"\0")
            if separator >= 0 and any(value[separator + 1 :]):
                raise SdistNormalizationError(
                    "sdist tar header hides data after a string terminator"
                )


def _validate_member_name(name: object, expected_root: str) -> str:
    if not isinstance(name, str) or not name:
        raise SdistNormalizationError("sdist member name must be a non-empty string")
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise SdistNormalizationError("sdist member name is not valid UTF-8") from error
    pure = PurePosixPath(name)
    if (
        len(encoded) > _MAX_NAME_BYTES
        or "\\" in name
        or "\x00" in name
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or str(pure) != name
        or (name != expected_root and not name.startswith(f"{expected_root}/"))
    ):
        raise SdistNormalizationError("sdist member path must be canonical and relative")
    return name


def _validate_pax_headers(member: tarfile.TarInfo) -> None:
    for key, value in member.pax_headers.items():
        if key in _VOLATILE_PAX_FIELDS:
            continue
        if key == "path" and value == member.name:
            continue
        raise SdistNormalizationError(f"sdist member {member.name!r} has unsupported PAX metadata")


def _hash_payload(archive: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    stream = archive.extractfile(member)
    if stream is None:
        raise SdistNormalizationError(f"sdist member {member.name!r} has no readable payload")
    digest = hashlib.sha256()
    observed = 0
    with stream:
        while True:
            chunk = stream.read(_COPY_CHUNK)
            if not chunk:
                break
            observed += len(chunk)
            if observed > member.size:
                raise SdistNormalizationError(
                    f"sdist member {member.name!r} exceeds its declared size"
                )
            digest.update(chunk)
    if observed != member.size:
        raise SdistNormalizationError(f"sdist member {member.name!r} is truncated")
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
        _validate_complete_gzip(raw)
        try:
            with gzip.GzipFile(fileobj=raw, mode="rb") as expanded:
                with tarfile.open(fileobj=expanded, mode="r:") as archive:
                    if archive.pax_headers:
                        raise SdistNormalizationError("sdist global PAX metadata is unsupported")
                    members = archive.getmembers()
                    _validate_header_string_fields(expanded, members)
                    _validate_tar_tail(expanded, archive.offset)
                    if not 0 < len(members) <= _MAX_MEMBERS:
                        raise SdistNormalizationError(
                            "sdist member count is outside the supported bound"
                        )

                    names: set[str] = set()
                    types: dict[str, str] = {}
                    total_size = 0
                    records: list[_Member] = []
                    for member in members:
                        name = _validate_member_name(member.name, expected_root)
                        if name in names:
                            raise SdistNormalizationError(
                                f"sdist contains duplicate member {name!r}"
                            )
                        names.add(name)
                        _validate_pax_headers(member)
                        if member.sparse is not None:
                            raise SdistNormalizationError(
                                "sdist permits only regular files and directories"
                            )
                        if member.isdir():
                            if member.size != 0:
                                raise SdistNormalizationError(
                                    f"sdist directory {name!r} has a payload"
                                )
                            kind = "directory"
                            digest = None
                            executable = True
                        elif member.isreg():
                            if not 0 <= member.size <= _MAX_MEMBER_BYTES:
                                raise SdistNormalizationError(
                                    f"sdist member {name!r} exceeds the supported bound"
                                )
                            total_size += member.size
                            if total_size > _MAX_TOTAL_BYTES:
                                raise SdistNormalizationError(
                                    "sdist payload exceeds the supported total bound"
                                )
                            kind = "file"
                            digest = _hash_payload(archive, member)
                            executable = bool(member.mode & 0o111)
                        else:
                            raise SdistNormalizationError(
                                "sdist permits only regular files and directories"
                            )

                        if normalized_epoch is not None:
                            expected_mode = 0o755 if kind == "directory" or executable else 0o644
                            if (
                                member.uid != 0
                                or member.gid != 0
                                or member.uname != ""
                                or member.gname != ""
                                or member.mtime != normalized_epoch
                                or stat.S_IMODE(member.mode) != expected_mode
                                or any(key in _VOLATILE_PAX_FIELDS for key in member.pax_headers)
                            ):
                                raise SdistNormalizationError(
                                    f"normalized sdist metadata drifted for {name!r}"
                                )

                        types[name] = kind
                        records.append(_Member(name, kind, member.size, digest, executable))

                    required = {
                        expected_root: "directory",
                        f"{expected_root}/PKG-INFO": "file",
                        f"{expected_root}/pyproject.toml": "file",
                    }
                    if any(types.get(name) != kind for name, kind in required.items()):
                        raise SdistNormalizationError(
                            "sdist is missing its root, PKG-INFO, or pyproject.toml"
                        )
                    for name in names:
                        if name == expected_root:
                            continue
                        parent = name.rsplit("/", 1)[0]
                        if types.get(parent) != "directory":
                            raise SdistNormalizationError(
                                f"sdist member {name!r} has a missing or non-directory parent"
                            )
                    records.sort(key=lambda record: record.name)
                    return observed_identity, tuple(records)
        except SdistNormalizationError:
            raise
        except (OSError, EOFError, gzip.BadGzipFile, tarfile.TarError) as error:
            raise SdistNormalizationError(
                "sdist is not a valid gzip-compressed tar archive"
            ) from error


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
        _validate_complete_gzip(raw_source)
        try:
            with gzip.GzipFile(fileobj=raw_source, mode="rb") as expanded:
                with tarfile.open(fileobj=expanded, mode="r:") as source:
                    members = source.getmembers()
                    _validate_tar_tail(expanded, source.offset)
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
                            for member in sorted(members, key=lambda item: item.name):
                                canonical = _canonical_tarinfo(member, epoch)
                                payload = source.extractfile(member) if member.isreg() else None
                                if member.isreg() and payload is None:
                                    raise SdistNormalizationError(
                                        f"sdist member {member.name!r} has no readable payload"
                                    )
                                if payload is None:
                                    output.addfile(canonical)
                                else:
                                    with payload:
                                        output.addfile(canonical, payload)
            raw_destination.flush()
            os.fsync(raw_destination.fileno())
        except SdistNormalizationError:
            raise
        except (OSError, EOFError, gzip.BadGzipFile, tarfile.TarError) as error:
            raise SdistNormalizationError("sdist changed into an invalid archive") from error


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_COPY_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_sdist(archive: Path, *, epoch: int) -> str:
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
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(destination.fileno(), 0o600)
            _write_normalized(archive, source_identity, destination, normalized_epoch)
        _, normalized_manifest = _scan_archive(
            temp_path,
            expected_root,
            normalized_epoch=normalized_epoch,
        )
        if normalized_manifest != source_manifest:
            raise SdistNormalizationError(
                "normalized sdist changed member names, types, modes, or payloads"
            )

        try:
            current = archive.lstat()
        except OSError:
            raise SdistNormalizationError("sdist changed before atomic replacement") from None
        if stat.S_ISLNK(current.st_mode) or _identity(current) != source_identity:
            raise SdistNormalizationError("sdist changed before atomic replacement")
        digest = _sha256(temp_path)
        os.replace(temp_path, archive)
        _sync_directory(archive.parent)
        return digest
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
    except SdistNormalizationError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epoch", required=True, type=_parse_epoch)
    parser.add_argument("archive", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        digest = normalize_sdist(arguments.archive, epoch=arguments.epoch)
    except SdistNormalizationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"normalized {arguments.archive.name} sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
