from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

from gpt2agent.backend import BackendClient
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._redact import redact


_ROUTE = "/backend-api/settings/voices"
_CONTRACT_ERROR = "voice catalog contract changed"
_MAX_VOICES = 128


def _fail_contract() -> None:
    """Raise a payload-free error for a private response-shape change."""
    raise RuntimeError(_CONTRACT_ERROR)


def _bounded_text(value: object, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or not value.isprintable()
    ):
        _fail_contract()
    return value


def _normalize_catalog(data: object) -> list[dict[str, Any]]:
    """Project the private Voice response onto the stable MCP contract."""
    if not isinstance(data, dict):
        _fail_contract()

    raw_voices = data.get("voices")
    if not isinstance(raw_voices, list) or len(raw_voices) > _MAX_VOICES:
        _fail_contract()

    normalized: list[dict[str, Any]] = []
    voice_ids: set[str] = set()
    for raw in raw_voices:
        if not isinstance(raw, dict):
            _fail_contract()

        voice_id = _bounded_text(raw.get("voice"), max_length=128)
        name = _bounded_text(raw.get("name"), max_length=256)
        description = _bounded_text(raw.get("description"), max_length=2_000)
        preview_url = raw.get("preview_url")
        if preview_url is not None and not isinstance(preview_url, str):
            _fail_contract()
        if voice_id in voice_ids:
            _fail_contract()
        voice_ids.add(voice_id)

        normalized.append(
            {
                "id": voice_id,
                "name": redact(name),
                "description": redact(description),
                "selected": None,
                "has_preview": bool(preview_url),
            }
        )

    selected = data.get("selected")
    if isinstance(selected, str) and selected in voice_ids:
        for item in normalized:
            item["selected"] = item["id"] == selected

    return normalized


def register(mcp, client: BackendClient) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    async def list_voices() -> list[dict[str, Any]]:
        """List Voice choices currently available to the signed-in account.

        Returns only stable catalog metadata: `id`, `name`, `description`,
        `selected`, and `has_preview`. This does not start a Voice session,
        fetch preview audio, synthesize speech, or expose GPT-Live audio.
        """
        data = await async_get(client, _ROUTE, target_path=_ROUTE)
        return _normalize_catalog(data)
