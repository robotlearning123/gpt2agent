from __future__ import annotations

from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._validation import bounded_string, nullable_bool, validate_cursor


def _nullable_time_string(value: Any, *, field: str) -> str | None:
    return bounded_string(
        value,
        adapter="automations",
        field=field,
        redact_value=True,
        maximum=2_048,
    )


def _nullable_next_run_times(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise BackendContractError(
            "automations", "next_run_times must be an array or null"
        )
    # The observed contract proves only an array. Preserve that minimum
    # acceptance while projecting a bounded, useful subset: nested/unknown
    # values and oversized strings are ignored rather than passed through.
    result: list[str] = []
    for entry in value:
        if len(result) >= 100:
            break
        if not isinstance(entry, str) or len(entry) > 2_048:
            continue
        normalized = bounded_string(
            entry,
            adapter="automations",
            field="next_run_times entry",
            redact_value=True,
            maximum=2_048,
        )
        assert normalized is not None
        result.append(normalized)
    return result


def normalize_scheduled_page(data: Any) -> dict:
    adapter = "automations"
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise BackendContractError(adapter, "items list envelope required")
    if len(data["items"]) > 100:
        raise BackendContractError(adapter, "page exceeds 100 items")
    cursor = validate_cursor(data.get("cursor"), adapter=adapter)
    items: list[dict] = []
    for raw in data["items"]:
        if not isinstance(raw, dict):
            raise BackendContractError(adapter, "every item must be an object")
        item_id = bounded_string(raw.get("id"), adapter=adapter, field="id", required=True)
        next_runs = _nullable_next_run_times(raw.get("next_run_times"))
        items.append(
            {
                "id": item_id,
                "updated_at": _nullable_time_string(
                    raw.get("updated_at"), field="updated_at"
                ),
                "next_run_times": next_runs,
                "is_enabled": nullable_bool(
                    raw.get("is_enabled"), adapter=adapter, field="is_enabled"
                ),
                "target_time_utc": _nullable_time_string(
                    raw.get("target_time_utc"), field="target_time_utc"
                ),
            }
        )
    return {"items": items, "cursor": cursor}


def register(mcp, client: BackendClient, conv=None) -> None:
    @mcp.tool(annotations=tool_annotations("list_scheduled_tasks"))
    async def list_scheduled_tasks(cursor: str | None = None) -> dict:
        """Return one page of scheduled ChatGPT automations."""
        cursor = validate_cursor(cursor)
        params: dict[str, str] = {"filter": "scheduled"}
        if cursor is not None:
            params["cursor"] = cursor
        data = await async_get(
            client,
            "/backend-api/automations",
            params=params,
            target_path="/backend-api/automations",
            fixed_probe=False,
        )
        return normalize_scheduled_page(data)
