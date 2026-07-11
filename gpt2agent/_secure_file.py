"""Small, dependency-free primitives for security-sensitive local writes."""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any


_TEMP_ATTEMPTS = 128


def _exclusive_flags() -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _open_random_temp_path(path: Path) -> tuple[int, Path]:
    """Create an unpredictable, exclusive 0600 sibling of *path*."""
    for _ in range(_TEMP_ATTEMPTS):
        candidate = path.with_name(f".{path.name}.tmp-{secrets.token_hex(16)}")
        try:
            return os.open(candidate, _exclusive_flags(), 0o600), candidate
        except FileExistsError:
            continue
    raise FileExistsError(f"could not reserve a temporary file beside {path}")


def _write_and_sync(fd: int, content: bytes, mode: int) -> None:
    """Write all bytes to *fd*, fix its mode, and make the data durable."""
    remaining = memoryview(content)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        if written == 0:  # pragma: no cover - defensive OS failure
            raise OSError("short write while persisting a local secret")
        remaining = remaining[written:]
    try:
        os.fchmod(fd, mode)
    except AttributeError:  # pragma: no cover - Windows
        pass
    os.fsync(fd)


def _sync_directory_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        # Windows and a few filesystems do not support fsync on a directory.
        unsupported = {errno.EBADF, errno.EINVAL, errno.EROFS}
        if hasattr(errno, "ENOTSUP"):
            unsupported.add(errno.ENOTSUP)
        if exc.errno not in unsupported:
            raise


def _sync_directory_path(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return  # Non-POSIX platforms may not permit opening directories.
    try:
        _sync_directory_fd(fd)
    finally:
        os.close(fd)


def atomic_replace_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace *path* without opening an existing destination.

    The temporary file is random, same-directory, exclusive, and no-follow
    where the platform exposes ``O_NOFOLLOW``.  Therefore a pre-planted temp
    symlink or destination symlink is replaced rather than followed.
    """
    fd, temp_path = _open_random_temp_path(path)
    try:
        _write_and_sync(fd, content, mode)
        os.close(fd)
        fd = -1
        os.replace(temp_path, path)
        _sync_directory_path(path.parent)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _current_uid() -> int | None:
    get_euid = getattr(os, "geteuid", None)
    return get_euid() if get_euid is not None else None


def _validate_owner(path: Path, owner: int, expected: int | None) -> None:
    if expected is not None and owner != expected:
        raise RuntimeError(
            f"{path} is owned by uid {owner}, not the current uid {expected}; refusing to write"
        )


def _open_private_directory(path: Path) -> int:
    """Open *path* as the current user's real 0700 directory."""
    if os.name == "nt":  # Windows does not expose POSIX directory descriptors.
        raise NotImplementedError
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass

    observed = path.lstat()
    if stat.S_ISLNK(observed.st_mode):
        raise RuntimeError(f"{path} is a symbolic link; refusing to store a token")
    if not stat.S_ISDIR(observed.st_mode):
        raise RuntimeError(f"{path} must be a directory; refusing to store a token")

    expected_uid = _current_uid()
    _validate_owner(path, observed.st_uid, expected_uid)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise RuntimeError(f"{path} must be a directory; refusing to store a token")
        _validate_owner(path, opened.st_uid, expected_uid)
        if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
            raise RuntimeError(f"{path} changed while it was being validated; refusing to write")

        try:
            os.fchmod(fd, 0o700)
        except AttributeError:  # pragma: no cover - Windows
            os.chmod(path, 0o700)
        final = os.fstat(fd)
        if stat.S_IMODE(final.st_mode) != 0o700:
            raise RuntimeError(f"{path} could not be restricted to mode 0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _existing_entry(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_random_temp_at(directory_fd: int, final_name: str) -> tuple[int, str]:
    for _ in range(_TEMP_ATTEMPTS):
        name = f".{final_name}.tmp-{secrets.token_hex(16)}"
        try:
            fd = os.open(name, _exclusive_flags(), 0o600, dir_fd=directory_fd)
            return fd, name
        except FileExistsError:
            continue
    raise FileExistsError(f"could not reserve a temporary file beside {final_name}")


def _validate_private_directory_path(path: Path) -> None:
    """Portable fallback validation when directory descriptors are unavailable."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass

    observed = path.lstat()
    if stat.S_ISLNK(observed.st_mode):
        raise RuntimeError(f"{path} is a symbolic link; refusing to store a token")
    if not stat.S_ISDIR(observed.st_mode):
        raise RuntimeError(f"{path} must be a directory; refusing to store a token")
    expected_uid = _current_uid()
    _validate_owner(path, observed.st_uid, expected_uid)

    try:
        os.chmod(path, 0o700, follow_symlinks=False)
    except (NotImplementedError, TypeError):  # pragma: no cover - Windows
        path.chmod(0o700)

    final = path.lstat()
    if stat.S_ISLNK(final.st_mode) or not stat.S_ISDIR(final.st_mode):
        raise RuntimeError(f"{path} changed while it was being validated; refusing to write")
    if (final.st_dev, final.st_ino) != (observed.st_dev, observed.st_ino):
        raise RuntimeError(f"{path} changed while it was being validated; refusing to write")
    _validate_owner(path, final.st_uid, expected_uid)
    if expected_uid is not None and stat.S_IMODE(final.st_mode) != 0o700:
        raise RuntimeError(f"{path} could not be restricted to mode 0700")


def _write_private_json_by_path(path: Path, content: bytes) -> None:
    """Path-based fallback for platforms without secure directory-fd APIs."""
    _validate_private_directory_path(path.parent)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise RuntimeError(f"{path} is a symbolic link; refusing to store a token")
        if not stat.S_ISREG(existing.st_mode):
            raise RuntimeError(f"{path} must be a regular file; refusing to store a token")
        _validate_owner(path, existing.st_uid, _current_uid())

    atomic_replace_bytes(path, content, mode=0o600)
    written = path.lstat()
    if stat.S_ISLNK(written.st_mode) or not stat.S_ISREG(written.st_mode):
        raise RuntimeError(f"{path} was not stored as a regular file")
    expected_uid = _current_uid()
    _validate_owner(path, written.st_uid, expected_uid)
    if expected_uid is not None and stat.S_IMODE(written.st_mode) != 0o600:
        raise RuntimeError(f"{path} was not stored with mode 0600")


def write_private_json(path: Path, value: Any, *, indent: int | None = None) -> None:
    """Safely write JSON below an owner-only, non-symlink directory.

    Existing symbolic-link and non-regular targets are rejected.  Existing
    regular files are atomically replaced, so even a hard link is never
    modified in place.
    """
    working_directory = Path.cwd()
    if not path.is_absolute():
        path = working_directory / path
    path = Path(os.path.normpath(path))
    invalid_parents = {path, working_directory, Path(path.anchor)}
    if path.name in {"", ".", ".."} or path.parent in invalid_parents:
        raise ValueError(
            "private JSON path must use a dedicated non-root parent directory"
        )

    content = json.dumps(value, indent=indent).encode()
    try:
        directory_fd = _open_private_directory(path.parent)
    except NotImplementedError:
        _write_private_json_by_path(path, content)
        return
    temp_fd = -1
    temp_name: str | None = None
    try:
        existing = _existing_entry(directory_fd, path.name)
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise RuntimeError(f"{path} is a symbolic link; refusing to store a token")
            if not stat.S_ISREG(existing.st_mode):
                raise RuntimeError(f"{path} must be a regular file; refusing to store a token")
            _validate_owner(path, existing.st_uid, _current_uid())

        temp_fd, temp_name = _open_random_temp_at(directory_fd, path.name)
        _write_and_sync(temp_fd, content, 0o600)
        os.close(temp_fd)
        temp_fd = -1
        os.replace(
            temp_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temp_name = None
        _sync_directory_fd(directory_fd)

        written = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(written.st_mode) or stat.S_IMODE(written.st_mode) != 0o600:
            raise RuntimeError(f"{path} was not stored as a regular 0600 file")
        _validate_owner(path, written.st_uid, _current_uid())
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)
