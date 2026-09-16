"""Image generation and file download tools."""
from __future__ import annotations

import json

from gpt2agent.backend import BackendClient
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._browser import browser_transport
from gpt2agent.tools._ids import validate_path_id
from gpt2agent.tools.manual import build_handoff


def register(mcp, client: BackendClient, conv=None, cfg=None) -> None:

    @mcp.tool()
    async def generate_image(
        prompt: str,
        model: str = "gpt-5-6",
        browser: bool = False,
        manual: bool = False,
    ) -> dict | str:
        """Generate an image using ChatGPT's built-in image generation.

        The image is created asynchronously. This tool waits until it's ready
        and returns download URLs and metadata.

        Args:
            prompt: Description of the image to generate.
            model: ChatGPT model to use (must have image_gen_tool_enabled).
                   Defaults to gpt-5-6.
            browser: When True, drive chatgpt.com in a real Chrome via the
                   experimental browser transport instead of the backend.
            manual: When True, return the paste-into-chatgpt.com handoff JSON
                   string instead of calling the backend.

        Returns:
            Dict with: conversation_id, assets (list with asset_pointer, file_id,
            width, height, size_bytes, download_url, file_name), metadata.

        Set `manual=True` to get a paste-into-chatgpt.com handoff JSON instead
        of calling the backend (zero network calls).

        Set `browser=True` to drive chatgpt.com in a real Chrome via the
        experimental browser transport (requires `[browser] enabled = true`
        in config.toml plus `pip install "gpt2agent[browser]"`). `manual=True`
        wins over `browser=True` — the explicit handoff beats engine choice.
        The browser path returns the chat reply text only; asset download
        URLs are a follow-up.
        """
        if manual:
            return json.dumps(
                build_handoff(
                    "generate_image", prompt, model=None, temporary=False,
                    extra={"mode": "images"},
                ),
                indent=2,
            )
        if browser:
            transport = browser_transport(
                (cfg or {}).get("browser", {}), "generate_image")
            return await transport.chat(
                prompt, model=model, temporary=False, mode="images")
        if conv is None:
            from gpt2agent.sse import ConversationClient
            _conv = ConversationClient(client)
        else:
            _conv = conv

        result = await _conv.image_gen(prompt, model=model)

        # Enrich each asset with a download URL (offload sync HTTP to thread)
        for asset in result.get("assets", []):
            file_id = asset.get("file_id", "")
            if file_id:
                file_id = validate_path_id(file_id, kind="file ID")
                try:
                    dl = await async_get(client, f"/backend-api/files/{file_id}/download")
                    asset["download_url"] = (dl or {}).get("download_url", "")
                    asset["file_name"] = (dl or {}).get("file_name", "")
                    asset["file_size_bytes"] = (dl or {}).get("file_size_bytes")
                    asset["mime_type"] = (dl or {}).get("mime_type")
                except Exception as e:
                    asset["download_error"] = str(e)[:200]

            if file_id and not asset.get("file_name"):
                try:
                    info = await async_get(client, f"/backend-api/files/{file_id}")
                    asset["file_name"] = (info or {}).get("name", "")
                    asset["use_case"] = (info or {}).get("use_case")
                    asset["state"] = (info or {}).get("state")
                    asset["creation_time"] = (info or {}).get("creation_time")
                except Exception as e:
                    asset["info_error"] = str(e)[:200]

        return result

    @mcp.tool()
    async def get_file_info(file_id: str) -> dict:
        """Get metadata for a ChatGPT file (images, uploads, etc.).

        Args:
            file_id: The file ID (e.g. file_00000000c02471f88295cda5f3b8c66b).

        Returns:
            Dict with id, name, size, use_case, state, creation_time, mime_type, etc.
        """
        file_id = validate_path_id(file_id, kind="file ID")
        return await async_get(client, f"/backend-api/files/{file_id}") or {}

    @mcp.tool()
    async def get_file_download_url(file_id: str) -> str:
        """Get a temporary download URL for a ChatGPT file.

        Args:
            file_id: The file ID.

        Returns:
            The download URL string (time-limited, expires after ~1 hour).
        """
        file_id = validate_path_id(file_id, kind="file ID")
        data = await async_get(client, f"/backend-api/files/{file_id}/download")
        return (data or {}).get("download_url", "")
