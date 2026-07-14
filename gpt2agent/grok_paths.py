"""Configured-root path policy shared by Grok local-file surfaces."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .errors import InputValidationError


_CWD_INVARIANT = (
    "grok_build.cwd must be an existing directory under a configured root"
)


class RootPolicy:
    def __init__(self, roots: Sequence[Path]) -> None:
        self.roots = tuple(path.expanduser().resolve() for path in roots)

    def directory(self, value: str | Path | None) -> Path:
        """Return one existing directory contained by a configured root."""
        if not self.roots:
            raise InputValidationError(_CWD_INVARIANT)
        try:
            candidate = Path.cwd() if value is None else Path(value).expanduser()
            resolved = candidate.resolve(strict=True)
            is_directory = resolved.is_dir()
        except (OSError, RuntimeError, TypeError, ValueError):
            raise InputValidationError(_CWD_INVARIANT) from None
        if not is_directory or not any(
            resolved == root or root in resolved.parents for root in self.roots
        ):
            raise InputValidationError(_CWD_INVARIANT)
        return resolved
