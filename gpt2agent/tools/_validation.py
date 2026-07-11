"""Shared bounded validation for v0.0.12 private-account adapters."""

from __future__ import annotations

import math
import string
from typing import Any

from gpt2agent.errors import BackendContractError, InputValidationError
from gpt2agent.tools._redact import redact


MAX_ACCOUNT_INTEGER = (1 << 63) - 1


def validate_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputValidationError(
            f"{name} must be an integer from {minimum} through {maximum}"
        )
    if value < minimum or value > maximum:
        raise InputValidationError(
            f"{name} must be an integer from {minimum} through {maximum}"
        )
    return value


def validate_cursor(
    value: Any, *, name: str = "cursor", adapter: str | None = None
) -> str | None:
    message = f"{name} must be a non-empty printable string up to 2048 characters"

    def invalid() -> None:
        if adapter is not None:
            raise BackendContractError(adapter, message)
        raise InputValidationError(message)

    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 2048:
        invalid()
    if any(character not in string.printable or character in "\r\n\t\x0b\x0c" for character in value):
        invalid()
    return value


def require_object(value: Any, *, adapter: str, invariant: str = "object envelope required") -> dict:
    if not isinstance(value, dict):
        raise BackendContractError(adapter, invariant)
    return value


def require_list(value: Any, *, adapter: str, invariant: str) -> list:
    if not isinstance(value, list):
        raise BackendContractError(adapter, invariant)
    return value


def bounded_string(
    value: Any,
    *,
    adapter: str,
    field: str,
    required: bool = False,
    redact_value: bool = False,
    maximum: int = 256,
) -> str | None:
    if value is None:
        if required:
            raise BackendContractError(adapter, f"{field} must be a usable string")
        return None
    if not isinstance(value, str) or (required and not value) or len(value) > maximum:
        raise BackendContractError(adapter, f"{field} must be a usable string up to {maximum} characters")
    result = redact(value) if redact_value else value
    return result if isinstance(result, str) else value


def bounded_string_list(
    value: Any,
    *,
    adapter: str,
    field: str,
    redact_values: bool = False,
    maximum_items: int = 100,
) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise BackendContractError(adapter, f"{field} must be a list")
    if len(value) > maximum_items:
        raise BackendContractError(adapter, f"{field} exceeds {maximum_items} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = bounded_string(
            item,
            adapter=adapter,
            field=field,
            redact_value=redact_values,
        )
        if normalized is not None:
            result.append(normalized)
    return result


def bounded_nonnegative_int(
    value: Any,
    *,
    adapter: str,
    field: str,
    maximum: int = MAX_ACCOUNT_INTEGER,
) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > maximum
    ):
        raise BackendContractError(
            adapter,
            f"{field} must be a non-negative integer no greater than {maximum}",
        )
    return value


def bounded_time_value(
    value: Any,
    *,
    adapter: str,
    field: str,
    maximum_string: int = 2_048,
) -> int | float | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BackendContractError(adapter, f"{field} must be a bounded time value")
    if isinstance(value, int):
        if -MAX_ACCOUNT_INTEGER <= value <= MAX_ACCOUNT_INTEGER:
            return value
        raise BackendContractError(adapter, f"{field} must be a bounded time value")
    if isinstance(value, float):
        if math.isfinite(value) and abs(value) <= MAX_ACCOUNT_INTEGER:
            return value
        raise BackendContractError(adapter, f"{field} must be a bounded time value")
    if isinstance(value, str):
        return bounded_string(
            value,
            adapter=adapter,
            field=field,
            redact_value=True,
            maximum=maximum_string,
        )
    raise BackendContractError(adapter, f"{field} must be a bounded time value")


def nullable_bool(value: Any, *, adapter: str, field: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise BackendContractError(adapter, f"{field} must be boolean or null")
