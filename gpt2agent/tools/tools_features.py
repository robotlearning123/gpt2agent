"""Code interpreter and canvas execution tools."""
from __future__ import annotations

import json

from gpt2agent.backend import BackendClient
from gpt2agent.tools._browser import browser_transport
from gpt2agent.tools.manual import build_handoff

# Prompt wrapper for canvas_execute — shared by the SSE path and the
# manual=True handoff so both send byte-identical text.
CANVAS_PROMPT_PREFIX = "Use Canvas to: "


def register(mcp, client: BackendClient, conv=None, cfg=None) -> None:

    @mcp.tool()
    async def code_interpreter(
        prompt: str,
        model: str = "gpt-5-6",
        browser: bool = False,
        manual: bool = False,
    ) -> dict | str:
        """Execute code via ChatGPT's code interpreter.

        Sends a prompt that triggers code execution. The server runs the code
        in a sandbox and returns the output.

        Args:
            prompt: The code or instruction to execute (e.g. "Run this Python code: ...").
            model: ChatGPT model to use. Defaults to gpt-5-6.
            browser: When True, drive chatgpt.com in a real Chrome via the
                   experimental browser transport instead of the backend.
            manual: When True, return the paste-into-chatgpt.com handoff JSON
                   string instead of calling the backend.

        Returns:
            Dict with: conversation_id, text (assistant explanation),
            tool_calls, tool_responses, multimodal_assets (if any charts/images).

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).

        Set `browser=True` to drive chatgpt.com in a real Chrome via the
        experimental browser transport (requires `[browser] enabled = true`
        in config.toml plus `pip install "gpt2agent[browser]"`). `manual=True`
        wins over `browser=True` — the explicit handoff beats engine choice.
        """
        if manual:
            return json.dumps(
                build_handoff(
                    "code_interpreter", prompt, model=None, temporary=False
                ),
                indent=2,
            )
        if browser:
            transport = browser_transport(
                (cfg or {}).get("browser", {}), "code_interpreter")
            return await transport.chat(prompt, model=model, temporary=False)
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
        browser: bool = False,
        manual: bool = False,
    ) -> dict | str:
        """Execute code via ChatGPT's Canvas feature.

        Creates a Canvas document with live code execution. Similar to
        code_interpreter but uses the Canvas editing environment.

        Canvas was retired upstream (2026-05): the model now answers with a
        deprecation notice instead of creating a document (measured
        2026-09-23 on a live Pro account). Use `code_interpreter` instead.

        Args:
            prompt: The code or instruction (e.g. "Create a React component that...").
            model: ChatGPT model to use. Defaults to gpt-5-6.
            browser: When True, drive chatgpt.com in a real Chrome via the
                   experimental browser transport instead of the backend.
            manual: When True, return the paste-into-chatgpt.com handoff JSON
                   string instead of calling the backend.

        Returns:
            Dict with: conversation_id, text, tool_calls, tool_responses.

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).

        Set `browser=True` to drive chatgpt.com in a real Chrome via the
        experimental browser transport (requires `[browser] enabled = true`
        in config.toml plus `pip install "gpt2agent[browser]"`). `manual=True`
        wins over `browser=True` — the explicit handoff beats engine choice.
        The browser path returns the chat reply text only; Canvas artifact
        extraction is a follow-up.
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
        if browser:
            transport = browser_transport(
                (cfg or {}).get("browser", {}), "canvas_execute")
            return await transport.chat(wrapped, model=model, temporary=False)
        if conv is None:
            from gpt2agent.sse import ConversationClient
            _conv = ConversationClient(client)
        else:
            _conv = conv

        return await _conv.tool_call(
            wrapped, model=model, temporary=False
        )
