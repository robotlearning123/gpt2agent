"""Safe, machine-readable failures for private ChatGPT account adapters."""

from __future__ import annotations

from urllib.parse import urlsplit


ERROR_CODES = frozenset(
    {
        "unavailable",
        "unsupported",
        "contract_changed",
        "temporarily_failed",
        "access_indeterminate",
        "invalid_input",
        "login_required",
        "unverified",
    }
)


def normalize_route(route: str) -> str:
    """Return a query-free route with known account identifiers removed."""
    try:
        parsed = urlsplit(route)
        path = parsed.path or "/"
    except (TypeError, ValueError):
        path = "/unknown"
    if not path.startswith("/"):
        path = "/" + path
    parts = path.split("/")
    # Current dynamic account routes place opaque IDs in these exact slots.
    # Preserve stable suffixes (such as /download) while never retaining the
    # conversation/file identifier in an exception or cooldown key.
    if len(parts) > 3 and parts[1:3] == ["backend-api", "conversation"]:
        if parts[3] not in {"init"}:
            parts[3] = "{id}"
    elif len(parts) > 3 and parts[1:3] == ["backend-api", "files"]:
        parts[3] = "{id}"
    path = "/".join(parts)
    if len(path) > 256:
        path = path[:256]
    return path


class BackendHTTPError(RuntimeError):
    """An HTTP failure containing only bounded, non-content metadata."""

    def __init__(
        self,
        method: str,
        route: str,
        status_code: int | None,
        *,
        code: str | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        resolved = code or "temporarily_failed"
        if resolved not in ERROR_CODES:
            raise ValueError("unknown backend error code")
        self.method = str(method).upper()[:12]
        self.route = normalize_route(route)
        self.status_code = status_code if isinstance(status_code, int) else None
        self.code = resolved
        self.retryable = bool(retryable)
        self.retry_after = (
            max(0.0, min(float(retry_after), 60.0))
            if isinstance(retry_after, (int, float))
            else None
        )
        status = str(self.status_code) if self.status_code is not None else "network"
        guidance = "; retry later" if self.retryable else ""
        if self.retry_after is not None:
            guidance = f"; retry after {self.retry_after:g}s"
        super().__init__(f"{self.code}: {self.method} {self.route} failed ({status}){guidance}")


def backend_http_error(
    method: str,
    route: str,
    status_code: int,
    *,
    established: bool = True,
    fixed_probe: bool = False,
    retry_after: float | None = None,
) -> BackendHTTPError:
    """Map one HTTP status to the release's conservative public error contract."""
    if status_code == 401:
        code, retryable = "login_required", False
    elif status_code == 403:
        code, retryable = "access_indeterminate", False
    elif status_code in (404, 405):
        code, retryable = (
            ("contract_changed", False)
            if established
            else ("unsupported", False)
        )
    elif status_code == 422:
        code, retryable = (
            ("contract_changed", False)
            if fixed_probe
            else ("invalid_input", False)
        )
    elif status_code == 429 or status_code >= 500 or status_code in (408, 425):
        code, retryable = "temporarily_failed", True
    else:
        code, retryable = "contract_changed", False
    return BackendHTTPError(
        method,
        route,
        status_code,
        code=code,
        retryable=retryable,
        retry_after=retry_after,
    )


class BackendContractError(RuntimeError):
    """A minimum-schema failure with a static adapter/invariant description."""

    code = "contract_changed"
    retryable = False

    def __init__(self, adapter: str, invariant: str) -> None:
        self.adapter = str(adapter)[:80]
        self.invariant = str(invariant)[:200]
        super().__init__(f"contract_changed: {self.adapter}: {self.invariant}")


class InputValidationError(ValueError):
    """A static, content-free caller-input failure safe for MCP clients."""

    code = "invalid_input"
    retryable = False

    def __init__(self, invariant: str) -> None:
        self.invariant = str(invariant)[:200]
        super().__init__(f"invalid_input: {self.invariant}")
