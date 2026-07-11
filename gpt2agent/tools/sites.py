from __future__ import annotations

from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._validation import (
    bounded_nonnegative_int,
    bounded_string,
    nullable_bool,
    validate_cursor,
    validate_int,
)


def normalize_sites_access(data: Any) -> dict:
    if not isinstance(data, dict):
        raise BackendContractError("sites_access", "object envelope required")
    return {
        field: nullable_bool(data.get(field), adapter="sites_access", field=field)
        for field in ("enabled", "custom_domains_enabled", "requires_workspace_slug")
    }


def _nullable_count(value: Any, *, field: str) -> int | None:
    return bounded_nonnegative_int(value, adapter="list_sites", field=field)


def normalize_sites_page(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise BackendContractError("list_sites", "items list envelope required")
    if len(data["items"]) > 100:
        raise BackendContractError("list_sites", "page exceeds 100 items")
    items: list[dict] = []
    for raw in data["items"]:
        if not isinstance(raw, dict):
            raise BackendContractError("list_sites", "site item must be an object")
        sharing = raw.get("sharing")
        if sharing is not None and not isinstance(sharing, dict):
            raise BackendContractError("list_sites", "sharing must be an object or null")
        sharing = sharing or {}
        items.append(
            {
                "id": bounded_string(
                    raw.get("id"), adapter="list_sites", field="id", required=True
                ),
                "title": bounded_string(
                    raw.get("title"),
                    adapter="list_sites",
                    field="title",
                    redact_value=True,
                ),
                "slug": bounded_string(
                    raw.get("slug"), adapter="list_sites", field="slug", redact_value=True
                ),
                "status": bounded_string(
                    raw.get("status"), adapter="list_sites", field="status"
                ),
                "updated_at": bounded_string(
                    raw.get("updated_at"),
                    adapter="list_sites",
                    field="updated_at",
                    redact_value=True,
                ),
                "disabled_by": bounded_string(
                    raw.get("disabled_by"),
                    adapter="list_sites",
                    field="disabled_by",
                    redact_value=True,
                ),
                "sharing": {
                    "access_mode": bounded_string(
                        sharing.get("access_mode"),
                        adapter="list_sites",
                        field="sharing.access_mode",
                    ),
                    "user_count": _nullable_count(
                        sharing.get("user_count"), field="sharing.user_count"
                    ),
                    "group_count": _nullable_count(
                        sharing.get("group_count"), field="sharing.group_count"
                    ),
                },
                "has_live_url": bool(raw.get("live_url")),
                "has_preview": bool(raw.get("preview_url")),
                "has_screenshot": bool(raw.get("screenshot_url")),
            }
        )
    return {
        "items": items,
        "cursor": validate_cursor(data.get("cursor"), adapter="list_sites"),
    }


def register(mcp, client: BackendClient) -> None:
    @mcp.tool(annotations=tool_annotations("sites_access"))
    async def sites_access() -> dict:
        """Return the non-identifying Sites access booleans for this account."""
        data = await async_get(
            client,
            "/backend-api/websites/access",
            target_path="/backend-api/websites/access",
            fixed_probe=True,
        )
        return normalize_sites_access(data)

    @mcp.tool(annotations=tool_annotations("list_sites"))
    async def list_sites(limit: int = 20, cursor: str | None = None) -> dict:
        """Return one bounded Sites page without content or private URLs."""
        limit = validate_int(limit, name="limit", minimum=1, maximum=100)
        cursor = validate_cursor(cursor)
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["after"] = cursor
        data = await async_get(
            client,
            "/backend-api/websites",
            params=params,
            target_path="/backend-api/websites",
            fixed_probe=False,
        )
        page = normalize_sites_page(data)
        if len(page["items"]) > limit:
            raise BackendContractError("list_sites", "page exceeds requested limit")
        return page
