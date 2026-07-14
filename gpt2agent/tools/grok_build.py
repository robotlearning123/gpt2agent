from __future__ import annotations

from typing import Any, Literal

from gpt2agent.errors import InputValidationError
from gpt2agent.grok_build import GrokBuildClient
from gpt2agent.tool_contracts import tool_annotations


_PROMPT_MAX_BYTES = 65_536


def _validate_agent_inputs(
    prompt: str,
    *,
    cwd: str | None,
    mode: Literal["plan", "apply"],
    model: str | None,
    max_turns: int | None,
    subagents: bool,
) -> None:
    encoded_size = 0
    encoding_failed = False
    if isinstance(prompt, str):
        try:
            encoded_size = len(prompt.encode("utf-8"))
        except UnicodeEncodeError:
            encoding_failed = True
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or "\x00" in prompt
        or encoding_failed
        or encoded_size > _PROMPT_MAX_BYTES
    ):
        raise InputValidationError(
            "grok_build.prompt must be 1..65536 UTF-8 bytes"
        )
    if cwd is not None and not isinstance(cwd, str):
        raise InputValidationError("grok_build.cwd must be a string or null")
    if mode not in ("plan", "apply"):
        raise InputValidationError("grok_build.mode must be plan or apply")
    if model is not None and not isinstance(model, str):
        raise InputValidationError("grok_build.model must be a string or null")
    if max_turns is not None and (
        isinstance(max_turns, bool)
        or not isinstance(max_turns, int)
        or not 1 <= max_turns <= 100
    ):
        raise InputValidationError("grok_build.max_turns must be 1..100")
    if not isinstance(subagents, bool):
        raise InputValidationError("grok_build.subagents must be boolean")


def register(mcp, client: GrokBuildClient) -> None:
    @mcp.tool(annotations=tool_annotations("grok_build_agent"))
    async def grok_build_agent(
        prompt: str,
        cwd: str | None = None,
        mode: Literal["plan", "apply"] = "plan",
        model: str | None = None,
        max_turns: int | None = None,
        subagents: bool = False,
    ) -> dict[str, Any]:
        """Run one bounded official Grok Build CLI agent session."""
        _validate_agent_inputs(
            prompt,
            cwd=cwd,
            mode=mode,
            model=model,
            max_turns=max_turns,
            subagents=subagents,
        )
        return await client.agent(
            prompt,
            cwd=cwd,
            mode=mode,
            model=model,
            max_turns=max_turns,
            subagents=subagents,
        )

    @mcp.tool(annotations=tool_annotations("grok_build_models"))
    async def grok_build_models() -> dict[str, Any]:
        """Return the authenticated Grok Build CLI model catalog."""
        return await client.models()

    @mcp.tool(annotations=tool_annotations("grok_build_status"))
    async def grok_build_status() -> dict[str, Any]:
        """Return secret-free Grok Build CLI installation and auth status."""
        return await client.status()
