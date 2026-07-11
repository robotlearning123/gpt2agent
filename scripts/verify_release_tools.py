#!/usr/bin/env python3
"""Validate immutable operator tools and reviewed local release inputs."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path


_MAX_POLICY_BYTES = 4 * 1024 * 1024
_TOOL_DIRECTORIES = frozenset({Path("/usr/bin"), Path("/usr/local/bin")})


class ToolValidationError(ValueError):
    """A required release tool or input is not trusted."""


def _canonical(path: Path, label: str) -> tuple[Path, os.stat_result]:
    if not path.is_absolute():
        raise ToolValidationError(f"{label} path is not absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError:
        raise ToolValidationError(f"{label} is unavailable") from None
    if resolved != path or stat.S_ISLNK(metadata.st_mode):
        raise ToolValidationError(f"{label} path is not canonical")
    return resolved, metadata


def validate_tool(path: Path, expected_name: str) -> None:
    """Require one canonical root-owned executable in a protected /usr bin dir."""
    resolved, metadata = _canonical(path, expected_name)
    if (
        resolved.name != expected_name
        or resolved.parent not in _TOOL_DIRECTORIES
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o6022
        or not metadata.st_mode & 0o111
    ):
        raise ToolValidationError(f"{expected_name} is not a trusted system executable")
    for parent in (resolved.parent, *resolved.parent.parents):
        try:
            parent_metadata = parent.lstat()
        except OSError:
            raise ToolValidationError(f"{expected_name} parent path is unavailable") from None
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != 0
            or parent_metadata.st_mode & 0o022
        ):
            raise ToolValidationError(f"{expected_name} parent path is not protected")


def validate_policy(path: Path) -> None:
    """Require one canonical, non-linked, owner-controlled reviewed policy file."""
    _, metadata = _canonical(path, "governance policy")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
        or metadata.st_size < 2
        or metadata.st_size > _MAX_POLICY_BYTES
    ):
        raise ToolValidationError("governance policy is not a protected regular file")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--gh", type=Path, required=True)
    check.add_argument("--git", type=Path, required=True)
    check.add_argument("--policy", type=Path)
    args = parser.parse_args(argv)
    try:
        validate_tool(args.gh, "gh")
        validate_tool(args.git, "git")
        if args.policy is not None:
            validate_policy(args.policy)
    except ToolValidationError as exc:
        print(f"release prerequisite validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
