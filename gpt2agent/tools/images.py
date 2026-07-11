"""Image generation and file download tools."""
from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

from gpt2agent.backend import BackendClient
from gpt2agent.errors import BackendContractError, InputValidationError
from gpt2agent.tool_contracts import tool_annotations
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._ids import validate_path_id
from gpt2agent.tools._redact import redact
from gpt2agent.tools._validation import (
    bounded_nonnegative_int,
    bounded_string,
    bounded_time_value,
)


_MAX_GENERATED_IMAGE_ASSETS = 100
_DOWNLOAD_HOST_LABEL_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_INTERNAL_DOWNLOAD_SUFFIXES = frozenset(
    {"corp", "home", "home.arpa", "internal", "lan", "local", "localdomain", "localhost"}
)
_UNSAFE_ENCODED_URL_CHARACTER_RE = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|5c|7f)", re.I)


def _invalid_download_url() -> None:
    raise BackendContractError(
        "file_download",
        "download_url must be one absolute public HTTPS URL",
    )


def _valid_percent_escapes(value: str) -> bool:
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in "0123456789abcdefABCDEF"
            or value[index + 2] not in "0123456789abcdefABCDEF"
        ):
            return False
    return True


def _project_download_url(value: Any) -> str:
    """Validate an untrusted download destination without rewriting its signature."""
    if value is None:
        return ""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or "#" in value
        or any(
            character == "\\"
            or character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in value
        )
        or not _valid_percent_escapes(value)
        or _UNSAFE_ENCODED_URL_CHARACTER_RE.search(value) is not None
    ):
        _invalid_download_url()

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        _invalid_download_url()

    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
    ):
        _invalid_download_url()

    authority = hostname if port is None else f"{hostname}:443"
    if parsed.netloc.casefold() != authority.casefold():
        _invalid_download_url()

    try:
        hostname.encode("ascii")
    except UnicodeEncodeError:
        _invalid_download_url()

    normalized_host = hostname.casefold()
    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        pass
    else:
        _invalid_download_url()

    labels = normalized_host.split(".")
    if (
        len(normalized_host) > 253
        or len(labels) < 2
        or all(label.isdigit() for label in labels)
        or any(_DOWNLOAD_HOST_LABEL_RE.fullmatch(label) is None for label in labels)
        or any(
            normalized_host == suffix or normalized_host.endswith(f".{suffix}")
            for suffix in _INTERNAL_DOWNLOAD_SUFFIXES
        )
    ):
        _invalid_download_url()

    return value


def _generated_image_file_id(value: Any) -> str:
    """Validate a backend file ID without ever returning secret-shaped IDs."""
    try:
        file_id = validate_path_id(value, kind="file ID")
    except InputValidationError:
        raise BackendContractError(
            "image_generation", "backend returned an invalid file ID"
        ) from None
    if redact(file_id) != file_id:
        raise BackendContractError(
            "image_generation", "backend returned an invalid file ID"
        )
    return file_id


def normalize_generated_image_result(data: Any) -> dict:
    """Return a fresh, bounded projection of the ConversationClient result."""
    adapter = "image_generation"
    if not isinstance(data, dict):
        raise BackendContractError(adapter, "image result object required")

    raw_conversation_id = data.get("conversation_id")
    conversation_id = bounded_string(
        raw_conversation_id,
        adapter=adapter,
        field="conversation_id",
        required=raw_conversation_id is not None,
        redact_value=True,
        maximum=2_048,
    )
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list):
        raise BackendContractError(adapter, "assets must be an array")
    if len(raw_assets) > _MAX_GENERATED_IMAGE_ASSETS:
        raise BackendContractError(adapter, "image asset list exceeds 100 records")

    assets: list[dict] = []
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise BackendContractError(adapter, "every image asset must be an object")
        file_id = _generated_image_file_id(raw_asset.get("file_id"))
        asset_pointer = bounded_string(
            raw_asset.get("asset_pointer"),
            adapter=adapter,
            field="asset_pointer",
            required=True,
            maximum=2_048,
        )
        if asset_pointer != f"sediment://{file_id}":
            raise BackendContractError(
                adapter, "asset_pointer must match the backend file ID"
            )
        assets.append(
            {
                "asset_pointer": asset_pointer,
                "file_id": file_id,
                "width": bounded_nonnegative_int(
                    raw_asset.get("width"), adapter=adapter, field="width"
                ),
                "height": bounded_nonnegative_int(
                    raw_asset.get("height"), adapter=adapter, field="height"
                ),
                "size_bytes": bounded_nonnegative_int(
                    raw_asset.get("size_bytes"),
                    adapter=adapter,
                    field="size_bytes",
                ),
            }
        )

    return {"conversation_id": conversation_id, "assets": assets}


def _nullable_nonnegative_int(value: Any, *, field: str) -> int | None:
    return bounded_nonnegative_int(value, adapter="file_info", field=field)


def _nullable_time(value: Any) -> int | float | str | None:
    return bounded_time_value(
        value,
        adapter="file_info",
        field="creation_time",
    )


def normalize_file_info(data: Any, *, expected_id: str) -> dict:
    """Return the reviewed file-info projection, never an opaque backend object."""
    if not isinstance(data, dict):
        raise BackendContractError("file_info", "file object response required")
    returned_id = data.get("id")
    if returned_id is not None:
        returned_id = bounded_string(
            returned_id, adapter="file_info", field="id", required=True
        )
        if returned_id != expected_id:
            raise BackendContractError("file_info", "response id does not match request")
    return {
        "id": expected_id,
        "name": bounded_string(
            data.get("name"),
            adapter="file_info",
            field="name",
            redact_value=True,
            maximum=2_048,
        ),
        "size": _nullable_nonnegative_int(data.get("size"), field="size"),
        "file_size_bytes": _nullable_nonnegative_int(
            data.get("file_size_bytes"), field="file_size_bytes"
        ),
        "use_case": bounded_string(
            data.get("use_case"), adapter="file_info", field="use_case"
        ),
        "state": bounded_string(
            data.get("state"), adapter="file_info", field="state"
        ),
        "creation_time": _nullable_time(data.get("creation_time")),
        "mime_type": bounded_string(
            data.get("mime_type"), adapter="file_info", field="mime_type"
        ),
    }


def normalize_download_info(data: Any) -> dict:
    if not isinstance(data, dict):
        raise BackendContractError("file_download", "download object response required")
    return {
        "download_url": _project_download_url(data.get("download_url")),
        "file_name": bounded_string(
            data.get("file_name"),
            adapter="file_download",
            field="file_name",
            redact_value=True,
            maximum=2_048,
        )
        or "",
        "file_size_bytes": _nullable_nonnegative_int(
            data.get("file_size_bytes"), field="file_size_bytes"
        ),
        "mime_type": bounded_string(
            data.get("mime_type"), adapter="file_download", field="mime_type"
        ),
    }


def register(mcp, client: BackendClient, conv=None) -> None:

    @mcp.tool(annotations=tool_annotations("generate_image"))
    async def generate_image(
        prompt: str,
        model: str = "gpt-5-3",
    ) -> dict:
        """Generate an image using ChatGPT's built-in image generation.

        The image is created asynchronously through the observed private
        prepare/conduit + `/f` v1 flow. This is an undocumented ChatGPT route,
        not an official or stable API contract. The tool waits until a visible
        result is bound to this stream by the observed dispatch or a same-message
        marker and then returns download URLs plus allowlisted asset fields.

        Args:
            prompt: Description of the image to generate.
            model: ChatGPT model to use (must have image_gen_tool_enabled).
                   Defaults to gpt-5-3.

        Returns:
            Dict with: conversation_id, assets (list with asset_pointer, file_id,
            width, height, size_bytes, download_url, file_name).
        """
        if conv is None:
            from gpt2agent.sse import ConversationClient
            _conv = ConversationClient(client)
        else:
            _conv = conv

        auth_headers = client.request_headers()
        result = normalize_generated_image_result(
            await _conv.image_gen(
                prompt,
                model=model,
                auth_headers=auth_headers,
            )
        )

        # Enrich each asset with a download URL (offload sync HTTP to thread)
        for asset in result.get("assets", []):
            file_id = asset.get("file_id", "")
            if file_id:
                try:
                    dl = await async_get(
                        client,
                        f"/backend-api/files/{file_id}/download",
                        auth_headers=auth_headers,
                    )
                    asset.update(normalize_download_info(dl))
                except Exception:
                    # Enrichment is optional, but exception text can contain an
                    # upstream URL, header, or token. Return a stable status only.
                    asset["download_error"] = "temporarily_failed"

            if file_id and not asset.get("file_name"):
                try:
                    info = await async_get(
                        client,
                        f"/backend-api/files/{file_id}",
                        auth_headers=auth_headers,
                    )
                    normalized = normalize_file_info(info, expected_id=file_id)
                    asset["file_name"] = normalized["name"] or ""
                    for field in ("use_case", "state", "creation_time"):
                        asset[field] = normalized[field]
                except Exception:
                    asset["info_error"] = "temporarily_failed"

        return result

    @mcp.tool(annotations=tool_annotations("get_file_info"))
    async def get_file_info(file_id: str) -> dict:
        """Get metadata for a ChatGPT file (images, uploads, etc.).

        Args:
            file_id: The file ID (e.g. file_00000000c02471f88295cda5f3b8c66b).

        Returns:
            Dict with id, name, size, use_case, state, creation_time, mime_type, etc.
        """
        file_id = validate_path_id(file_id, kind="file ID")
        data = await async_get(client, f"/backend-api/files/{file_id}")
        return normalize_file_info(data, expected_id=file_id)

    @mcp.tool(annotations=tool_annotations("get_file_download_url"))
    async def get_file_download_url(file_id: str) -> str:
        """Get a temporary download URL for a ChatGPT file.

        Args:
            file_id: The file ID.

        Returns:
            The download URL string (time-limited, expires after ~1 hour).
        """
        file_id = validate_path_id(file_id, kind="file ID")
        data = await async_get(client, f"/backend-api/files/{file_id}/download")
        return normalize_download_info(data)["download_url"]
