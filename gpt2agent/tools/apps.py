from __future__ import annotations

from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._validation import bounded_string, nullable_bool


def _classify(app_id: str) -> str:
    if app_id.startswith("connector_"):
        return "official_connector"
    if app_id.startswith("asdk_app_"):
        return "third_party_sdk"
    return "unknown"


def normalize_apps(data: Any) -> list[dict]:
    """Return the exact bounded projection exposed by ``list_apps``."""
    adapter = "apps"
    if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
        raise BackendContractError(adapter, "apps list envelope required")
    if len(data["apps"]) > 1_000:
        raise BackendContractError(adapter, "catalog exceeds 1000 items")

    result: list[dict] = []
    for entry in data["apps"]:
        try:
            if isinstance(entry, str):
                app_id = bounded_string(
                    entry,
                    adapter=adapter,
                    field="id",
                    required=True,
                )
                enabled = None
                connected = None
            elif isinstance(entry, dict):
                app_id = bounded_string(
                    entry.get("id"),
                    adapter=adapter,
                    field="id",
                    required=True,
                )
                enabled = nullable_bool(
                    entry.get("enabled"), adapter=adapter, field="enabled"
                )
                # Key presence matters: a disconnected app must not fall
                # through to the legacy key or to None.
                connected = nullable_bool(
                    entry["is_connected"]
                    if "is_connected" in entry
                    else entry.get("connected"),
                    adapter=adapter,
                    field="connected",
                )
            else:
                continue
        except BackendContractError:
            # The mixed Apps catalog is explicitly a skip-invalid adapter.
            continue
        result.append(
            {
                "id": app_id,
                "type": _classify(app_id),
                "enabled": enabled,
                "connected": connected,
            }
        )
    return result


def register(mcp, client: BackendClient) -> None:
    @mcp.tool(annotations=tool_annotations("list_apps"))
    async def list_apps() -> list:
        """Return ChatGPT connected apps/connectors. Names unresolvable — IDs with type classification returned."""
        data = await async_get(
            client,
            "/backend-api/apps/list",
            target_path="/backend-api/apps/list",
        )
        return normalize_apps(data)
