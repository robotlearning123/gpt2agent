from __future__ import annotations

from gpt2agent.backend import BackendClient
from gpt2agent.capabilities import build_account_capabilities
from gpt2agent.tool_contracts import tool_annotations


def register(mcp, client: BackendClient) -> None:
    @mcp.tool(annotations=tool_annotations("account_capabilities"))
    async def account_capabilities() -> dict:
        """Return a shape-only, typed inventory of account capabilities."""
        return await build_account_capabilities(client)
