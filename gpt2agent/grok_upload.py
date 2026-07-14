"""Descriptor-safe local upload policy for Grok website attachments.

``GrokUploadPolicy.open`` transfers ownership of the returned descriptor to its
caller.  The caller must close ``ValidatedUpload.fd`` after the upload attempt.
"""

from __future__ import annotations

import os
import re
import stat
from collections import OrderedDict
from collections.abc import Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Any

from .grok_errors import GrokError


DEFAULT_UPLOAD_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "application/pdf",
        "application/json",
        "text/csv",
        "image/png",
        "image/jpeg",
        "image/webp",
    }
)
DEFAULT_UPLOAD_MAX_BYTES = 25 * 1024 * 1024

_SUFFIX_MEDIA_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_MAX_PATH_BYTES = 4_096
_READ_CHUNK_BYTES = 64 * 1024
_REGISTRY_CAPACITY = 256
_MAX_ATTACHMENTS_PER_REQUEST = 20
_ATTACHMENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


@dataclass(frozen=True)
class ValidatedUpload:
    fd: int
    name: str
    media_type: str
    size: int


def _blocked() -> GrokError:
    return GrokError("GROK_UPLOAD_BLOCKED", retryable=False)


def _close_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _required_open_flags() -> tuple[int, int]:
    if (
        os.name != "posix"
        or not _OPEN_SUPPORTS_DIR_FD
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_CLOEXEC")
        or not hasattr(os, "O_NONBLOCK")
    ):
        raise _blocked()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    leaf_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    return directory_flags, leaf_flags


def _validated_root_parts(roots: Sequence[Path]) -> tuple[tuple[str, ...], ...]:
    if isinstance(roots, (str, bytes, bytearray)) or not isinstance(roots, Sequence):
        raise _blocked()
    normalized: list[tuple[str, ...]] = []
    for root in roots:
        if not isinstance(root, Path) or not root.is_absolute():
            raise _blocked()
        raw = os.fspath(root)
        if "\x00" in raw or len(os.fsencode(raw)) > _MAX_PATH_BYTES:
            raise _blocked()
        parts = root.parts
        if not parts or parts[0] != os.sep or any(part in {"", ".", ".."} for part in parts[1:]):
            raise _blocked()
        normalized.append(tuple(parts[1:]))
    return tuple(sorted(set(normalized), key=len, reverse=True))


def _validated_target_parts(path: Any) -> tuple[str, ...]:
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise _blocked()
    if len(os.fsencode(path)) > _MAX_PATH_BYTES or not path.startswith(os.sep):
        raise _blocked()
    if path.startswith(os.sep * 2):
        raise _blocked()
    components = path.split(os.sep)
    if any(component in {"", ".", ".."} for component in components[1:]):
        raise _blocked()
    return tuple(components[1:])


def _relative_to_configured_root(
    target: tuple[str, ...], roots: tuple[tuple[str, ...], ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    for root in roots:
        if len(target) > len(root) and target[: len(root)] == root:
            return root, target[len(root) :]
    raise _blocked()


def _media_type_for_name(name: str, allowed_types: frozenset[str]) -> str:
    suffixes = Path(name).suffixes
    if len(suffixes) != 1:
        raise _blocked()
    media_type = _SUFFIX_MEDIA_TYPES.get(suffixes[0].lower())
    if media_type is None or media_type not in allowed_types:
        raise _blocked()
    return media_type


def _open_descriptor_relative(
    root: tuple[str, ...], relative: tuple[str, ...]
) -> int:
    directory_flags, leaf_flags = _required_open_flags()
    directory_fd: int | None = None
    try:
        directory_fd = os.open(os.sep, directory_flags)
        for component in (*root, *relative[:-1]):
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            previous_fd = directory_fd
            directory_fd = next_fd
            _close_quietly(previous_fd)
        return os.open(relative[-1], leaf_flags, dir_fd=directory_fd)
    finally:
        _close_quietly(directory_fd)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_descriptor(fd: int, maximum_bytes: int) -> int:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise _blocked()

    total = 0
    limit = maximum_bytes + 1
    while total < limit:
        chunk = os.read(fd, min(_READ_CHUNK_BYTES, limit - total))
        if not chunk:
            break
        total += len(chunk)

    after = os.fstat(fd)
    if (
        _metadata_identity(before) != _metadata_identity(after)
        or total != before.st_size
        or total == 0
        or total > maximum_bytes
    ):
        raise _blocked()
    if os.lseek(fd, 0, os.SEEK_SET) != 0:
        raise _blocked()
    return total


class GrokUploadPolicy:
    def __init__(
        self,
        roots: Sequence[Path],
        *,
        maximum_bytes: int = DEFAULT_UPLOAD_MAX_BYTES,
        allowed_types: AbstractSet[str] = DEFAULT_UPLOAD_TYPES,
    ) -> None:
        failure: GrokError | None = None
        normalized_roots: tuple[tuple[str, ...], ...] = ()
        normalized_types: frozenset[str] = frozenset()
        try:
            if (
                isinstance(maximum_bytes, bool)
                or not isinstance(maximum_bytes, int)
                or not 1 <= maximum_bytes <= DEFAULT_UPLOAD_MAX_BYTES
            ):
                raise _blocked()
            if not isinstance(allowed_types, Set):
                raise _blocked()
            normalized_types = frozenset(allowed_types)
            if (
                not all(isinstance(value, str) for value in normalized_types)
                or not normalized_types <= DEFAULT_UPLOAD_TYPES
            ):
                raise _blocked()
            normalized_roots = _validated_root_parts(roots)
        except Exception:
            failure = _blocked()
        if failure is not None:
            raise failure

        self._roots = normalized_roots
        self._maximum_bytes = maximum_bytes
        self._allowed_types = normalized_types

    def open(self, path: str) -> ValidatedUpload:
        """Open one root-contained file; the caller owns the returned descriptor."""
        failure: GrokError | None = None
        result: ValidatedUpload | None = None
        try:
            target = _validated_target_parts(path)
            root, relative = _relative_to_configured_root(target, self._roots)
            name = relative[-1]
            media_type = _media_type_for_name(name, self._allowed_types)
            fd = _open_descriptor_relative(root, relative)
            keep_fd = False
            try:
                size = _validate_descriptor(fd, self._maximum_bytes)
                result = ValidatedUpload(fd=fd, name=name, media_type=media_type, size=size)
                keep_fd = True
            finally:
                if not keep_fd:
                    _close_quietly(fd)
        except Exception:
            failure = _blocked()
        if failure is not None:
            raise failure
        if result is None:  # pragma: no cover - defensive control-flow guard
            raise _blocked()
        return result


def _validated_generation(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _blocked()
    return value


def _validated_attachment_id(value: Any) -> str:
    if not isinstance(value, str) or _ATTACHMENT_ID_RE.fullmatch(value) is None:
        raise _blocked()
    return value


class AttachmentRegistry:
    def __init__(self) -> None:
        self._generation: int | None = None
        self._entries: OrderedDict[str, None] = OrderedDict()

    def record(self, auth_generation: int, attachment_id: str) -> None:
        failure: GrokError | None = None
        try:
            generation = _validated_generation(auth_generation)
            identifier = _validated_attachment_id(attachment_id)
            if self._generation != generation:
                if self._generation is not None and generation < self._generation:
                    raise _blocked()
                self._entries.clear()
                self._generation = generation
            if identifier not in self._entries:
                self._entries[identifier] = None
                if len(self._entries) > _REGISTRY_CAPACITY:
                    self._entries.popitem(last=False)
        except Exception:
            failure = _blocked()
        if failure is not None:
            raise failure

    def validate_many(
        self, auth_generation: int, attachment_ids: Sequence[str]
    ) -> tuple[str, ...]:
        failure: GrokError | None = None
        result: tuple[str, ...] | None = None
        try:
            generation = _validated_generation(auth_generation)
            if (
                isinstance(attachment_ids, (str, bytes, bytearray))
                or not isinstance(attachment_ids, Sequence)
                or len(attachment_ids) > _MAX_ATTACHMENTS_PER_REQUEST
            ):
                raise _blocked()
            values = tuple(_validated_attachment_id(value) for value in attachment_ids)
            if values and self._generation != generation:
                raise _blocked()
            if len(set(values)) != len(values) or any(value not in self._entries for value in values):
                raise _blocked()
            result = values
        except Exception:
            failure = _blocked()
        if failure is not None:
            raise failure
        if result is None:  # pragma: no cover - defensive control-flow guard
            raise _blocked()
        return result
