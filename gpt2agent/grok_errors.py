"""Closed, secret-safe error contract for Grok account integrations."""

from __future__ import annotations

import math
import re
from urllib.parse import urlsplit


GROK_ERROR_CODES = frozenset(
    {
        "GROK_BUILD_CLI_NOT_FOUND",
        "GROK_BUILD_AUTH_MISSING",
        "GROK_BUILD_QUOTA",
        "GROK_BUILD_TIMEOUT",
        "GROK_BUILD_OUTPUT_TOO_LARGE",
        "GROK_BUILD_FAILED",
        "GROK_WEB_AUTH_MISSING",
        "GROK_WEB_AUTH_EXPIRED",
        "GROK_WEB_RATE_LIMITED",
        "GROK_WEB_CONTRACT_CHANGED",
        "GROK_WEB_TIMEOUT",
        "GROK_WEB_OUTPUT_TOO_LARGE",
        "GROK_UPLOAD_BLOCKED",
        "GROK_WEB_FAILED",
    }
)

_MAX_METHOD_LENGTH = 16
_MAX_INPUT_ROUTE_LENGTH = 2_048
_METHOD_RE = re.compile(r"[A-Z]+")
_STATIC_ROUTE_TEMPLATES = frozenset(
    {
        "/rest/app-chat/conversations/new",
        "/rest/models",
        "/rest/modes",
    }
)
_DYNAMIC_ROUTE_TEMPLATES = (
    (
        re.compile(
            r"/rest/app-chat/conversations/reconnect-response-v2/[^/\s]+"
        ),
        "/rest/app-chat/conversations/reconnect-response-v2/{id}",
    ),
    (
        re.compile(
            r"/rest/app-chat/conversations/(?!new\Z|reconnect-response-v2\Z)[^/\s]+"
        ),
        "/rest/app-chat/conversations/{id}",
    ),
    (
        re.compile(r"/rest/app-chat/attachments/[^/\s]+"),
        "/rest/app-chat/attachments/{id}",
    ),
    (
        re.compile(r"/rest/app-chat/responses/[^/\s]+"),
        "/rest/app-chat/responses/{id}",
    ),
    (
        re.compile(r"/rest/app-chat/events/[^/\s]+"),
        "/rest/app-chat/events/{id}",
    ),
)


def normalize_grok_route(route: str | None) -> str | None:
    """Return a bounded endpoint label without queries or resource IDs."""
    if route is None:
        return None
    if not isinstance(route, str):
        raise ValueError("Grok error route must be a string or None")
    try:
        path = urlsplit(route).path
    except ValueError:
        return "<route>"
    if (
        len(path) > _MAX_INPUT_ROUTE_LENGTH
        or not path.startswith("/")
        or not path.isascii()
        or not path.isprintable()
        or "%" in path
    ):
        return "<route>"
    if path in _STATIC_ROUTE_TEMPLATES:
        return path
    for pattern, normalized in _DYNAMIC_ROUTE_TEMPLATES:
        if pattern.fullmatch(path) is not None:
            return normalized
    return "<route>"


class GrokError(RuntimeError):
    """A bounded failure containing only allowlisted operational metadata."""

    code: str
    method: str | None
    route: str | None
    status_code: int | None
    retryable: bool
    retry_after: float | None

    def __init__(
        self,
        code: str,
        *,
        method: str | None = None,
        route: str | None = None,
        status_code: int | None = None,
        retryable: bool,
        retry_after: float | None = None,
    ) -> None:
        if not isinstance(code, str) or code not in GROK_ERROR_CODES:
            raise ValueError("unsupported Grok error code")
        if method is not None:
            if not isinstance(method, str):
                raise ValueError("Grok error method must be a bounded HTTP method")
            method = method.strip().upper()
            if (
                not method
                or len(method) > _MAX_METHOD_LENGTH
                or _METHOD_RE.fullmatch(method) is None
            ):
                raise ValueError("Grok error method must be a bounded HTTP method")
        if status_code is not None and (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise ValueError("Grok error status must be an HTTP status code")
        if not isinstance(retryable, bool):
            raise ValueError("Grok error retryable flag must be boolean")
        if retry_after is not None:
            if (
                isinstance(retry_after, bool)
                or not isinstance(retry_after, (int, float))
                or not math.isfinite(retry_after)
            ):
                raise ValueError("Grok error retry timing must be finite")
            retry_after = min(60.0, max(0.0, float(retry_after)))

        self.code = code
        self.method = method
        self.route = normalize_grok_route(route)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after

        context = " ".join(value for value in (self.method, self.route) if value)
        message = f"{self.code}: {context + ' ' if context else ''}failed"
        if self.status_code is not None:
            message += f" ({self.status_code})"
        retry_metadata: list[str] = []
        if self.retryable:
            retry_metadata.append("retryable")
        if self.retry_after is not None:
            retry_metadata.append(f"retry after {self.retry_after:g}s")
        if retry_metadata:
            message += f" [{'; '.join(retry_metadata)}]"
        super().__init__(message)
