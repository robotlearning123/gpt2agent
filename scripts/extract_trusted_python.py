#!/usr/bin/python3.12
"""Safely install the exact reviewed CPython 3.12.13 account-gate runtime."""

from __future__ import annotations

import argparse
import hashlib
import os
import posixpath
import shutil
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class ArchivePolicy:
    source_url: str
    archive_size: int
    archive_sha256: str
    executable_sha256: str
    runtime_tree_sha256: str
    symlink_map_sha256: str
    regular_files: int
    symlinks: int
    directories: int


PINNED_POLICY = ArchivePolicy(
    source_url=(
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        "20260510/cpython-3.12.13%2B20260510-x86_64-unknown-linux-gnu-"
        "install_only.tar.gz"
    ),
    archive_size=111_027_266,
    archive_sha256="e7332b4b4bb85006deb48d251c786a04c14de104c9b3a006b33457a4a604b8bc",
    executable_sha256="f7014f68e3c8f180811740735cf1dd5c28be6cff84db11d0ced2a8cd039670a0",
    runtime_tree_sha256="74e93975be819af02939878b97bafb7aa7961adfa31ef7c47845d25e2b88fc07",
    symlink_map_sha256="7bff615f0d200134c34e2b3b27cce6fa65d3f608c70508d2bcbb632b82f4fae5",
    regular_files=3_474,
    symlinks=1_048,
    directories=0,
)

_MAX_MEMBERS = 10_000
_MAX_MEMBER_SIZE = 256 * 1024 * 1024
_MAX_EXPANDED_SIZE = 512 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024
_TREE_HEADER = b"gpt2agent-cpython-runtime-tree-v1\0"


class RuntimeArchiveError(RuntimeError):
    """The supplied archive or extracted runtime violated the trust contract."""


@dataclass(frozen=True)
class ReviewedMember:
    info: tarfile.TarInfo
    relative: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while chunk := stream.read(_COPY_CHUNK):
                digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _safe_archive_path(name: str, *, is_directory: bool) -> str | None:
    if not isinstance(name, str) or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise RuntimeArchiveError("archive contains a control-character path")
    if "\\" in name or name.startswith("/"):
        raise RuntimeArchiveError("archive path is not canonical")
    normalized = name[:-1] if is_directory and name.endswith("/") else name
    if normalized == "python":
        return None
    if not normalized.startswith("python/"):
        raise RuntimeArchiveError("archive member is outside the python/ root")
    relative = normalized.removeprefix("python/")
    parts = relative.split("/")
    if not relative or any(part in {"", ".", ".."} for part in parts):
        raise RuntimeArchiveError("archive path is not canonical")
    if "/".join(parts) != relative:
        raise RuntimeArchiveError("archive path is not canonical")
    return relative


def _safe_link_target(relative: str, target: str) -> str:
    if (
        not isinstance(target, str)
        or not target
        or target.startswith("/")
        or "\\" in target
        or any(ord(char) < 32 or ord(char) == 127 for char in target)
    ):
        raise RuntimeArchiveError("archive symlink target is unsafe")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise RuntimeArchiveError("archive symlink escapes the runtime")
    return resolved


def _validated_members(
    archive: tarfile.TarFile,
    policy: ArchivePolicy,
) -> list[ReviewedMember]:
    members: list[ReviewedMember] = []
    kinds: dict[str, str] = {}
    links: dict[str, tuple[str, str]] = {}
    expanded_size = 0
    counts = {"regular": 0, "symlink": 0, "directory": 0}

    for info in archive.getmembers():
        if len(members) >= _MAX_MEMBERS:
            raise RuntimeArchiveError("archive member count exceeds the limit")
        relative = _safe_archive_path(info.name, is_directory=info.isdir())
        if relative is None:
            if not info.isdir():
                raise RuntimeArchiveError("archive python root is not a directory")
            continue
        if relative in kinds:
            raise RuntimeArchiveError("archive contains a duplicate path")
        if info.isreg():
            if info.sparse is not None or info.size < 0 or info.size > _MAX_MEMBER_SIZE:
                raise RuntimeArchiveError("archive regular file is sparse or oversized")
            expanded_size += info.size
            if expanded_size > _MAX_EXPANDED_SIZE:
                raise RuntimeArchiveError("archive expanded size exceeds the limit")
            kinds[relative] = "regular"
            counts["regular"] += 1
        elif info.isdir():
            kinds[relative] = "directory"
            counts["directory"] += 1
        elif info.issym():
            resolved = _safe_link_target(relative, info.linkname)
            kinds[relative] = "symlink"
            links[relative] = (info.linkname, resolved)
            counts["symlink"] += 1
        else:
            raise RuntimeArchiveError("archive contains a hardlink or special member")
        members.append(ReviewedMember(info=info, relative=relative))

    expected_counts = {
        "regular": policy.regular_files,
        "symlink": policy.symlinks,
        "directory": policy.directories,
    }
    if counts != expected_counts:
        raise RuntimeArchiveError("archive member topology does not match the reviewed asset")

    for path, kind in kinds.items():
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            parent_kind = kinds.get(parent)
            if parent_kind is not None and parent_kind != "directory":
                raise RuntimeArchiveError("archive member is nested below a non-directory")
        if kind == "symlink" and any(
            other.startswith(f"{path}/") for other in kinds if other != path
        ):
            raise RuntimeArchiveError("archive member is nested below a symlink")

    def resolve_link(path: str, seen: frozenset[str]) -> None:
        if path in seen:
            raise RuntimeArchiveError("archive symlink chain contains a cycle")
        kind = kinds.get(path)
        if kind == "regular":
            return
        if kind != "symlink":
            raise RuntimeArchiveError("archive symlink target is missing or non-regular")
        resolve_link(links[path][1], seen | {path})

    for path in links:
        resolve_link(path, frozenset())

    symlink_manifest = hashlib.sha256()
    for member in sorted(
        (member for member in members if member.info.issym()),
        key=lambda item: os.fsencode(item.info.name),
    ):
        symlink_manifest.update(os.fsencode(member.info.name))
        symlink_manifest.update(b"\0")
        symlink_manifest.update(os.fsencode(member.info.linkname))
        symlink_manifest.update(b"\0")
    if symlink_manifest.hexdigest() != policy.symlink_map_sha256:
        raise RuntimeArchiveError("archive symlink map does not match the reviewed asset")
    return members


def _ensure_parent_directories(root: Path, relative: str) -> None:
    current = root
    for component in relative.split("/")[:-1]:
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeArchiveError("extraction parent is not a directory") from None
        os.chmod(current, 0o700, follow_symlinks=False)


def _copy_exact(source: BinaryIO, target: Path, expected_size: int, mode: int) -> None:
    fd = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
    )
    written = 0
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            while chunk := source.read(_COPY_CHUNK):
                written += len(chunk)
                if written > expected_size:
                    raise RuntimeArchiveError("archive member exceeds its declared size")
                output.write(chunk)
            output.flush()
        if written != expected_size:
            raise RuntimeArchiveError("archive member is shorter than its declared size")
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _extract_members(
    archive: tarfile.TarFile,
    members: list[ReviewedMember],
    destination: Path,
) -> None:
    destination.mkdir(mode=0o700)
    os.chmod(destination, 0o700)

    for member in sorted(
        (member for member in members if member.info.isdir()),
        key=lambda item: (item.relative.count("/"), os.fsencode(item.relative)),
    ):
        _ensure_parent_directories(destination, f"{member.relative}/leaf")
        path = destination / member.relative
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700, follow_symlinks=False)

    for member in sorted(
        (member for member in members if member.info.isreg()),
        key=lambda item: os.fsencode(item.relative),
    ):
        _ensure_parent_directories(destination, member.relative)
        source = archive.extractfile(member.info)
        if source is None:
            raise RuntimeArchiveError("archive regular file has no data stream")
        mode = 0o700 if member.info.mode & 0o111 else 0o600
        with source:
            _copy_exact(source, destination / member.relative, member.info.size, mode)

    for member in sorted(
        (member for member in members if member.info.issym()),
        key=lambda item: os.fsencode(item.relative),
    ):
        _ensure_parent_directories(destination, member.relative)
        os.symlink(member.info.linkname, destination / member.relative)


def _tree_digest(root: Path) -> str:
    root = root.resolve(strict=True)
    root_device = root.stat().st_dev
    current_uid = os.getuid()
    records: list[tuple[bytes, bytes]] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                metadata = entry.stat(follow_symlinks=False)
                relative = path.relative_to(root).as_posix()
                if metadata.st_dev != root_device or metadata.st_uid != current_uid:
                    raise RuntimeArchiveError("extracted runtime owner or device is invalid")
                if any(ord(char) < 32 or ord(char) == 127 for char in relative):
                    raise RuntimeArchiveError("extracted runtime path is invalid")
                mode = stat.S_IMODE(metadata.st_mode)
                if stat.S_ISDIR(metadata.st_mode):
                    if mode != 0o700:
                        raise RuntimeArchiveError("extracted runtime directory mode is invalid")
                    record = b"D\0" + os.fsencode(relative) + b"\0" + b"700\0"
                    records.append((os.fsencode(relative), record))
                    visit(path)
                elif stat.S_ISREG(metadata.st_mode):
                    if mode not in {0o600, 0o700} or metadata.st_nlink != 1:
                        raise RuntimeArchiveError("extracted runtime file mode or links are invalid")
                    file_hash = _sha256_file(path).encode("ascii")
                    record = (
                        b"F\0"
                        + os.fsencode(relative)
                        + b"\0"
                        + f"{mode:o}".encode("ascii")
                        + b"\0"
                        + str(metadata.st_size).encode("ascii")
                        + b"\0"
                        + file_hash
                        + b"\0"
                    )
                    records.append((os.fsencode(relative), record))
                elif stat.S_ISLNK(metadata.st_mode):
                    target = os.readlink(path)
                    try:
                        resolved = (path.parent / target).resolve(strict=True)
                    except (OSError, RuntimeError):
                        raise RuntimeArchiveError(
                            "extracted runtime symlink is invalid"
                        ) from None
                    if not resolved.is_relative_to(root) or not resolved.is_file():
                        raise RuntimeArchiveError("extracted runtime symlink escapes the tree")
                    record = (
                        b"L\0"
                        + os.fsencode(relative)
                        + b"\0"
                        + os.fsencode(target)
                        + b"\0"
                    )
                    records.append((os.fsencode(relative), record))
                else:
                    raise RuntimeArchiveError("extracted runtime contains a special entry")

    visit(root)
    digest = hashlib.sha256(_TREE_HEADER)
    for _path, record in sorted(records, key=lambda item: item[0]):
        digest.update(record)
    return digest.hexdigest()


def _validate_protected_archive(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeArchiveError("trusted runtime archive path is not canonical")
    canonical = path.resolve(strict=True)
    if canonical != path:
        raise RuntimeArchiveError("trusted runtime archive path is not canonical")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in {0, os.getuid()}:
        raise RuntimeArchiveError("trusted runtime archive file is invalid")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise RuntimeArchiveError("trusted runtime archive is group- or world-writable")

    current = path.parent
    while True:
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.getuid()}:
            raise RuntimeArchiveError("trusted runtime archive ancestry is invalid")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o022 and not (metadata.st_uid == 0 and mode & stat.S_ISVTX):
            raise RuntimeArchiveError("trusted runtime archive ancestry is writable")
        if current == current.parent:
            break
        current = current.parent
    return canonical


def _snapshot_archive(source: Path, parent: Path, policy: ArchivePolicy) -> Path:
    snapshot = parent / ".reviewed-cpython-runtime.tar.gz"
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    target_fd = -1
    digest = hashlib.sha256()
    copied = 0
    try:
        target_fd = os.open(
            snapshot,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        source_metadata = os.fstat(source_fd)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise RuntimeArchiveError("trusted runtime archive changed type")
        with os.fdopen(source_fd, "rb", closefd=False) as input_stream, os.fdopen(
            target_fd, "wb", closefd=False
        ) as output_stream:
            while chunk := input_stream.read(_COPY_CHUNK):
                copied += len(chunk)
                if copied > policy.archive_size:
                    raise RuntimeArchiveError("trusted runtime archive size does not match")
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(target_fd)
        if copied != policy.archive_size or digest.hexdigest() != policy.archive_sha256:
            raise RuntimeArchiveError("trusted runtime archive digest does not match")
    except BaseException:
        snapshot.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)
    return snapshot


def _validate_destination_parent(destination: Path) -> Path:
    supplied_parent = destination.parent
    try:
        parent = supplied_parent.resolve(strict=True)
    except OSError:
        raise RuntimeArchiveError("runtime destination parent is unavailable") from None
    if parent != supplied_parent:
        raise RuntimeArchiveError("runtime destination parent path is not canonical")

    current_uid = os.getuid()
    current = parent
    while True:
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, current_uid}:
            raise RuntimeArchiveError("runtime destination ancestry is invalid")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o022 and not (
            metadata.st_uid == 0 and mode & stat.S_ISVTX
        ):
            raise RuntimeArchiveError("runtime destination ancestry is writable")
        if current == current.parent:
            break
        current = current.parent

    parent_metadata = parent.lstat()
    if parent_metadata.st_uid != current_uid or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise RuntimeArchiveError("runtime destination parent must be owner-private")
    return parent


def _remove_destination(path: Path) -> None:
    if not os.path.lexists(path):
        return
    metadata = path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def install_reviewed_runtime(
    archive_path: Path,
    destination: Path,
    policy: ArchivePolicy = PINNED_POLICY,
) -> None:
    archive_path = _validate_protected_archive(archive_path)
    if not destination.is_absolute() or os.path.lexists(destination):
        raise RuntimeArchiveError("runtime destination must be a new absolute path")
    parent = _validate_destination_parent(destination)

    snapshot: Path | None = None
    try:
        snapshot = _snapshot_archive(archive_path, parent, policy)
        with tarfile.open(snapshot, mode="r:gz") as archive:
            members = _validated_members(archive, policy)
            _extract_members(archive, members, destination)
        executable = destination / "bin" / "python3.12"
        if _sha256_file(executable) != policy.executable_sha256:
            raise RuntimeArchiveError("trusted runtime executable digest does not match")
        if _tree_digest(destination) != policy.runtime_tree_sha256:
            raise RuntimeArchiveError("trusted runtime tree digest does not match")
    except BaseException:
        _remove_destination(destination)
        raise
    finally:
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        install_reviewed_runtime(args.archive, args.destination)
    except (OSError, RuntimeArchiveError, tarfile.TarError) as exc:
        raise SystemExit(f"trusted runtime installer: {exc}") from None


if __name__ == "__main__":
    main()
