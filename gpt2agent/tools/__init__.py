from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gpt2agent.backend import BackendClient


def register_all(mcp, client: BackendClient, conv=None, *, model_catalog=None) -> None:
    """Register every backend tool on *mcp*."""
    # Keep package initialization dependency-free. Domain modules such as
    # model_catalog import focused helpers below this package; eager imports here
    # would pull capability registration back into partially initialized domain
    # modules and create a circular import.
    from gpt2agent.tools import (
        account,
        apps,
        automations,
        capabilities,
        codex,
        conversations,
        gpts,
        images,
        instructions,
        memory,
        plugins,
        sites,
        tools_features,
        work,
        writes,
    )

    account.register(mcp, client, model_catalog=model_catalog)
    memory.register(mcp, client)
    instructions.register(mcp, client)
    codex.register(mcp, client)
    gpts.register(mcp, client)
    conversations.register(mcp, client)
    apps.register(mcp, client)
    automations.register(mcp, client)
    plugins.register(mcp, client)
    work.register(mcp, client, model_catalog=model_catalog)
    sites.register(mcp, client)
    capabilities.register(mcp, client)
    writes.register(mcp, client)
    images.register(mcp, client, conv)
    tools_features.register(mcp, client, conv)
    from gpt2agent.resources import register as register_resources

    register_resources(mcp)
