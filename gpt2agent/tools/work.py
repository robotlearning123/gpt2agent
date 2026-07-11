from __future__ import annotations

from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError
from gpt2agent.model_catalog import ModelCatalog, normalize_work_models
from gpt2agent.tool_contracts import tool_annotations


def normalize_work_page(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise BackendContractError("work_models", "models list envelope required")
    return {"items": normalize_work_models(data["models"])}


def register(
    mcp,
    client: BackendClient,
    *,
    model_catalog: ModelCatalog | None = None,
) -> None:
    catalog = model_catalog or ModelCatalog(client)

    @mcp.tool(annotations=tool_annotations("list_work_models"))
    async def list_work_models() -> dict:
        """Return the account-visible Work catalog without merging it into Chat models."""
        return {"items": await catalog.work()}
