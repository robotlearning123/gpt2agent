from __future__ import annotations

import asyncio

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError, InputValidationError
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get, async_post
from gpt2agent.tools._validation import bounded_string, nullable_bool
from gpt2agent.tools.codex import normalize_codex_environments


_CUSTOM_INSTRUCTION_TEXT_FIELDS = (
    "about_user_message",
    "about_model_message",
)
_CUSTOM_INSTRUCTION_BOOL_FIELDS = ("enabled", "traits_enabled")


def _normalize_custom_instruction_write_state(data: object) -> dict:
    adapter = "custom_instructions_set"
    if not isinstance(data, dict):
        raise BackendContractError(adapter, "current state object required")

    payload: dict = {}
    for field in _CUSTOM_INSTRUCTION_BOOL_FIELDS:
        if field in data:
            payload[field] = nullable_bool(data[field], adapter=adapter, field=field)
    for field in _CUSTOM_INSTRUCTION_TEXT_FIELDS:
        if field in data:
            payload[field] = bounded_string(
                data[field],
                adapter=adapter,
                field=field,
                maximum=10_000,
            )
    if "personality_type_selection" in data:
        payload["personality_type_selection"] = bounded_string(
            data["personality_type_selection"],
            adapter=adapter,
            field="personality_type_selection",
            maximum=256,
        )
    if "disabled_tools" in data:
        disabled_tools = data["disabled_tools"]
        if disabled_tools is None:
            payload["disabled_tools"] = None
        elif (
            not isinstance(disabled_tools, list)
            or len(disabled_tools) > 100
            or any(not isinstance(item, str) for item in disabled_tools)
        ):
            raise BackendContractError(
                adapter, "disabled_tools must be a bounded string list or null"
            )
        else:
            payload["disabled_tools"] = [
                bounded_string(
                    item,
                    adapter=adapter,
                    field="disabled_tools entry",
                    required=True,
                )
                for item in disabled_tools
            ]
    return payload


def normalize_codex_task_create(data: object) -> dict:
    """Project a Codex create response onto a minimal action receipt."""
    if not isinstance(data, dict):
        raise BackendContractError("codex_task_create", "object response required")
    task = data.get("task") if "task" in data else data
    turn = data.get("turn")
    if not isinstance(task, dict):
        raise BackendContractError("codex_task_create", "task object required")
    if turn is not None and not isinstance(turn, dict):
        raise BackendContractError("codex_task_create", "turn must be an object or null")
    status = (turn or {}).get("turn_status")
    if status is None:
        status = task.get("status")
    return {
        "id": bounded_string(
            task.get("id"),
            adapter="codex_task_create",
            field="task.id",
            required=True,
        ),
        "status": bounded_string(
            status,
            adapter="codex_task_create",
            field="status",
        ),
    }


def register(mcp, client: BackendClient) -> None:
    custom_instruction_write_lock = asyncio.Lock()

    @mcp.tool(annotations=tool_annotations("custom_instructions_set"))
    async def custom_instructions_set(
        about_user: str | None = None,
        about_model: str | None = None,
    ) -> dict:
        """Overwrite ChatGPT custom instructions (read-modify-write — preserves fields not supplied)."""
        fields = [
            name
            for name, value in (("about_user", about_user), ("about_model", about_model))
            if value is not None
        ]
        if not fields:
            raise InputValidationError(
                "at least one custom-instruction field must be supplied"
            )

        # Serialize the read-modify-write window so concurrent partial updates
        # cannot preserve stale values. The current response is then projected
        # onto the reviewed write schema rather than replayed opaquely.
        async with custom_instruction_write_lock:
            auth_headers = client.request_headers()
            current = await async_get(
                client,
                "/backend-api/user_system_messages",
                target_path="/backend-api/user_system_messages",
                auth_headers=auth_headers,
            )
            # None = empty-2xx glitch (backend.get contract): the current state is
            # UNKNOWN, so blind-posting only the supplied fields would silently
            # clear the other one. Refuse instead. ({} = known-empty is fine.)
            if current is None:
                raise RuntimeError(
                    "could not read current custom instructions — refusing to "
                    "overwrite; retry in a moment"
                )
            payload = _normalize_custom_instruction_write_state(current)
            if about_user is not None:
                payload["about_user_message"] = bounded_string(
                    about_user,
                    adapter="custom_instructions_set",
                    field="about_user",
                    maximum=10_000,
                )
            if about_model is not None:
                payload["about_model_message"] = bounded_string(
                    about_model,
                    adapter="custom_instructions_set",
                    field="about_model",
                    maximum=10_000,
                )
            await async_post(
                client,
                "/backend-api/user_system_messages",
                json=payload,
                target_path="/backend-api/user_system_messages",
                auth_headers=auth_headers,
            )
        return {"updated": True, "fields": fields}

    # memory_add is NOT registered as an MCP tool.
    # SPIKE FINDING 2026-04-23: POST /backend-api/memories → 405 Method Not Allowed
    # (Allow: GET only). PATCH and PUT also 405. Memory creation is model-initiated
    # only — not available via REST. Exposing a tool that always raises misleads agents
    # that introspect the tool list, so registration is intentionally skipped.
    def memory_add(content: str) -> dict:  # noqa: F841 — kept as documentation
        raise RuntimeError(
            "POST /backend-api/memories is not supported — server returns 405 "
            "Method Not Allowed (Allow: GET). Memory creation must go through "
            "a ChatGPT conversation (model-initiated only)."
        )

    @mcp.tool(annotations=tool_annotations("codex_task_create"))
    async def codex_task_create(
        repo_label: str,
        prompt: str,
        environment_id: str | None = None,
        branch: str = "main",
    ) -> dict:
        """Create a new Codex task.

        Resolves environment_id from repo_label if not supplied.
        Verified payload shape (2026-04-23): POST /backend-api/codex/tasks
        with new_task={environment_id, branch} + top-level input_items.
        """
        auth_headers = client.request_headers()
        env_id = environment_id
        if env_id is None:
            data = await async_get(
                client,
                "/backend-api/codex/environments",
                target_path="/backend-api/codex/environments",
                auth_headers=auth_headers,
            )
            envs = normalize_codex_environments(data)
            matches = [e for e in envs if e.get("label") == repo_label]
            if len(matches) == 0:
                raise InputValidationError(
                    "No Codex environment matches repo_label"
                )
            if len(matches) > 1:
                raise InputValidationError(
                    "Ambiguous repo_label; pass environment_id explicitly"
                )
            env_id = matches[0]["id"]

        payload = {
            "new_task": {
                "environment_id": env_id,
                "branch": branch,
            },
            "input_items": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"content_type": "text", "text": prompt}],
                }
            ],
        }
        result = await async_post(
            client,
            "/backend-api/codex/tasks",
            json=payload,
            target_path="/backend-api/codex/tasks",
            auth_headers=auth_headers,
        )
        return normalize_codex_task_create(result)
