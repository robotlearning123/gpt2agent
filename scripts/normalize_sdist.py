#!/usr/bin/env python3
"""Rewrite one trusted build sdist with deterministic, fail-closed metadata."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import os
import re
import stat
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path, PurePosixPath
from typing import BinaryIO


MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 65_536
MAX_TAR_BYTES = MAX_TOTAL_BYTES + MAX_MEMBERS * 1024
MAX_NAME_BYTES = 4_096
MAX_DECOMPRESS_BYTES = 1024 * 1024
MAX_GZIP_EPOCH = (1 << 32) - 1
_ARCHIVE_ROOT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_ALLOWED_PAX_HEADERS = frozenset({"mtime", "path"})


class SdistNormalizationError(ValueError):
    """The input is not one bounded, canonical source distribution."""


def _validated_epoch(epoch: int) -> int:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or not 0 <= epoch <= MAX_GZIP_EPOCH:
        raise SdistNormalizationError("epoch must fit the gzip unsigned 32-bit field")
    return epoch


def _canonical_path(path: Path) -> tuple[Path, os.stat_result]:
    candidate = Path(path)
    try:
        parent = candidate.parent.resolve(strict=True)
        candidate = parent / candidate.name
        metadata = os.lstat(candidate)
    except OSError as exc:
        raise SdistNormalizationError("sdist path is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SdistNormalizationError("sdist must be a regular file, not a symlink")
    if metadata.st_size < 1 or metadata.st_size > MAX_ARCHIVE_BYTES:
        raise SdistNormalizationError("sdist compressed size is outside the reviewed bound")
    return candidate, metadata


def _archive_root(path: Path) -> str:
    suffix = ".tar.gz"
    if not path.name.endswith(suffix):
        raise SdistNormalizationError("sdist filename must end in .tar.gz")
    root = path.name[: -len(suffix)]
    if _ARCHIVE_ROOT.fullmatch(root) is None or "-" not in root:
        raise SdistNormalizationError("sdist filename has no canonical archive root")
    return root


def _safe_member_name(name: str, root: str) -> str:
    try:
        encoded = name.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise SdistNormalizationError("sdist member name is not valid UTF-8") from exc
    pure = PurePosixPath(name)
    if (
        not encoded
        or len(encoded) > MAX_NAME_BYTES
        or "\\" in name
        or "\x00" in name
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or str(pure) != name
        or (name != root and not name.startswith(f"{root}/"))
    ):
        raise SdistNormalizationError("sdist contains a noncanonical member path")
    return name


def _validated_members(archive: tarfile.TarFile, root: str) -> list[tarfile.TarInfo]:
    if archive.pax_headers:
        raise SdistNormalizationError("sdist global PAX headers are not supported")
    members = archive.getmembers()
    if not 1 <= len(members) <= MAX_MEMBERS:
        raise SdistNormalizationError("sdist member count is outside the reviewed bound")

    seen: set[str] = set()
    total_size = 0
    for member in members:
        name = _safe_member_name(member.name, root)
        if name in seen:
            raise SdistNormalizationError("sdist contains duplicate member names")
        seen.add(name)
        if set(member.pax_headers) - _ALLOWED_PAX_HEADERS or member.sparse is not None:
            raise SdistNormalizationError("sdist contains unsupported PAX metadata")
        if member.type == tarfile.DIRTYPE:
            if member.size != 0:
                raise SdistNormalizationError("sdist directory has a nonzero size")
        elif member.type in {tarfile.REGTYPE, tarfile.AREGTYPE}:
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                raise SdistNormalizationError("sdist member size is outside the reviewed bound")
            total_size += member.size
            if total_size > MAX_TOTAL_BYTES:
                raise SdistNormalizationError("sdist expanded size is outside the reviewed bound")
        else:
            raise SdistNormalizationError("sdist contains a link or special file")

    required = {root, f"{root}/PKG-INFO", f"{root}/pyproject.toml"}
    if not required <= seen:
        raise SdistNormalizationError("sdist is missing its root, PKG-INFO, or pyproject.toml")
    by_name = {member.name: member for member in members}
    regular_names = {
        name
        for name, member in by_name.items()
        if member.type in {tarfile.REGTYPE, tarfile.AREGTYPE}
    }
    if any(
        str(parent) in regular_names
        for name in seen
        for parent in PurePosixPath(name).parents
    ):
        raise SdistNormalizationError("sdist uses a regular file as a parent path")
    if by_name[root].type != tarfile.DIRTYPE or any(
        by_name[name].type not in {tarfile.REGTYPE, tarfile.AREGTYPE}
        for name in required - {root}
    ):
        raise SdistNormalizationError("sdist required entries have invalid types")
    return sorted(members, key=lambda member: member.name)


def _normalized_member(member: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
    result = copy.copy(member)
    result.uid = 0
    result.gid = 0
    result.uname = ""
    result.gname = ""
    result.mtime = epoch
    result.pax_headers = {}
    result.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
    return result


def _same_metadata(current: os.stat_result, expected: os.stat_result) -> bool:
    return (
        current.st_mode == expected.st_mode
        and current.st_dev == expected.st_dev
        and current.st_ino == expected.st_ino
        and current.st_size == expected.st_size
        and current.st_mtime_ns == expected.st_mtime_ns
        and current.st_ctime_ns == expected.st_ctime_ns
    )


def _same_file(path: Path, expected: os.stat_result) -> bool:
    try:
        current = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(current.st_mode)
        and not stat.S_ISLNK(current.st_mode)
        and _same_metadata(current, expected)
    )


def _open_source(path: Path) -> BinaryIO:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SdistNormalizationError("sdist could not be opened safely") from exc
    return os.fdopen(descriptor, "rb")


def _validate_complete_gzip(stream: BinaryIO) -> None:
    """Require exactly one complete gzip member with no ignored trailing bytes."""
    expanded = 0
    decoder = zlib.decompressobj(wbits=31)
    try:
        stream.seek(0)
        while chunk := stream.read(16 * 1024):
            pending = chunk
            while pending:
                remaining = MAX_TAR_BYTES - expanded
                output_limit = min(MAX_DECOMPRESS_BYTES, remaining + 1)
                expanded += len(decoder.decompress(pending, output_limit))
                if expanded > MAX_TAR_BYTES:
                    raise SdistNormalizationError(
                        "sdist expanded archive is outside the reviewed bound"
                    )
                if decoder.eof:
                    if decoder.unused_data or stream.read(1):
                        raise SdistNormalizationError(
                            "sdist must contain exactly one gzip member without trailing data"
                        )
                    return
                pending = decoder.unconsumed_tail
        if not decoder.eof:
            raise SdistNormalizationError("sdist gzip stream is truncated")
    except (OSError, zlib.error) as exc:
        raise SdistNormalizationError("sdist gzip stream is malformed") from exc
    finally:
        stream.seek(0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_sdist(path: Path, *, epoch: int) -> str:
    """Atomically normalize *path* and return the resulting SHA-256 digest."""
    normalized_epoch = _validated_epoch(epoch)
    source, source_metadata = _canonical_path(path)
    root = _archive_root(source)
    temporary: Path | None = None
    try:
        with _open_source(source) as source_stream:
            opened_metadata = os.fstat(source_stream.fileno())
            if not _same_metadata(opened_metadata, source_metadata):
                raise SdistNormalizationError("sdist changed while it was opened")
            _validate_complete_gzip(source_stream)
            try:
                with tarfile.open(fileobj=source_stream, mode="r:gz") as incoming:
                    members = _validated_members(incoming, root)
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{source.name}.",
                        suffix=".tmp",
                        dir=source.parent,
                    )
                    temporary = Path(temporary_name)
                    os.fchmod(descriptor, 0o600)
                    with os.fdopen(descriptor, "wb") as raw_output:
                        with gzip.GzipFile(
                            filename="",
                            mode="wb",
                            fileobj=raw_output,
                            compresslevel=9,
                            mtime=normalized_epoch,
                        ) as compressed:
                            with tarfile.open(
                                fileobj=compressed,
                                mode="w",
                                format=tarfile.PAX_FORMAT,
                            ) as outgoing:
                                for member in members:
                                    payload = incoming.extractfile(member) if member.isfile() else None
                                    if member.isfile() and payload is None:
                                        raise SdistNormalizationError(
                                            "sdist regular member has no readable payload"
                                        )
                                    with payload if payload is not None else _NullContext() as stream:
                                        outgoing.addfile(
                                            _normalized_member(member, normalized_epoch),
                                            stream,
                                        )
                        raw_output.flush()
                        os.fsync(raw_output.fileno())
            except (EOFError, gzip.BadGzipFile, tarfile.TarError, OSError) as exc:
                raise SdistNormalizationError("sdist archive is malformed") from exc
            if not _same_metadata(os.fstat(source_stream.fileno()), opened_metadata):
                raise SdistNormalizationError("sdist changed during normalization")

        if not _same_file(source, source_metadata):
            raise SdistNormalizationError("sdist changed during normalization")
        digest = _sha256(temporary)
        os.replace(temporary, source)
        temporary = None
        directory = os.open(source.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return digest
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


def _epoch(value: str) -> int:
    try:
        return _validated_epoch(int(value, 10))
    except (ValueError, SdistNormalizationError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdist", type=Path)
    parser.add_argument("--epoch", required=True, type=_epoch)
    args = parser.parse_args(argv)
    try:
        digest = normalize_sdist(args.sdist, epoch=args.epoch)
    except SdistNormalizationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"normalized {args.sdist.name} sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
