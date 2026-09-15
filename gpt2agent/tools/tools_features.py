"""Code interpreter and canvas execution tools."""
from __future__ import annotations

import json

from gpt2agent.backend import BackendClient
from gpt2agent.tools.manual import build_handoff

# Prompt wrapper for canvas_execute — shared by the SSE path and the
# manual=True handoff so both send byte-identical text.
CANVAS_PROMPT_PREFIX = "Use Canvas to: "


def register(mcp, client: BackendClient, conv=None) -> None:

    @mcp.tool()
    async def code_interpreter(
        prompt: str,
        model: str = "gpt-5-6",
        manual: bool = False,
    ) -> dict:
        """Execute code via ChatGPT's code interpreter.

        Sends a prompt that triggers code execution. The server runs the code
        in a sandbox and returns the output.

        Args:
            prompt: The code or instruction to execute (e.g. "Run this Python code: ...").
            model: ChatGPT model to use. Defaults to gpt-5-6.

        Returns:
            Dict with: conversation_id, text (assistant explanation),
            tool_calls, tool_responses, multimodal_assets (if any charts/images).

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).
        """
        if manual:
            return json.dumps(
                build_handoff(
                    "code_interpreter", prompt, model=None, temporary=False
                ),
                indent=2,
            )
        if conv is None:
            from gpt2agent.sse import ConversationClient
            _conv = ConversationClient(client)
        else:
            _conv = conv

        return await _conv.tool_call(prompt, model=model, temporary=False)

    @mcp.tool()
    async def canvas_execute(
        prompt: str,
        model: str = "gpt-5-6",
        manual: bool = False,
    ) -> dict:
        """Execute code via ChatGPT's Canvas feature.

        Creates a Canvas document with live code execution. Similar to
        code_interpreter but uses the Canvas editing environment.

        Args:
            prompt: The code or instruction (e.g. "Create a React component that...").
            model: ChatGPT model to use. Defaults to gpt-5-6.

        Returns:
            Dict with: conversation_id, text, tool_calls, tool_responses.

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).
        """
        wrapped = f"{CANVAS_PROMPT_PREFIX}{prompt}"
        if manual:
            return json.dumps(
                build_handoff(
                    "canvas_execute", wrapped, model=model, temporary=False,
                    extra={"mode": "canvas"},
                ),
                indent=2,
            )
        if conv is None:
            from gpt2agent.sse import ConversationClient
            _conv = ConversationClient(client)
        else:
            _conv = conv

        return await _conv.tool_call(
            wrapped, model=model, temporary=False
        )
