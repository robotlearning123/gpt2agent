from __future__ import annotations

from typing import Any

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._validation import bounded_string, nullable_bool


def normalize_custom_instructions(data: Any) -> dict:
    adapter = "custom_instructions"
    if not isinstance(data, dict):
        raise BackendContractError(adapter, "object response required")
    return {
        "enabled": nullable_bool(data.get("enabled"), adapter=adapter, field="enabled"),
        "traits_enabled": nullable_bool(
            data.get("traits_enabled"), adapter=adapter, field="traits_enabled"
        ),
        "personality_type": bounded_string(
            data.get("personality_type_selection"),
            adapter=adapter,
            field="personality_type_selection",
            maximum=256,
        ),
        "about_user": bounded_string(
            data.get("about_user_message") or "",
            adapter=adapter,
            field="about_user_message",
            redact_value=True,
            maximum=10_000,
        ),
        "about_model": bounded_string(
            data.get("about_model_message") or "",
            adapter=adapter,
            field="about_model_message",
            redact_value=True,
            maximum=10_000,
        ),
    }


def register(mcp, client: BackendClient) -> None:
    @mcp.tool(annotations=tool_annotations("custom_instructions_get"))
    async def custom_instructions_get() -> dict:
        """Return ChatGPT custom instructions (PII redacted)."""
        ci = await async_get(
            client,
            "/backend-api/user_system_messages",
            target_path="/backend-api/user_system_messages",
        )
        # Preserve the established empty-2xx compatibility result without
        # coercing other falsey/malformed JSON types into a valid object.
        if ci is None:
            ci = {}
        return normalize_custom_instructions(ci)
