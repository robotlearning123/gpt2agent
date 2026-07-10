from __future__ import annotations

from gpt2agent.backend import BackendClient
from gpt2agent.tools import (
    account,
    apps,
    codex,
    conversations,
    gpts,
    images,
    instructions,
    memory,
    tools_features,
    voice,
    writes,
)


def register_all(mcp, client: BackendClient, conv=None) -> None:
    """Register every backend tool on *mcp*."""
    account.register(mcp, client)
    memory.register(mcp, client)
    instructions.register(mcp, client)
    codex.register(mcp, client)
    gpts.register(mcp, client)
    conversations.register(mcp, client)
    apps.register(mcp, client)
    voice.register(mcp, client)
    writes.register(mcp, client)
    images.register(mcp, client, conv)
    tools_features.register(mcp, client, conv)
