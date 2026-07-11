from __future__ import annotations

from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._validation import bounded_string


def normalize_custom_gpts(data: Any) -> list[dict]:
    adapter = "custom_gpts"
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise BackendContractError(adapter, "items list envelope required")
    if len(data["items"]) > 1_000:
        raise BackendContractError(adapter, "catalog exceeds 1000 items")
    result: list[dict] = []
    for raw in data["items"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("gizmo"), dict):
            raise BackendContractError(adapter, "every item must contain a gizmo object")
        gizmo = raw["gizmo"]
        result.append(
            {
                "name": bounded_string(
                    gizmo.get("name") or "",
                    adapter=adapter,
                    field="name",
                    redact_value=True,
                    maximum=512,
                ),
                "short_url": bounded_string(
                    gizmo.get("short_url"),
                    adapter=adapter,
                    field="short_url",
                    required=True,
                    maximum=512,
                ),
            }
        )
    return result


def register(mcp, client: BackendClient) -> None:
    @mcp.tool(annotations=tool_annotations("list_custom_gpts"))
    async def list_custom_gpts() -> list:
        """List your private Custom GPTs from the ChatGPT sidebar.

        Returns a list of ``{"name", "short_url"}``. Pass a returned
        ``short_url`` as the ``gizmo_id`` argument of ``gpt_chat`` to talk to
        that Custom GPT.
        """
        data = await async_get(
            client,
            "/backend-api/gizmos/snorlax/sidebar",
            target_path="/backend-api/gizmos/snorlax/sidebar",
        )
        return normalize_custom_gpts(data)
