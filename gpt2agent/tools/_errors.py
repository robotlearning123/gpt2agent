"""Fail-closed serialization at the MCP tool boundary."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import UrlElicitationRequiredError
from mcp.types import ContentBlock

from gpt2agent.errors import (
    BackendContractError,
    BackendHTTPError,
    InputValidationError,
)


def serialize_tool_error(error: BaseException) -> str:
    """Expose only typed, content-free failures to MCP clients.

    FastMCP wraps handler exceptions in ``ToolError`` and keeps the original as
    ``__cause__``. Walk that short chain so the server can retain the bounded
    metadata from our typed backend errors. Everything else fails closed: a
    generic exception may contain prompts, account data, response bodies, or
    identifiers and therefore must never be stringified at this boundary.
    """
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current, (BackendHTTPError, BackendContractError, InputValidationError)
        ):
            return str(current)
        current = current.__cause__
    return "unavailable: tool execution failed"


class SafeFastMCP(FastMCP):
    """FastMCP server that never reflects arbitrary exception text."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            return await super().call_tool(name, arguments)
        except UrlElicitationRequiredError:
            # Preserve the SDK's dedicated -32042 control flow for tools that
            # require URL-mode authorization before they can continue.
            raise
        except Exception as exc:
            raise ToolError(serialize_tool_error(exc)) from None
