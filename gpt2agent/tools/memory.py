from __future__ import annotations

from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._redact import redact
from gpt2agent.tools._validation import bounded_string, bounded_time_value


def normalize_memories(data: Any) -> list[dict]:
    adapter = "memory"
    if not isinstance(data, dict) or not isinstance(data.get("memories"), list):
        raise BackendContractError(adapter, "memories list envelope required")
    if len(data["memories"]) > 1_000:
        raise BackendContractError(adapter, "result exceeds 1000 items")
    result: list[dict] = []
    for raw in data["memories"]:
        if not isinstance(raw, dict):
            raise BackendContractError(adapter, "every item must be an object")
        timestamp = bounded_time_value(
            raw.get("created_timestamp"),
            adapter=adapter,
            field="created_timestamp",
        )
        result.append(
            {
                "id": bounded_string(
                    raw.get("id"), adapter=adapter, field="id", required=True
                ),
                "status": bounded_string(
                    raw.get("status"), adapter=adapter, field="status"
                ),
                "content": bounded_string(
                    raw.get("content") or "",
                    adapter=adapter,
                    field="content",
                    redact_value=True,
                    maximum=10_000,
                ),
                "created_timestamp": timestamp,
            }
        )
    return result


async def _fetch_memories(client: BackendClient) -> list[dict]:
    data = await async_get(
        client,
        "/backend-api/memories",
        target_path="/backend-api/memories",
    )
    return normalize_memories(data)


def register(mcp, client: BackendClient) -> None:
    @mcp.tool(annotations=tool_annotations("memory_list"))
    async def memory_list() -> list:
        """Return all ChatGPT memories (PII redacted)."""
        return await _fetch_memories(client)

    @mcp.tool(annotations=tool_annotations("memory_search"))
    async def memory_search(query: str) -> list:
        """Keyword search over ChatGPT memories. Returns matching entries (PII redacted)."""
        q = query.lower()
        matches = []
        for m in await _fetch_memories(client):
            content = redact(m.get("content") or "")
            if q in content.lower():
                matches.append(
                    {
                        "id": m.get("id"),
                        "content": content,
                        "created_timestamp": m.get("created_timestamp"),
                    }
                )
        return matches
