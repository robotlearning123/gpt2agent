from __future__ import annotations

import math
from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError
from gpt2agent.message_visibility import is_user_visible_message
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._ids import validate_path_id
from gpt2agent.tools._redact import redact
from gpt2agent.tools._validation import bounded_string, nullable_bool, validate_int

_MAX_SAFE_INTEGER = (1 << 63) - 1


def _bounded_scalar(value: Any, *, adapter: str, field: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) <= _MAX_SAFE_INTEGER:
            return value
        raise BackendContractError(adapter, f"{field} must be a bounded scalar or null")
    if isinstance(value, float):
        if math.isfinite(value) and abs(value) <= _MAX_SAFE_INTEGER:
            return value
        raise BackendContractError(adapter, f"{field} must be a finite scalar or null")
    if isinstance(value, str):
        return bounded_string(
            value,
            adapter=adapter,
            field=field,
            redact_value=True,
            maximum=2_048,
        )
    raise BackendContractError(adapter, f"{field} must be a bounded scalar or null")


def _nullable_nonnegative_int(value: Any, *, adapter: str, field: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_SAFE_INTEGER
    ):
        raise BackendContractError(adapter, f"{field} must be a non-negative integer or null")
    return value


def normalize_conversation_detail(
    data: Any,
    *,
    expected_id: str,
    max_messages: int,
) -> dict:
    """Project a private conversation response onto the documented safe shape."""
    adapter = "conversation_detail"
    if not isinstance(data, dict):
        raise BackendContractError(adapter, "conversation object response required")
    if not data:
        return {}

    returned_id = bounded_string(
        data.get("id"), adapter=adapter, field="id", maximum=2_048
    )
    if returned_id is not None and returned_id != expected_id:
        raise BackendContractError(adapter, "response id does not match request")

    raw_mapping = data.get("mapping")
    if raw_mapping is None:
        mapping: dict = {}
    elif isinstance(raw_mapping, dict):
        mapping = raw_mapping
    else:
        raise BackendContractError(adapter, "mapping object required")

    # Walk the active branch from the leaf up to the root, then reverse to
    # chronological order. If there is no active node, sort the visible nodes
    # using a type-stable scalar key so malformed nested values are never
    # compared or echoed by Python's error machinery.
    ordered_nodes: list[dict] = []
    current = data.get("current_node")
    if current is not None:
        current = bounded_string(
            current, adapter=adapter, field="current_node", maximum=2_048
        )
    if current and current in mapping:
        seen_ids: set[str] = set()
        node_id: str | None = current
        while node_id and node_id in mapping and node_id not in seen_ids:
            seen_ids.add(node_id)
            node = mapping[node_id]
            if not isinstance(node, dict):
                raise BackendContractError(adapter, "active mapping node must be an object")
            ordered_nodes.append(node)
            parent = node.get("parent")
            node_id = bounded_string(
                parent, adapter=adapter, field="parent", maximum=2_048
            )
        ordered_nodes.reverse()
    else:
        indexed_nodes = [
            (index, node)
            for index, node in enumerate(mapping.values())
            if isinstance(node, dict)
        ]

        def sort_key(indexed_node: tuple[int, dict]) -> tuple[int, float | str, int]:
            index, node = indexed_node
            message = node.get("message")
            value = message.get("create_time") if isinstance(message, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if isinstance(value, int) or math.isfinite(value):
                    return (0, float(value), index)
            if isinstance(value, str) and len(value) <= 2_048:
                return (1, value, index)
            return (2, "", index)

        ordered_nodes = [node for _, node in sorted(indexed_nodes, key=sort_key)]

    messages: list[dict] = []
    for node in ordered_nodes:
        message = node.get("message")
        if message is None:
            continue
        if not isinstance(message, dict):
            raise BackendContractError(adapter, "message must be an object or null")
        if not is_user_visible_message(message):
            continue
        author = message.get("author")
        if not isinstance(author, dict):
            raise BackendContractError(adapter, "message author object required")
        role = bounded_string(
            author.get("role"), adapter=adapter, field="role", maximum=64
        )
        if role not in ("user", "assistant", "tool"):
            continue

        content = message.get("content")
        if content is None:
            content = {}
        if not isinstance(content, dict):
            raise BackendContractError(adapter, "message content must be an object")
        content_type = bounded_string(
            content.get("content_type"),
            adapter=adapter,
            field="content_type",
            maximum=128,
        )
        parts = content.get("parts")
        if parts is None:
            parts = []
        if not isinstance(parts, list) or len(parts) > 100:
            raise BackendContractError(
                adapter, "message parts must be an array of at most 100 items"
            )

        entry = {
            "id": bounded_string(
                message.get("id"),
                adapter=adapter,
                field="message id",
                redact_value=True,
                maximum=2_048,
            ),
            "role": role,
            "recipient": bounded_string(
                message.get("recipient"),
                adapter=adapter,
                field="recipient",
                redact_value=True,
                maximum=2_048,
            ),
            "content_type": content_type,
            "status": bounded_string(
                message.get("status"),
                adapter=adapter,
                field="status",
                redact_value=True,
                maximum=2_048,
            ),
            "create_time": _bounded_scalar(
                message.get("create_time"), adapter=adapter, field="create_time"
            ),
        }
        if content_type in ("text", "multimodal_text") and parts:
            string_parts = [part for part in parts if isinstance(part, str)]
            if string_parts:
                # Redact before truncating so a secret straddling the boundary
                # cannot survive as a partial token.
                entry["text"] = redact(string_parts[0])[:2000]
            image_parts = [
                part
                for part in parts
                if isinstance(part, dict)
                and part.get("content_type") == "image_asset_pointer"
            ]
            if image_parts:
                entry["images"] = [
                    {
                        "asset_pointer": bounded_string(
                            part.get("asset_pointer"),
                            adapter=adapter,
                            field="asset_pointer",
                            required=True,
                            redact_value=True,
                            maximum=2_048,
                        ),
                        "width": _nullable_nonnegative_int(
                            part.get("width"), adapter=adapter, field="width"
                        ),
                        "height": _nullable_nonnegative_int(
                            part.get("height"), adapter=adapter, field="height"
                        ),
                    }
                    for part in image_parts
                ]
        elif content_type == "code" and parts and isinstance(parts[0], str):
            entry["code"] = redact(parts[0])[:500]
        messages.append(entry)

    messages = messages[-max_messages:]
    return {
        "id": expected_id,
        "title": bounded_string(
            data.get("title") or "",
            adapter=adapter,
            field="title",
            redact_value=True,
            maximum=4_096,
        ),
        "create_time": _bounded_scalar(
            data.get("create_time"), adapter=adapter, field="create_time"
        ),
        "update_time": _bounded_scalar(
            data.get("update_time"), adapter=adapter, field="update_time"
        ),
        "message_count": len(messages),
        "messages": messages,
    }


def normalize_conversation_summaries(data: Any, *, limit: int) -> list[dict]:
    adapter = "conversations"
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise BackendContractError(adapter, "items list envelope required")
    if len(data["items"]) > 100:
        raise BackendContractError(adapter, "result exceeds 100 items")
    result: list[dict] = []
    for raw in data["items"][:limit]:
        if not isinstance(raw, dict):
            raise BackendContractError(adapter, "every item must be an object")
        result.append(
            {
                "id": bounded_string(
                    raw.get("id"), adapter=adapter, field="id", required=True
                ),
                "title": bounded_string(
                    raw.get("title") or "",
                    adapter=adapter,
                    field="title",
                    redact_value=True,
                    maximum=4_096,
                ),
                "update_time": _bounded_scalar(
                    raw.get("update_time"), adapter=adapter, field="update_time"
                ),
                "is_archived": nullable_bool(
                    raw.get("is_archived"), adapter=adapter, field="is_archived"
                ),
                "gizmo_id": bounded_string(
                    raw.get("gizmo_id"), adapter=adapter, field="gizmo_id"
                ),
            }
        )
    return result


def normalize_background_tasks(data: Any, *, limit: int) -> list[dict]:
    adapter = "background_jobs"
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise BackendContractError(adapter, "tasks list envelope required")
    if len(data["tasks"]) > 100:
        raise BackendContractError(adapter, "result exceeds 100 items")
    result: list[dict] = []
    for raw in data["tasks"][:limit]:
        if not isinstance(raw, dict):
            raise BackendContractError(adapter, "every item must be an object")
        result.append(
            {
                "task_id": bounded_string(
                    raw.get("task_id"), adapter=adapter, field="task_id", required=True
                ),
                "title": bounded_string(
                    raw.get("title") or "",
                    adapter=adapter,
                    field="title",
                    redact_value=True,
                    maximum=4_096,
                ),
                "status": bounded_string(
                    raw.get("status"), adapter=adapter, field="status"
                ),
                "created_at": _bounded_scalar(
                    raw.get("created_at"), adapter=adapter, field="created_at"
                ),
                "updated_at": _bounded_scalar(
                    raw.get("updated_at"), adapter=adapter, field="updated_at"
                ),
                "prompt": bounded_string(
                    raw.get("prompt") or "",
                    adapter=adapter,
                    field="prompt",
                    redact_value=True,
                    maximum=10_000,
                ),
                "conversation_id": bounded_string(
                    raw.get("conversation_id"),
                    adapter=adapter,
                    field="conversation_id",
                ),
                "image_gen_message": raw.get("image_gen_message") is not None,
                "interruptions_disabled": nullable_bool(
                    raw.get("interruptions_disabled"),
                    adapter=adapter,
                    field="interruptions_disabled",
                ),
            }
        )
    return result


def register(mcp, client: BackendClient) -> None:
    @mcp.tool(annotations=tool_annotations("list_conversations"))
    async def list_conversations(limit: int = 20) -> list:
        """Return recent ChatGPT conversations (titles PII-redacted)."""
        limit = validate_int(limit, name="limit", minimum=1, maximum=100)
        data = await async_get(
            client,
            f"/backend-api/conversations?offset=0&limit={limit}&order=updated",
            target_path="/backend-api/conversations",
        )
        return normalize_conversation_summaries(data, limit=limit)

    @mcp.tool(annotations=tool_annotations("get_conversation"))
    async def get_conversation(conversation_id: str, max_messages: int = 100) -> dict:
        """Get full details of a ChatGPT conversation including all messages.

        Args:
            conversation_id: The conversation ID.
            max_messages: Maximum number of messages to return (default 100).

        Returns:
            Dict with allowlisted metadata and a bounded chronological messages list.
        """
        conversation_id = validate_path_id(conversation_id, kind="conversation ID")
        max_messages = validate_int(
            max_messages, name="max_messages", minimum=1, maximum=100
        )
        data = await async_get(client, f"/backend-api/conversation/{conversation_id}")
        return normalize_conversation_detail(
            data, expected_id=conversation_id, max_messages=max_messages
        )

    @mcp.tool(annotations=tool_annotations("list_tasks"))
    async def list_tasks(limit: int = 20) -> list:
        """Return background/asynchronous ChatGPT jobs with PII-redacted text."""
        limit = validate_int(limit, name="limit", minimum=1, maximum=100)
        data = await async_get(
            client,
            "/backend-api/tasks",
            params={"limit": limit},
            target_path="/backend-api/tasks",
        )
        return normalize_background_tasks(data, limit=limit)
