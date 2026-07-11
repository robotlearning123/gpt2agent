from __future__ import annotations

import re

from gpt2agent.errors import InputValidationError


_PATH_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


def validate_path_id(value: str, *, kind: str) -> str:
    """Require one bounded URL-safe path segment before interpolating an ID."""
    if not isinstance(value, str) or not _PATH_ID_RE.fullmatch(value):
        raise InputValidationError(
            f"invalid {kind}: expected one URL-safe identifier segment"
        )
    return value
