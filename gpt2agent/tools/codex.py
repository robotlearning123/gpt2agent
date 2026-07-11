from __future__ import annotations

from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._validation import bounded_string, validate_int


def normalize_codex_environments(data: Any) -> list[dict]:
    adapter = "codex"
    if isinstance(data, list):
        envs = data
    elif isinstance(data, dict) and isinstance(data.get("environments"), list):
        envs = data["environments"]
    else:
        raise BackendContractError(adapter, "environments list envelope required")
    if len(envs) > 1_000:
        raise BackendContractError(adapter, "result exceeds 1000 items")
    result: list[dict] = []
    for raw in envs:
        if not isinstance(raw, dict):
            raise BackendContractError(adapter, "every environment must be an object")
        repos = raw.get("repos")
        if repos is not None and not isinstance(repos, list):
            raise BackendContractError(adapter, "repos must be an array or null")
        raw_network_access = raw.get("agent_network_access")
        if raw_network_access is not None and not isinstance(
            raw_network_access, (bool, str)
        ):
            raise BackendContractError(adapter, "agent_network_access must be scalar or null")
        network_access = (
            bounded_string(
                raw_network_access,
                adapter=adapter,
                field="agent_network_access",
                redact_value=True,
                maximum=256,
            )
            if isinstance(raw_network_access, str)
            else raw_network_access
        )
        result.append(
            {
                "id": bounded_string(
                    raw.get("id"), adapter=adapter, field="id", required=True
                ),
                "label": bounded_string(
                    raw.get("label"),
                    adapter=adapter,
                    field="label",
                    redact_value=True,
                    maximum=1_024,
                ),
                "workspace_dir": bounded_string(
                    raw.get("workspace_dir"),
                    adapter=adapter,
                    field="workspace_dir",
                    redact_value=True,
                    maximum=4_096,
                ),
                "agent_network_access": network_access,
                "repo_count": len(repos or []),
            }
        )
    return result


def normalize_codex_tasks(data: Any, *, limit: int) -> list[dict]:
    adapter = "codex_tasks"
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise BackendContractError(adapter, "tasks items list envelope required")
    if len(data["items"]) > 100:
        raise BackendContractError(adapter, "result exceeds 100 items")
    result: list[dict] = []
    for raw in data["items"][:limit]:
        if not isinstance(raw, dict):
            raise BackendContractError(adapter, "every task must be an object")
        task = raw.get("task") if "task" in raw else raw
        turn = raw.get("turn")
        if not isinstance(task, dict):
            raise BackendContractError(adapter, "task must be an object")
        if turn is not None and not isinstance(turn, dict):
            raise BackendContractError(adapter, "turn must be an object or null")
        result.append(
            {
                "id": bounded_string(
                    task.get("id"), adapter=adapter, field="id", required=True
                ),
                "title": bounded_string(
                    task.get("title") or "",
                    adapter=adapter,
                    field="title",
                    redact_value=True,
                    maximum=4_096,
                ),
                "status": bounded_string(
                    (turn or {}).get("turn_status"),
                    adapter=adapter,
                    field="turn_status",
                ),
            }
        )
    return result


def register(mcp, client: BackendClient) -> None:
    @mcp.tool(annotations=tool_annotations("list_codex_envs"))
    async def list_codex_envs() -> list:
        """Return Codex environments (label, repos, network access)."""
        data = await async_get(
            client,
            "/backend-api/codex/environments",
            target_path="/backend-api/codex/environments",
        )
        return normalize_codex_environments(data)

    @mcp.tool(annotations=tool_annotations("list_codex_tasks"))
    async def list_codex_tasks(limit: int = 10) -> list:
        """Return recent Codex tasks (title + status). Content is PII-redacted."""
        limit = validate_int(limit, name="limit", minimum=1, maximum=100)
        data = await async_get(
            client,
            "/backend-api/codex/tasks",
            params={"limit": limit},
            target_path="/backend-api/codex/tasks",
        )
        return normalize_codex_tasks(data, limit=limit)
