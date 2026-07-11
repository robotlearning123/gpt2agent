"""Native SSE client for /backend-api/conversation — no proxy required."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from copy import deepcopy
from typing import AsyncIterator, Mapping
from uuid import uuid4

from curl_cffi.requests import AsyncSession

from gpt2agent.backend import (
    BackendClient,
    _BASE,
    _account_session_options,
    _disable_tls_key_logging,
    _is_filesize_exceeded,
)
from gpt2agent.errors import BackendContractError, BackendHTTPError, backend_http_error
from gpt2agent.message_visibility import is_user_visible_message
from gpt2agent.sentinel import SentinelGate  # noqa: F401  (used in stream)
from gpt2agent.tools._redact import redact

_log = logging.getLogger(__name__)

_CONV_URL = _BASE + "/backend-api/conversation"

_INCOMPLETE_RESPONSE_MESSAGE = (
    "ChatGPT stream ended before completion; partial output was discarded"
)
_MAX_SSE_LINE_BYTES = 4 * 1024 * 1024
_MAX_SSE_STREAM_BYTES = 64 * 1024 * 1024
_MAX_TOOL_RECORDS = 100
_MAX_TOOL_PARTS = 100
_MAX_TOOL_PART_CHARS = 20_000
_MAX_TOOL_TEXT_CHARS = 100_000
_MAX_TOOL_PROJECTED_CHARS = 4 * 1024 * 1024
_MAX_TOOL_INTEGER = (1 << 63) - 1
_MAX_METADATA_POINTER_CHARS = 2_048
_MAX_METADATA_POINTER_DEPTH = 32
_MAX_METADATA_ARRAY_INDEX = 100
_MAX_METADATA_LIST_ITEMS = _MAX_METADATA_ARRAY_INDEX + 1
_MAX_METADATA_NODES = 4_096
_MAX_METADATA_STRUCTURE_DEPTH = 64
_MAX_METADATA_STRING_CHARS = 1_000_000
_BACKEND_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_SEDIMENT_ASSET_RE = re.compile(r"sediment://([A-Za-z0-9_-]{1,256})\Z")
_TOOL_ACTIVITY_CATEGORIES = (
    "web",
    "code_execution",
    "image_generation",
    "canvas",
    "connector",
    "tool_response",
)
_KNOWN_MESSAGE_ROLES = frozenset({"assistant", "system", "tool", "user"})


def _bounded_tool_string(
    value,
    *,
    field: str,
    maximum: int,
    redact_value: bool = True,
    adapter: str = "tool_call",
) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise BackendContractError(adapter, f"{field} must be a bounded string")
    if not redact_value:
        return value
    projected = redact(value)
    assert isinstance(projected, str)
    return projected


def _redacted_tool_text(value: str, *, maximum: int) -> str:
    projected = redact(value)
    assert isinstance(projected, str)
    return projected[:maximum]


def _tool_asset_integer(value, *, field: str, adapter: str = "tool_call") -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_TOOL_INTEGER
    ):
        raise BackendContractError(
            adapter, f"{field} must be a bounded non-negative integer or null"
        )
    return value


def _project_tool_image_asset(part: dict, *, adapter: str = "tool_call") -> dict:
    asset_pointer = _bounded_tool_string(
        part.get("asset_pointer"),
        field="asset_pointer",
        maximum=2_048,
        redact_value=False,
        adapter=adapter,
    )
    match = _SEDIMENT_ASSET_RE.fullmatch(asset_pointer)
    if match is None:
        raise BackendContractError(
            adapter, "asset_pointer must contain one sediment file identifier"
        )
    file_id = match.group(1)
    projected_pointer = redact(asset_pointer)
    projected_file_id = redact(file_id)
    assert isinstance(projected_pointer, str)
    assert isinstance(projected_file_id, str)
    return {
        "asset_pointer": projected_pointer,
        "file_id": projected_file_id,
        "width": _tool_asset_integer(part.get("width"), field="width", adapter=adapter),
        "height": _tool_asset_integer(part.get("height"), field="height", adapter=adapter),
        "size_bytes": _tool_asset_integer(
            part.get("size_bytes"), field="size_bytes", adapter=adapter
        ),
    }


def _backend_path_id(value, *, adapter: str, field: str) -> str:
    if not isinstance(value, str) or _BACKEND_ID_RE.fullmatch(value) is None:
        raise BackendContractError(adapter, f"{field} must be one bounded URL-safe identifier")
    return value


def _private_protocol_string(value: object, *, maximum: int = 256) -> str | None:
    """Validate an opaque backend correlation value without ever exposing it."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        return None
    return value


def _image_dispatch_binding(message: object) -> dict[str, str | None] | None:
    """Extract the minimum private tuple needed to bind an image tool result."""
    if not isinstance(message, dict):
        return None
    author = message.get("author")
    if not isinstance(author, dict) or author.get("role") != "assistant":
        return None
    recipient = _private_protocol_string(message.get("recipient"))
    message_id = _private_protocol_string(message.get("id"))
    if recipient in (None, "all") or message_id is None:
        return None
    metadata = message.get("metadata")
    turn_exchange_id = None
    if isinstance(metadata, dict) and metadata.get("turn_exchange_id") is not None:
        turn_exchange_id = _private_protocol_string(metadata.get("turn_exchange_id"))
        if turn_exchange_id is None:
            return None
    return {
        "message_id": message_id,
        "recipient": recipient,
        "turn_exchange_id": turn_exchange_id,
    }


def _is_generated_image_part(part: object) -> bool:
    if not isinstance(part, dict) or part.get("content_type") != "image_asset_pointer":
        return False
    pointer = part.get("asset_pointer")
    if not isinstance(pointer, str) or _SEDIMENT_ASSET_RE.fullmatch(pointer) is None:
        return False
    metadata = part.get("metadata")
    if not isinstance(metadata, dict):
        return False
    dalle = metadata.get("dalle")
    generation = metadata.get("generation")
    return bool(
        isinstance(dalle, dict)
        and dalle.get("serialization_title") == "DALL-E generation metadata"
        or isinstance(generation, dict)
        and generation.get("serialization_title") == "Image Generation metadata"
    )


def _is_image_result_candidate(
    message: object,
    *,
    dispatch: dict[str, str | None] | None,
    marked_message_ids: set[str] | None = None,
    marker_protocol_seen: bool = False,
) -> bool:
    """Return whether a tool carrier is provably from this image-generation turn."""
    if not isinstance(message, dict) or not is_user_visible_message(message):
        return False
    author = message.get("author")
    content = message.get("content")
    metadata = message.get("metadata")
    if (
        not isinstance(author, dict)
        or author.get("role") != "tool"
        or message.get("recipient") != "all"
        or message.get("status") != "finished_successfully"
        or not isinstance(content, dict)
        or content.get("content_type") != "multimodal_text"
        or not isinstance(metadata, dict)
    ):
        return False
    async_task_type = metadata.get("async_task_type")
    if async_task_type is not None and async_task_type != "image_gen":
        return False
    parts = content.get("parts")
    if (
        not isinstance(parts, list)
        or not parts
        or len(parts) > _MAX_TOOL_PARTS
        or not any(_is_generated_image_part(part) for part in parts)
    ):
        return False
    message_id = _private_protocol_string(message.get("id"))
    if dispatch is None:
        # ``async_task_type`` identifies an image result but does not bind a
        # historical result to the request being polled.  Without a dispatch,
        # require the same-stream marker/message-ID relation observed in the
        # current ChatGPT image protocol.
        return bool(
            marker_protocol_seen
            and message_id is not None
            and marked_message_ids is not None
            and message_id in marked_message_ids
        )

    if marker_protocol_seen and (
        message_id is None or marked_message_ids is None or message_id not in marked_message_ids
    ):
        return False

    author_name = _private_protocol_string(author.get("name"))
    parent_id = _private_protocol_string(metadata.get("parent_id"))
    if author_name != dispatch["recipient"] or parent_id != dispatch["message_id"]:
        return False
    dispatch_turn = dispatch.get("turn_exchange_id")
    if dispatch_turn is not None:
        candidate_turn = _private_protocol_string(metadata.get("turn_exchange_id"))
        if candidate_turn != dispatch_turn:
            return False
    return True


def _validated_patch_tokens(
    path: object,
    operation: object,
    *,
    adapter: str,
) -> tuple[str | None, str | None]:
    """Validate hash-safe JSON-patch routing tokens before dispatch."""
    if path is not None and not isinstance(path, str):
        raise BackendContractError(adapter, "patch path must be a string or null")
    if operation is not None and not isinstance(operation, str):
        raise BackendContractError(adapter, "patch operation must be a string or null")
    if isinstance(path, str) and len(path) > _MAX_METADATA_POINTER_CHARS:
        raise BackendContractError(adapter, "patch pointer path exceeds the size limit")
    if isinstance(operation, str) and len(operation) > 64:
        raise BackendContractError(adapter, "patch operation exceeds the size limit")
    return path, operation


def _patch_mutates_current_message(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    path = event.get("p")
    operation = event.get("o")
    if path is not None and not isinstance(path, str):
        return True
    if operation is not None and not isinstance(operation, str):
        return True
    if isinstance(path, str) and len(path) > _MAX_METADATA_POINTER_CHARS:
        return True
    if isinstance(operation, str) and len(operation) > 64:
        return True
    if isinstance(path, str) and (path == "/message" or path.startswith("/message/")):
        return True
    value = event.get("v")
    envelope_message = value.get("message") if isinstance(value, dict) else None
    envelope_author = (
        envelope_message.get("author") if isinstance(envelope_message, dict) else None
    )
    envelope_role = envelope_author.get("role") if isinstance(envelope_author, dict) else None
    has_valid_envelope = bool(
        isinstance(envelope_message, dict)
        and isinstance(envelope_role, str)
        and envelope_role in _KNOWN_MESSAGE_ROLES
    )
    if path == "" and operation == "patch":
        if not isinstance(value, list):
            return True
        return any(
            not isinstance(subpatch, dict) or _patch_mutates_current_message(subpatch)
            for subpatch in value
        )
    if path == "":
        if operation in ("add", "replace") and has_valid_envelope:
            return False
        return True
    # A scalar/bare ``v`` continues the last path in the v1 protocol. The image
    # adapter deliberately does not retain or reconstruct that unbounded state,
    # so any such continuation after a candidate must revoke it. A full implicit
    # envelope is handled and revalidated separately.
    return bool(
        path is None
        and operation is None
        and "v" in event
        and not has_valid_envelope
    )


def _image_payloads(prompt: str, model: str) -> tuple[dict, dict]:
    if not isinstance(prompt, str) or not prompt or len(prompt) > _MAX_TOOL_TEXT_CHARS:
        raise BackendContractError("image_generation", "prompt must be a non-empty bounded string")
    model = _backend_path_id(model, adapter="image_generation", field="model")
    message_id = str(uuid4())
    parent_message_id = str(uuid4())
    message = {
        "id": message_id,
        "author": {"role": "user"},
        "create_time": time.time(),
        "content": {"content_type": "text", "parts": [prompt]},
        "metadata": {
            "system_hints": ["picture_v2"],
            "serialization_metadata": {"custom_symbol_offsets": []},
        },
    }
    common = {
        "action": "next",
        "parent_message_id": parent_message_id,
        "model": model,
        "timezone_offset_min": -480,
        "timezone": "UTC",
        "conversation_mode": {"kind": "primary_assistant"},
        "history_and_training_disabled": False,
        "system_hints": ["picture_v2"],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": {"app_name": "chatgpt.com"},
    }
    prepare = {
        **common,
        "client_prepare_state": "success",
        "partial_query": message,
    }
    generate = {
        **common,
        "client_prepare_state": "sent",
        "messages": [message],
        "force_parallel_switch": "auto",
    }
    return prepare, generate


_CONNECTOR_ACTION_RE = re.compile(
    r"(?:^|[/\\:])connector_[a-z0-9_]+/"
    r"(?:start|run|invoke|search|execute)(?:$|[/?#])",
    re.IGNORECASE,
)


def _connector_dispatch_category(text: object) -> str | None:
    """Classify one bounded recipient-all connector dispatch envelope.

    Some account backends address a connector dispatch to ``recipient=all``
    and place its private arguments in an assistant text part.  Recognize the
    JSON structure rather than one exact serialization so that whitespace,
    key order, and connector names cannot turn the private envelope into user
    output.
    """
    if not isinstance(text, str) or not text or len(text) > _MAX_TOOL_TEXT_CHARS:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("args"), dict):
        return None
    path = payload.get("path")
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 2_048
        or _CONNECTOR_ACTION_RE.search(path) is None
    ):
        return None

    target_tokens = frozenset(re.findall(r"[a-z0-9]+", path.casefold()))
    if target_tokens & {"browser", "search", "web"} or {
        "deep",
        "research",
    }.issubset(target_tokens):
        return "web"
    if target_tokens & {"code", "interpreter", "python"}:
        return "code_execution"
    if "image" in target_tokens:
        return "image_generation"
    if "canvas" in target_tokens:
        return "canvas"
    return "connector"


def _message_connector_dispatch_category(message: object) -> str | None:
    if not isinstance(message, dict):
        return None
    author = message.get("author")
    if not isinstance(author, dict) or author.get("role") != "assistant":
        return None
    if message.get("recipient") not in (None, "all"):
        return None
    content = message.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        return None
    return _connector_dispatch_category(parts[0])


def _tool_activity_category(message: object) -> str | None:
    """Map a backend tool lifecycle to one non-sensitive audit category."""
    if not isinstance(message, dict):
        return None
    author = message.get("author")
    if not isinstance(author, dict):
        return None
    role = author.get("role")
    if role == "tool":
        return "tool_response"
    if role != "assistant":
        return None

    dispatch_category = _message_connector_dispatch_category(message)
    if dispatch_category is not None:
        return dispatch_category

    recipient = message.get("recipient")
    content = message.get("content")
    content_type = content.get("content_type") if isinstance(content, dict) else None
    if recipient in (None, "all") and content_type != "code":
        return None

    target = recipient.casefold() if isinstance(recipient, str) else ""
    target_tokens = frozenset(re.findall(r"[a-z0-9]+", target))
    if target_tokens & {"browser", "search", "web"} or {
        "deep",
        "research",
    }.issubset(target_tokens):
        return "web"
    if content_type == "code" or target_tokens & {
        "code",
        "interpreter",
        "python",
    }:
        return "code_execution"
    if "image" in target_tokens:
        return "image_generation"
    if "canvas" in target_tokens:
        return "canvas"
    return "connector"


def _append_tool_activity_receipt(text: str, categories: set[str]) -> str:
    """Append the authoritative final receipt without exposing private payloads."""
    ordered = [name for name in _TOOL_ACTIVITY_CATEGORIES if name in categories]
    if not ordered:
        ordered = ["none"]
    labels = ", ".join(f"`{name}`" for name in ordered)
    receipt = f"Tool activity receipt: {labels}. Private dispatch payloads were withheld."
    body = text or "(no response)"
    return f"{body}\n\n---\n{receipt}"


class _IncompleteStreamError(RuntimeError):
    def __init__(
        self,
        conversation_id: str | None = None,
        tool_activity: set[str] | None = None,
    ) -> None:
        super().__init__(_INCOMPLETE_RESPONSE_MESSAGE)
        self.conversation_id = conversation_id
        self.tool_activity = frozenset(tool_activity or ())


def _is_successful_assistant_terminal(message: dict) -> bool:
    author = message.get("author")
    content = message.get("content")
    recipient = message.get("recipient")
    return (
        is_user_visible_message(message)
        and isinstance(author, dict)
        and author.get("role") == "assistant"
        and recipient in (None, "all")
        and isinstance(content, dict)
        and content.get("content_type") in ("text", "multimodal_text")
        and message.get("status") == "finished_successfully"
        and _message_connector_dispatch_category(message) is None
    )


def _is_visible_assistant_message(message: dict) -> bool:
    author = message.get("author")
    content = message.get("content")
    return (
        is_user_visible_message(message)
        and isinstance(author, dict)
        and author.get("role") == "assistant"
        and message.get("recipient") in (None, "all")
        and isinstance(content, dict)
        and content.get("content_type") in ("text", "multimodal_text")
        and _message_connector_dispatch_category(message) is None
    )


def _bounded_lifecycle_timestamp(
    raw_time: object,
    *,
    adapter: str,
) -> int | float | None:
    """Return one exact bounded create_time value, or null for insertion order."""
    if raw_time is None:
        return None
    if isinstance(raw_time, bool) or not isinstance(raw_time, (int, float)):
        raise BackendContractError(
            adapter, "message create_time must be a finite number or null"
        )
    if isinstance(raw_time, int):
        if abs(raw_time) > _MAX_TOOL_INTEGER:
            raise BackendContractError(
                adapter, "message create_time must be a bounded number or null"
            )
        return raw_time
    if not math.isfinite(raw_time) or abs(raw_time) > _MAX_TOOL_INTEGER:
        raise BackendContractError(
            adapter, "message create_time must be a bounded number or null"
        )
    return raw_time


def _ordered_poll_lifecycles(
    mapping: dict,
    *,
    adapter: str,
) -> list[tuple[tuple[int | float, int], dict]]:
    """Return validated messages in one deterministic lifecycle order.

    Backend snapshots normally carry numeric ``create_time`` values.  Older
    shapes can omit them, in which case insertion order is the only available
    ordering signal and is used consistently for the entire snapshot. Mixed
    timestamp modes are ambiguous and fail closed; a newer non-output message
    such as a user turn must still invalidate an earlier terminal candidate.
    """
    records: list[tuple[int, int | float | None, dict]] = []
    any_missing_time = False
    any_present_time = False
    for index, node in enumerate(mapping.values()):
        if node is None:
            continue
        if not isinstance(node, dict):
            raise BackendContractError(adapter, "mapping node must be an object or null")
        message = node.get("message")
        if message is None:
            continue
        if not isinstance(message, dict):
            raise BackendContractError(adapter, "mapping message must be an object or null")
        author = message.get("author")
        if not isinstance(author, dict):
            raise BackendContractError(adapter, "message author must be an object")
        role = author.get("role")
        if not isinstance(role, str) or role not in _KNOWN_MESSAGE_ROLES:
            raise BackendContractError(adapter, "message author role is invalid")
        metadata = message.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise BackendContractError(adapter, "message metadata must be an object")
        _validate_metadata_bounds(metadata, adapter=adapter)

        timestamp = _bounded_lifecycle_timestamp(
            message.get("create_time"),
            adapter=adapter,
        )
        if timestamp is None:
            any_missing_time = True
        else:
            any_present_time = True
        records.append((index, timestamp, message))

    if any_missing_time and any_present_time:
        raise BackendContractError(adapter, "message create_time values must use one ordering mode")

    ordered = [
        (
            (index if any_missing_time else timestamp, index),
            message,
        )
        for index, timestamp, message in records
    ]
    ordered.sort(key=lambda item: item[0])
    return ordered


def _poll_terminal_text(message: dict, *, adapter: str) -> str | None:
    if not _is_successful_assistant_terminal(message):
        return None
    content = message.get("content")
    assert isinstance(content, dict)
    parts = content.get("parts")
    if not isinstance(parts, list) or len(parts) > _MAX_TOOL_PARTS:
        raise BackendContractError(adapter, "message parts must be an array of at most 100 items")
    if not parts:
        return None
    text = parts[0]
    if not isinstance(text, str) or not text:
        return None
    if len(text) > _MAX_TOOL_TEXT_CHARS:
        raise BackendContractError(adapter, "assistant text exceeds the size limit")
    return text


def _raise_for_sse_error(obj: dict, *, route: str = "/backend-api/conversation") -> None:
    """Surface an in-band error without retaining its account-content body."""
    has_error = False
    err = obj.get("error")
    if err is not None:
        has_error = True
    if obj.get("type") in ("error", "conversation_error"):
        has_error = True
    if not has_error:
        return
    raise BackendHTTPError(
        "STREAM",
        route,
        None,
        code="temporarily_failed",
        retryable=True,
    )


def _decode_sse_object(data: str, *, adapter: str) -> dict | None:
    """Decode one bounded SSE payload without leaking raw parser failures."""
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise BackendContractError(adapter, "event payload must be valid JSON") from None
    return payload if isinstance(payload, dict) else None


async def _post_account_stream(session, url: str, route: str, **kwargs):
    """Issue one stream POST and normalize network failures without body data."""
    response_oversized = False
    network_failed = False
    _disable_tls_key_logging()
    try:
        kwargs["allow_redirects"] = False
        response = await session.post(url, **kwargs)
    except Exception as exc:
        response_oversized = _is_filesize_exceeded(exc)
        network_failed = not response_oversized
    if response_oversized:
        raise BackendContractError(f"sse {route}", "stream exceeds 64 MiB") from None
    if network_failed:
        raise BackendHTTPError(
            "POST", route, None, code="temporarily_failed", retryable=True
        ) from None
    return response


def _stream_session() -> AsyncSession:
    """Build an account stream session with libcurl's native size ceiling."""
    return AsyncSession(**_account_session_options(_MAX_SSE_STREAM_BYTES))


async def _bounded_sse_lines(response, *, route: str) -> AsyncIterator[str]:
    """Parse response bytes while bounding both the current line and stream."""
    total = 0
    pending = bytearray()
    swallow_lf = False

    iterator_failed = False
    iterator_oversized = False
    try:
        iterator = response.aiter_content().__aiter__()
    except Exception as exc:
        iterator_failed = True
        iterator_oversized = _is_filesize_exceeded(exc)
    if iterator_failed:
        if iterator_oversized:
            raise BackendContractError(f"sse {route}", "stream exceeds 64 MiB") from None
        raise BackendHTTPError(
            "STREAM", route, None, code="temporarily_failed", retryable=True
        ) from None

    while True:
        read_failed = False
        read_oversized = False
        finished = False
        try:
            raw_chunk = await anext(iterator)
        except StopAsyncIteration:
            finished = True
        except Exception as exc:
            read_failed = True
            read_oversized = _is_filesize_exceeded(exc)

        if finished:
            break
        if read_failed:
            if read_oversized:
                raise BackendContractError(f"sse {route}", "stream exceeds 64 MiB") from None
            raise BackendHTTPError(
                "STREAM", route, None, code="temporarily_failed", retryable=True
            ) from None
        if not isinstance(raw_chunk, (bytes, bytearray)):
            raise BackendContractError(f"sse {route}", "stream chunk must be bytes")
        if not raw_chunk:
            continue

        chunk_size = len(raw_chunk)
        if chunk_size > _MAX_SSE_STREAM_BYTES - total:
            raise BackendContractError(f"sse {route}", "stream exceeds 64 MiB")
        total += chunk_size
        chunk = raw_chunk if isinstance(raw_chunk, bytes) else bytes(raw_chunk)

        position = 0
        if swallow_lf:
            swallow_lf = False
            if chunk[0] == 0x0A:
                position = 1

        while position < len(chunk):
            cr = chunk.find(b"\r", position)
            lf = chunk.find(b"\n", position)
            if cr < 0:
                delimiter = lf
            elif lf < 0:
                delimiter = cr
            else:
                delimiter = min(cr, lf)

            if delimiter < 0:
                fragment = chunk[position:]
                if len(fragment) > _MAX_SSE_LINE_BYTES - len(pending):
                    raise BackendContractError(f"sse {route}", "event line exceeds 4 MiB")
                pending.extend(fragment)
                break

            fragment = chunk[position:delimiter]
            if len(fragment) > _MAX_SSE_LINE_BYTES - len(pending):
                raise BackendContractError(f"sse {route}", "event line exceeds 4 MiB")
            pending.extend(fragment)
            yield pending.decode("utf-8", errors="replace")
            pending.clear()

            if chunk[delimiter] == 0x0D:
                if delimiter + 1 == len(chunk):
                    swallow_lf = True
                    position = delimiter + 1
                elif chunk[delimiter + 1] == 0x0A:
                    position = delimiter + 2
                else:
                    position = delimiter + 1
            else:
                position = delimiter + 1

    if pending:
        yield pending.decode("utf-8", errors="replace")


def _raise_http_status(route: str, status_code: int) -> None:
    raise backend_http_error("POST", route, status_code)


# /backend-api/f/conversation — the frontend-facing endpoint used by the web app.
# Required for Deep Research heavy path; regular /conversation also works for normal chat.
_F_CONV_URL = _BASE + "/backend-api/f/conversation"

#: Model slug for legacy Deep Research (resolves to i-mini-m / web-search backend)
DR_MODEL = "research"

#: Model slug for heavy Deep Research — gpt-5-5-pro with extended thinking + DR connector
HEAVY_DR_MODEL = "gpt-5-5-pro"

#: System hint for heavy Deep Research (connector identifier from chatgpt.com frontend)
HEAVY_DR_HINT = "connector:connector_openai_deep_research"


def _raw_dump(obj: dict, *, phase: str) -> None:
    """Reject the removed raw-payload persistence escape hatch."""
    del obj, phase
    if os.environ.get("GPT2AGENT_RAW_DUMP"):
        raise RuntimeError(
            "GPT2AGENT_RAW_DUMP is no longer supported because it persisted "
            "private account payloads; unset GPT2AGENT_RAW_DUMP to continue"
        )


def _has_citation_payload(meta: dict | None) -> bool:
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("content_references") or meta.get("search_result_groups"))


def _citation_payload(*metas: dict | None) -> tuple[list, list]:
    refs: list = []
    groups: list = []
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        if not refs:
            refs = meta.get("content_references") or []
        if not groups:
            groups = meta.get("search_result_groups") or []
    return refs, groups


#: Prefix a tool node uses when the connector widget state is replayed into the
#: conversation transcript as plain text.
_WIDGET_STATE_TEXT_PREFIX = "The latest state of the widget is: "
_DR_APP_RESOURCE = "Deep Research App_start"
_DR_CONNECTOR_ID = "connector_openai_deep_research"
_DR_CONNECTOR_URI = f"connectors://{_DR_CONNECTOR_ID}"
_DR_RESOURCE_URI = f"/{_DR_CONNECTOR_ID}/implicit_link::{_DR_CONNECTOR_ID}/start"
_DR_WIDGET_EXCLUSIVE_KEY = f"widget_state:{_DR_APP_RESOURCE}"


def _coerce_widget_state(obj: object) -> dict | None:
    """Return the widget-state dict from a dict or a JSON-string carrier.

    The Deep Research App connector exposes the widget state two ways: as a
    ``"The latest state of the widget is: {…}"`` text part (tool node) and as a
    JSON string under ``message.metadata.chatgpt_sdk.widget_state`` (returned
    when the conversation is fetched with ``include_widget_state=true``). Both
    decode to the same object; some carriers wrap it as ``{"widget_state": {…}}``.
    """
    if isinstance(obj, str):
        brace = obj.find("{")
        if brace < 0:
            return None
        try:
            obj = json.loads(obj[brace:])
        except (json.JSONDecodeError, ValueError, RecursionError):
            return None
    if not isinstance(obj, dict):
        return None
    if "report_message" in obj:
        return obj
    inner = obj.get("widget_state")
    return inner if isinstance(inner, dict) else None


def _dr_widget_report_candidates(detail: dict | None) -> list[tuple[dict, str, list]]:
    """Return validated ``(carrier, report_text, references)`` candidates.

    The carrier object is retained so a polling caller can require the widget
    report to belong to the authoritative latest lifecycle.  This provenance
    must not be discarded before lifecycle ordering is checked: an older,
    completed widget can remain in the mapping while a newer tool call is still
    running.
    """
    mapping = (detail or {}).get("mapping") or {}
    if not isinstance(mapping, dict):
        return []
    candidates: list[tuple[dict, str, list]] = []
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        author = msg.get("author") or {}
        metadata = msg.get("metadata") or {}
        content = msg.get("content") or {}
        if (
            not isinstance(author, dict)
            or not isinstance(metadata, dict)
            or not isinstance(content, dict)
            or author.get("role") != "tool"
            or msg.get("recipient") != "all"
            or msg.get("status") != "finished_successfully"
        ):
            continue
        carriers: list[object] = []
        parts = content.get("parts")
        if parts is None:
            parts = []
        if not isinstance(parts, list) or len(parts) > _MAX_TOOL_PARTS:
            raise BackendContractError(
                "heavy_deep_research",
                "widget carrier parts must be an array of at most 100 items",
            )
        if (
            author.get("name") == "api_tool.widget_state"
            and content.get("content_type") == "text"
            and metadata.get("exclusive_key") == _DR_WIDGET_EXCLUSIVE_KEY
            and metadata.get("is_visually_hidden_from_conversation") is True
            and parts
            and isinstance(parts[0], str)
            and parts[0].startswith(_WIDGET_STATE_TEXT_PREFIX)
        ):
            carriers.append(parts[0])
        sdk = metadata.get("chatgpt_sdk")
        invoked_resource = metadata.get("invoked_resource")
        if (
            author.get("name") == "api_tool.call_tool"
            and content.get("content_type") == "code"
            and isinstance(sdk, dict)
            and sdk.get("resource_name") == _DR_APP_RESOURCE
            and sdk.get("attribution_id") == _DR_CONNECTOR_ID
            and sdk.get("resolved_pineapple_uri") == _DR_CONNECTOR_URI
            and sdk.get("distribution_channel") == "openai"
            and sdk.get("connector_type") == "FIRST_PARTY_ECOSYSTEM"
            and isinstance(invoked_resource, dict)
            and invoked_resource.get("resource_uri") == _DR_RESOURCE_URI
            and sdk.get("widget_state") is not None
        ):
            carriers.append(sdk["widget_state"])
        for carrier in carriers:
            state = _coerce_widget_state(carrier)
            report = (state or {}).get("report_message")
            if not isinstance(report, dict):
                continue
            # Only emit the exact completed shape observed from the connector.
            if (state or {}).get("status") != "completed" or report.get(
                "status"
            ) != "finished_successfully":
                continue
            report_content = report.get("content") or {}
            if not isinstance(report_content, dict) or report_content.get("content_type") != "text":
                continue
            rparts = report_content.get("parts")
            if rparts is None:
                rparts = []
            if not isinstance(rparts, list) or len(rparts) > _MAX_TOOL_PARTS:
                raise BackendContractError(
                    "heavy_deep_research",
                    "widget report parts must be an array of at most 100 items",
                )
            if rparts and not isinstance(rparts[0], str):
                raise BackendContractError(
                    "heavy_deep_research", "widget report first part must be a string"
                )
            text = rparts[0] if rparts else ""
            if not text:
                continue
            if len(text) > _MAX_TOOL_TEXT_CHARS:
                raise BackendContractError(
                    "heavy_deep_research", "widget report text exceeds the size limit"
                )
            report_metadata = report.get("metadata") or {}
            if not isinstance(report_metadata, dict):
                raise BackendContractError(
                    "heavy_deep_research", "widget report metadata must be an object"
                )
            _validate_metadata_bounds(report_metadata, adapter="heavy_deep_research")
            refs = report_metadata.get("content_references") or []
            if not isinstance(refs, list):
                raise BackendContractError(
                    "heavy_deep_research", "widget references must be an array"
                )
            candidates.append((msg, text, refs))
    return candidates


def _dr_report_from_widget_state(detail: dict | None) -> tuple[str, list]:
    """Recover the Deep Research report from a connector widget-state node.

    The "Deep Research App" connector (pineapple URI
    ``connectors://connector_openai_deep_research``) never writes its final
    report as an assistant text node in the conversation ``mapping``; the report
    text lives in ``widget_state.report_message`` and renders client-side. This
    walks the mapping for either widget-state carrier (see
    :func:`_coerce_widget_state`) and returns ``(report_text, content_references)``
    for the longest *completed* report found, or ``("", [])`` if none is present.

    Hardening (audits 2026-06-18 and 2026-07-10):

    * Only ``tool`` nodes with the observed Deep Research-specific backend
      envelope are accepted. Arbitrary assistant, user, or tool content with a
      widget-shaped payload is ignored. These envelope checks are fail-closed
      provenance validation, not cryptographic authentication; the payload itself
      is unsigned and can still contain incorrect or prompt-injected research.
    * The text carrier must *start with* the prefix, not merely contain it.
    * An in-progress draft is ignored: both the top-level widget status and the
      nested report status must carry their explicit completed values, so polling
      never emits a half-written report as the final answer.
    """
    candidates = _dr_widget_report_candidates(detail)
    if not candidates:
        return "", []
    _, text, refs = max(candidates, key=lambda candidate: len(candidate[1]))
    return text, refs


def _is_ascii_pointer_index(part: str) -> bool:
    return bool(part) and part.isascii() and part.isdecimal()


def _validate_metadata_bounds(value: object, *, adapter: str) -> None:
    """Validate one JSON metadata tree before copying or projecting it."""
    stack: list[tuple[object, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    node_count = 0
    string_chars = 0
    while stack:
        current, depth = stack.pop()
        node_count += 1
        if node_count > _MAX_METADATA_NODES:
            raise BackendContractError(adapter, "metadata exceeds the node limit")
        if depth > _MAX_METADATA_STRUCTURE_DEPTH:
            raise BackendContractError(adapter, "metadata exceeds the nesting limit")
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen_containers:
                raise BackendContractError(adapter, "metadata must be an acyclic JSON tree")
            seen_containers.add(identity)
            for key, child in current.items():
                if not isinstance(key, str) or len(key) > _MAX_METADATA_POINTER_CHARS:
                    raise BackendContractError(adapter, "metadata object key is invalid")
                string_chars += len(key)
                if string_chars > _MAX_METADATA_STRING_CHARS:
                    raise BackendContractError(adapter, "metadata exceeds the string size limit")
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            identity = id(current)
            if identity in seen_containers:
                raise BackendContractError(adapter, "metadata must be an acyclic JSON tree")
            seen_containers.add(identity)
            if len(current) > _MAX_METADATA_LIST_ITEMS:
                raise BackendContractError(adapter, "metadata list exceeds the size limit")
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str):
            string_chars += len(current)
            if string_chars > _MAX_METADATA_STRING_CHARS:
                raise BackendContractError(adapter, "metadata exceeds the string size limit")
        elif current is None or isinstance(current, (bool, int)):
            continue
        elif isinstance(current, float) and math.isfinite(current):
            continue
        else:
            raise BackendContractError(adapter, "metadata must contain JSON values")


def _pointer_parts(
    path: str,
    prefix: str,
    *,
    adapter: str = "conversation_stream",
) -> list[str]:
    if len(path) > _MAX_METADATA_POINTER_CHARS:
        raise BackendContractError(adapter, "metadata pointer exceeds the size limit")
    tail = path[len(prefix) :].strip("/")
    if not tail:
        return []
    raw_parts = tail.split("/")
    if len(raw_parts) > _MAX_METADATA_POINTER_DEPTH:
        raise BackendContractError(adapter, "metadata pointer exceeds the depth limit")
    parts: list[str] = []
    for raw_part in raw_parts:
        if re.search(r"~(?:[^01]|$)", raw_part):
            raise BackendContractError(adapter, "metadata pointer escape is invalid")
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if part.isnumeric() and not _is_ascii_pointer_index(part):
            raise BackendContractError(adapter, "metadata pointer array index is invalid")
        if _is_ascii_pointer_index(part) and int(part) > _MAX_METADATA_ARRAY_INDEX:
            raise BackendContractError(adapter, "metadata pointer array index is too large")
        parts.append(part)
    return parts


def _new_container(next_part: str) -> list | dict:
    return [] if next_part == "-" or _is_ascii_pointer_index(next_part) else {}


def _ensure_list_slot(
    seq: list,
    part: str,
    value_factory,
    *,
    adapter: str = "conversation_stream",
):
    if part == "-":
        if len(seq) >= _MAX_METADATA_LIST_ITEMS:
            raise BackendContractError(adapter, "metadata list exceeds the size limit")
        seq.append(value_factory())
        return seq[-1]
    if not _is_ascii_pointer_index(part):
        raise BackendContractError(adapter, "metadata list pointer must be an index")
    idx = int(part)
    if idx > _MAX_METADATA_ARRAY_INDEX:
        raise BackendContractError(adapter, "metadata pointer array index is too large")
    while len(seq) <= idx:
        if len(seq) >= _MAX_METADATA_LIST_ITEMS:
            raise BackendContractError(adapter, "metadata list exceeds the size limit")
        seq.append(None)
    if not isinstance(seq[idx], (dict, list)):
        seq[idx] = value_factory()
    return seq[idx]


def _merge_metadata_path(
    meta: dict,
    path: str,
    op: str,
    value,
    *,
    adapter: str = "conversation_stream",
) -> dict:
    _validate_metadata_bounds(meta, adapter=adapter)
    if path == "/message/metadata":
        if op in ("append", "patch") and isinstance(value, dict):
            _validate_metadata_bounds(value, adapter=adapter)
            merged = {**meta, **value}
            _validate_metadata_bounds(merged, adapter=adapter)
            return merged
        if op in ("add", "replace") and isinstance(value, dict):
            _validate_metadata_bounds(value, adapter=adapter)
            return value
        return meta

    if not path.startswith("/message/metadata/"):
        return meta

    out = deepcopy(meta)
    parts = _pointer_parts(path, "/message/metadata", adapter=adapter)
    if not parts:
        return out

    cur: dict | list = out
    for i, part in enumerate(parts[:-1]):
        next_part = parts[i + 1]
        if isinstance(cur, dict):
            nxt = cur.get(part)
            if not isinstance(nxt, (dict, list)):
                nxt = _new_container(next_part)
                cur[part] = nxt
            cur = nxt
        elif isinstance(cur, list):
            nxt = _ensure_list_slot(
                cur,
                part,
                lambda: _new_container(next_part),
                adapter=adapter,
            )
            cur = nxt

    key = parts[-1]
    if isinstance(cur, dict):
        if op == "append":
            existing = cur.get(key)
            if isinstance(existing, list):
                if len(existing) >= _MAX_METADATA_LIST_ITEMS:
                    raise BackendContractError(adapter, "metadata list exceeds the size limit")
                cur[key] = [*existing, value]
            elif isinstance(existing, str) and isinstance(value, str):
                cur[key] = existing + value
            else:
                cur[key] = value
        elif op == "patch" and isinstance(value, dict) and isinstance(cur.get(key), dict):
            cur[key] = {**cur[key], **value}
        else:
            cur[key] = value
    elif isinstance(cur, list):
        if key == "-" or op == "append":
            if len(cur) >= _MAX_METADATA_LIST_ITEMS:
                raise BackendContractError(adapter, "metadata list exceeds the size limit")
            cur.append(value)
        elif _is_ascii_pointer_index(key):
            idx = int(key)
            if idx > _MAX_METADATA_ARRAY_INDEX:
                raise BackendContractError(adapter, "metadata pointer array index is too large")
            while len(cur) <= idx:
                if len(cur) >= _MAX_METADATA_LIST_ITEMS:
                    raise BackendContractError(adapter, "metadata list exceeds the size limit")
                cur.append(None)
            cur[idx] = value
        else:
            raise BackendContractError(adapter, "metadata list pointer must be an index")
    _validate_metadata_bounds(out, adapter=adapter)
    return out


def _is_connector_dispatch_text(text: str) -> bool:
    return _connector_dispatch_category(text) is not None


def _build_payload(
    model: str,
    messages: list[dict],
    *,
    gizmo_id: str | None = None,
    temporary: bool = True,
    thinking_effort: str | None = None,
) -> dict:
    payload: dict = {
        "action": "next",
        "messages": [
            {
                "id": str(uuid4()),
                "author": {"role": m["role"]},
                "content": {"content_type": "text", "parts": [m["content"]]},
            }
            for m in messages
        ],
        "parent_message_id": str(uuid4()),
        "model": model,
        "conversation_mode": {"kind": "primary_assistant"},
        "force_paragen": False,
        "force_rate_limit": False,
        "force_use_sse": True,
        "timezone_offset_min": -480,
        "history_and_training_disabled": temporary,
        "system_hints": [],
    }
    if gizmo_id:
        payload["gizmo_id"] = gizmo_id
        payload["conversation_origin"] = {"type": "custom_gpt", "gizmo_id": gizmo_id}
    if thinking_effort is not None:
        payload["thinking_effort"] = thinking_effort
    return payload


def _build_dr_payload(
    query: str,
    *,
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
) -> dict:
    """Build payload for legacy Deep Research: model=research + system_hints=['research'].

    This resolves to i-mini-m (web-search/SearchGPT backend), NOT the Pro-tier
    multi-section deep research.  Use _build_heavy_dr_payload() for the full DR.

    NOTE: history_and_training_disabled must be False here. ChatGPT refuses
    Deep Research in "temporary chats" (the True setting), returning
    "Research is not currently supported in temporary chats". DR requires a
    persistent conversation so the connector can poll for the final report.

    When ``conversation_id`` + ``parent_message_id`` are supplied, the payload
    continues an existing conversation — used by multi-turn clarification
    handling in ``ConversationClient.deep_research``.
    """
    payload = _build_payload(DR_MODEL, [{"role": "user", "content": query}])
    payload["system_hints"] = ["research"]
    payload["history_and_training_disabled"] = False
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if parent_message_id:
        payload["parent_message_id"] = parent_message_id
    return payload


_CLARIFICATION_HINTS = (
    "could you confirm",
    "could you clarify",
    "could you tell me",
    "would you like",
    "do you want me",
    "shall i proceed",
    "before i start",
    "before i begin",
    "to make sure",
    "to ensure i",
    "i'd like to clarif",
    "i'd like to confirm",
    "can you specify",
    "just one key clarif",
    "one quick clarif",
    "one clarif",
    "a quick question",
    "i have one question",
    "i need to confirm",
)

_DR_AUTO_PROCEED = (
    "Proceed with your best interpretation of any ambiguity. "
    "Do not ask further clarifying questions. Begin the research now."
)

# A genuine clarification request is a question or a short list of questions —
# a few sentences. Anything longer is a report, even if its prose happens to
# contain a hint phrase ("to make sure the comparison is fair, …").
_CLARIFICATION_MAX_LEN = 1200


def _looks_like_clarification(text: str) -> bool:
    """Heuristic: does this assistant 'done' text look like a clarification request?

    Matches a curated phrase list ("could you confirm", "before I start", "just
    one key clarification", "shall I proceed", etc.), but only on short text
    (≤ _CLARIFICATION_MAX_LEN chars). Without the length ceiling, a real
    multi-thousand-word report containing an ordinary phrase like "to make
    sure" anywhere in its body trips the substring match — the wrapper then
    burns a DR round on _DR_AUTO_PROCEED and overwrites the real report with
    the follow-up response. The earlier "short text ending in ?" branch stays
    removed — real reports often end with rhetorical questions. The heuristic
    is conservative; if a clarification slips through, the caller still gets
    the question text and can re-invoke explicitly.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > _CLARIFICATION_MAX_LEN:
        return False
    lower = stripped.lower()
    return any(p in lower for p in _CLARIFICATION_HINTS)


def _build_heavy_dr_payload(query: str, *, model: str | None = None) -> dict:
    """Build payload for heavy Deep Research — the true Pro-tier 5–30 min DR path.

    Ground-truth reverse-engineered from chatgpt.com/deep-research browser traffic
    (2026-04-23).  Key differences from legacy DR:

    * URL target: /backend-api/f/conversation  (frontend endpoint)
    * model: gpt-5-5-pro
    * system_hints: ["connector:connector_openai_deep_research"]
    * thinking_effort: "extended"
    * message.metadata contains deep_research_version / venus_model_variant / caterpillar fields

    The server_ste_metadata from the SSE stream will show tool_name="ApiToolWrapper"
    and tool_invoked=true, confirming the DR connector fired.  The resolved_model_slug
    in the user-message echo is "i-mini-m" (the orchestration layer); the actual heavy
    reasoning runs as a background tool call inside the connector.

    Rate-limited by the account-reported "deep_research" feature quota; limits
    and reset timing can vary.
    """
    msg_id = str(uuid4())
    return {
        "action": "next",
        "messages": [
            {
                "id": msg_id,
                "author": {"role": "user"},
                "create_time": time.time(),
                "content": {"content_type": "text", "parts": [query]},
                "metadata": {
                    "caterpillar_selected_sources": [],
                    "developer_mode_connector_ids": [],
                    "selected_mcp_sources": [],
                    "selected_sources": [],
                    "selected_github_repos": [],
                    "selected_all_github_repos": False,
                    "system_hints": [HEAVY_DR_HINT],
                    "deep_research_version": "standard",
                    "venus_model_variant": "standard",
                    "serialization_metadata": {"custom_symbol_offsets": []},
                    "user_timezone": "UTC",
                },
            }
        ],
        "parent_message_id": str(uuid4()),
        "model": model or HEAVY_DR_MODEL,
        "client_prepare_state": "success",
        "timezone_offset_min": -480,
        "timezone": "UTC",
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_followups": True,
        "system_hints": [HEAVY_DR_HINT],
        "thinking_effort": "extended",
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "force_parallel_switch": "auto",
        "paragen_cot_summary_display_override": "allow",
        # MUST be False — Deep Research is rejected by the server in
        # "temporary chats" ("Research is not currently supported in temporary chats").
        # DR also depends on the conversation persisting so Phase 2 polling
        # at /backend-api/conversation/{id} can fetch the final report.
        "history_and_training_disabled": False,
        "force_use_sse": True,
    }


class ConversationClient:
    def __init__(self, backend: BackendClient) -> None:
        self._backend = backend

    async def stream(
        self,
        model: str,
        messages: list[dict],
        tools: list | None = None,
        *,
        gizmo_id: str | None = None,
        temporary: bool = True,
        thinking_effort: str | None = None,
        auth_headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[str | dict]:
        # Buffers each backend message until later visibility patches can no longer
        # revoke it, then yields text chunks (str). The optional final dict is one
        # combined private sentinel for ``complete()``; callers that join chunks
        # must skip non-str items.
        headers = (
            dict(auth_headers) if auth_headers is not None else self._backend.request_headers()
        )
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"

        sentinel = await SentinelGate(self._backend).get_tokens(headers)
        headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel["chat-requirements"]
        if sentinel.get("proof"):
            headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
        if sentinel.get("turnstile"):
            headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]

        payload = _build_payload(
            model,
            messages,
            gizmo_id=gizmo_id,
            temporary=temporary,
            thinking_effort=thinking_effort,
        )
        if tools:
            payload["tools"] = tools

        async with _stream_session() as s:
            resp = await _post_account_stream(
                s,
                _CONV_URL,
                "/backend-api/conversation",
                headers=headers,
                json=payload,
                timeout=300,
                stream=True,
            )
            if resp.status_code not in (200, 201):
                _raise_http_status("/backend-api/conversation", resp.status_code)

            current_msg_id: str | None = None
            have_current_message = False
            last_text_parts: list[str] = []
            last_text_length = 0
            current_candidate_key: str | None = None
            candidate_order: list[str] = []
            candidate_text: dict[str, str] = {}
            revoked_message_ids: set[str] = set()
            _conversation_id: str | None = None
            done_received = False
            message_completed = False
            current_message_visible = False
            current_visibility_revoked = False
            current_message_role = ""
            current_metadata: dict = {}
            last_path: str | None = None
            tool_activity: set[str] = set()

            def _set_current_text(value: str) -> None:
                nonlocal last_text_length
                if len(value) > _MAX_TOOL_TEXT_CHARS:
                    raise BackendContractError(
                        "conversation_stream", "assistant text exceeds the size limit"
                    )
                last_text_parts.clear()
                if value:
                    last_text_parts.append(value)
                last_text_length = len(value)

            def _save_current() -> None:
                if current_candidate_key is None:
                    return
                text = "".join(last_text_parts)
                dispatch_category = (
                    _connector_dispatch_category(text)
                    if current_message_role == "assistant"
                    else None
                )
                if dispatch_category is not None:
                    tool_activity.add(dispatch_category)
                    candidate_text.pop(current_candidate_key, None)
                    return
                if current_message_visible and text:
                    candidate_text[current_candidate_key] = text
                else:
                    candidate_text.pop(current_candidate_key, None)

            def _reset_if_new_msg(msg_id: str | None) -> bool:
                """Start a patch target while retaining revocable candidates by ID."""
                nonlocal current_msg_id, have_current_message
                nonlocal current_message_visible, current_message_role
                nonlocal current_metadata, current_visibility_revoked, last_path
                nonlocal current_candidate_key
                is_new = not have_current_message or (
                    msg_id is not None and msg_id != current_msg_id
                )
                if is_new:
                    if have_current_message:
                        _save_current()
                    current_msg_id = msg_id
                    have_current_message = True
                    current_candidate_key = (
                        f"message:{msg_id}" if msg_id is not None else "message:unknown"
                    )
                    if current_candidate_key not in candidate_order:
                        if len(candidate_order) >= _MAX_TOOL_RECORDS:
                            raise BackendContractError(
                                "conversation_stream",
                                "message candidate list exceeds 100 records",
                            )
                        candidate_order.append(current_candidate_key)
                    _set_current_text("")
                    current_message_visible = False
                    current_visibility_revoked = bool(
                        msg_id is not None and msg_id in revoked_message_ids
                    )
                    current_message_role = ""
                    current_metadata = {}
                    last_path = None
                    return True
                return False

            def _track_message_lifecycle(message: dict) -> None:
                nonlocal message_completed
                message_completed = _is_successful_assistant_terminal(message)
                category = _tool_activity_category(message)
                if category is not None:
                    tool_activity.add(category)

            def _accept_message_snapshot(message: dict) -> None:
                nonlocal current_message_visible, current_message_role
                nonlocal current_metadata, current_visibility_revoked
                nonlocal last_path
                author = message.get("author")
                if not isinstance(author, dict):
                    raise BackendContractError(
                        "conversation_stream", "message author must be an object"
                    )
                role = author.get("role")
                if not isinstance(role, str) or role not in _KNOWN_MESSAGE_ROLES:
                    raise BackendContractError(
                        "conversation_stream", "message author role is invalid"
                    )
                _track_message_lifecycle(message)
                msg_id = message.get("id")
                candidate_key = (
                    f"message:{msg_id}" if isinstance(msg_id, str) else "message:unknown"
                )
                candidate_was_known = candidate_key in candidate_order
                started_new = _reset_if_new_msg(msg_id if isinstance(msg_id, str) else None)
                current_message_role = role
                metadata = message.get("metadata", {})
                if metadata is None:
                    metadata = {}
                elif not isinstance(metadata, dict):
                    raise BackendContractError(
                        "conversation_stream", "message metadata must be an object"
                    )
                _validate_metadata_bounds(metadata, adapter="conversation_stream")
                current_metadata = metadata
                if current_visibility_revoked or not _is_visible_assistant_message(message):
                    if (
                        current_visibility_revoked
                        or role != "assistant"
                        or not started_new
                        or not candidate_was_known
                    ):
                        candidate_text.clear()
                    _revoke_current_visibility()
                    return
                current_message_visible = True
                content = message.get("content")
                if not isinstance(content, dict):
                    current_message_visible = False
                    _set_current_text("")
                    last_path = None
                    return
                parts = content.get("parts")
                if not isinstance(parts, list) or len(parts) > _MAX_TOOL_PARTS:
                    current_message_visible = False
                    _set_current_text("")
                    last_path = None
                    if isinstance(parts, list):
                        raise BackendContractError(
                            "conversation_stream",
                            "message parts must be an array of at most 100 items",
                        )
                    return
                _set_current_text(parts[0] if parts and isinstance(parts[0], str) else "")
                last_path = "/message/content/parts/0"

            def _revoke_current_visibility() -> None:
                nonlocal current_message_visible, current_visibility_revoked
                nonlocal last_path, message_completed
                current_message_visible = False
                current_visibility_revoked = True
                message_completed = False
                if current_msg_id is not None:
                    revoked_message_ids.add(current_msg_id)
                if current_candidate_key is not None:
                    candidate_text.pop(current_candidate_key, None)
                _set_current_text("")
                last_path = None

            def _apply_path_patch(path: str, op: str, value: object) -> None:
                nonlocal current_metadata, last_path, last_text_length
                nonlocal message_completed
                last_path = path
                if not have_current_message:
                    return
                if path == "/message/metadata" or path.startswith("/message/metadata/"):
                    if op not in {"add", "append", "patch", "replace"}:
                        _revoke_current_visibility()
                        return
                    if path == "/message/metadata" and not isinstance(value, dict):
                        _revoke_current_visibility()
                        return
                    current_metadata = _merge_metadata_path(
                        current_metadata,
                        path,
                        op,
                        value,
                        adapter="conversation_stream",
                    )
                    if not is_user_visible_message({"metadata": current_metadata}):
                        _revoke_current_visibility()
                    return
                if path == "/message/status":
                    if op not in {"add", "replace"} or not isinstance(value, str):
                        _revoke_current_visibility()
                        return
                    if current_message_role in ("assistant", "tool"):
                        message_completed = bool(
                            current_message_visible
                            and current_message_role == "assistant"
                            and value == "finished_successfully"
                        )
                    return
                if path != "/message/content/parts/0":
                    if path == "/message" or path.startswith("/message/"):
                        # Exposure-defining fields such as recipient, author,
                        # and content type can change after text was buffered.
                        # Until every mutation is revalidated as a full message,
                        # discard the candidate rather than projecting stale text.
                        _revoke_current_visibility()
                    return
                if (
                    not current_message_visible
                    or op not in {"add", "append", "replace"}
                    or not isinstance(value, str)
                ):
                    _revoke_current_visibility()
                    return
                message_completed = False
                if op == "append":
                    if last_text_length + len(value) > _MAX_TOOL_TEXT_CHARS:
                        raise BackendContractError(
                            "conversation_stream",
                            "assistant text exceeds the size limit",
                        )
                    if value:
                        last_text_parts.append(value)
                        last_text_length += len(value)
                elif op in {"add", "replace"}:
                    _set_current_text(value)

            def _apply_patch(obj: dict) -> None:
                nonlocal last_path
                p, o = _validated_patch_tokens(
                    obj.get("p"),
                    obj.get("o"),
                    adapter="conversation_stream",
                )
                has_v = "v" in obj
                value = obj.get("v")

                if (
                    isinstance(value, dict)
                    and "message" in value
                    and ((p == "" and o in {"add", "replace"}) or (p is None and o is None))
                ):
                    message = value.get("message")
                    if isinstance(message, dict):
                        _accept_message_snapshot(message)
                    else:
                        _reset_if_new_msg(None)
                        _revoke_current_visibility()
                    return

                if p == "" and o == "patch" and isinstance(value, list):
                    for subpatch in value:
                        if isinstance(subpatch, dict):
                            _apply_patch(subpatch)
                        else:
                            _revoke_current_visibility()
                    return

                if p == "":
                    _revoke_current_visibility()
                    last_path = None
                    return

                if isinstance(p, str) and p:
                    _apply_path_patch(p, o if isinstance(o, str) else "replace", value)
                    return

                if p is None and o is None and has_v and last_path:
                    _apply_path_patch(last_path, "append", value)

            async for raw_line in _bounded_sse_lines(resp, route="/backend-api/conversation"):
                line = raw_line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done_received = True
                    break
                obj = _decode_sse_object(data, adapter="conversation_stream")
                if obj is None:
                    continue
                _raise_for_sse_error(obj)

                # Capture conversation_id from any event that carries it
                cid = obj.get("conversation_id")
                if cid and not _conversation_id:
                    _conversation_id = _backend_path_id(
                        cid, adapter="conversation_stream", field="conversation_id"
                    )

                # Format A: v1 JSON-patch stream. Output remains buffered until
                # this message can no longer receive a late visibility patch.
                if "v" in obj or "p" in obj or "o" in obj:
                    _apply_patch(obj)
                    continue

                # Format B: full message replacement (history_disabled mode)
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                _accept_message_snapshot(msg)

            if not (done_received or message_completed):
                raise _IncompleteStreamError(_conversation_id, tool_activity)

            if have_current_message:
                _save_current()
            for candidate_key in candidate_order:
                chunk = candidate_text.get(candidate_key)
                if chunk:
                    yield chunk

            terminal: dict[str, object] = {}
            if _conversation_id:
                terminal["_conversation_id"] = _conversation_id
            if tool_activity:
                terminal["_tool_activity"] = sorted(tool_activity)
            if terminal:
                yield terminal

    async def complete(
        self,
        model: str,
        messages: list[dict],
        *,
        gizmo_id: str | None = None,
        temporary: bool = True,
        poll_async: bool = False,
        thinking_effort: str | None = None,
        auth_headers: Mapping[str, str] | None = None,
    ) -> str:
        operation_headers = (
            dict(auth_headers) if auth_headers is not None else self._backend.request_headers()
        )
        chunks: list[str] = []
        conv_id: str | None = None
        tool_activity: set[str] = set()
        try:
            async for event in self.stream(
                model,
                messages,
                gizmo_id=gizmo_id,
                temporary=temporary,
                thinking_effort=thinking_effort,
                auth_headers=operation_headers,
            ):
                if isinstance(event, dict):
                    if event.get("_conversation_id"):
                        conv_id = event["_conversation_id"]
                    activity = event.get("_tool_activity")
                    if isinstance(activity, list):
                        tool_activity.update(
                            category
                            for category in activity
                            if category in _TOOL_ACTIVITY_CATEGORIES
                        )
                    continue  # never let a non-str sentinel reach "".join(chunks)
                chunks.append(event)
        except _IncompleteStreamError as exc:
            tool_activity.update(exc.tool_activity)
            if poll_async and exc.conversation_id:
                recovered = await self._poll_async_response(
                    exc.conversation_id,
                    auth_headers=operation_headers,
                    initial_tool_activity=tool_activity,
                )
                if recovered:
                    return recovered
            raise
        text = "".join(chunks)

        # Agent mode: the stream ends immediately with async_status and the real
        # response arrives later, so we poll the conversation for up to 5 min.
        # This MUST be opt-in (poll_async): conv_id is captured on nearly every
        # stream, so an unconditional "not text and conv_id" poll would make an
        # ordinary chat that returns empty text hang for the full poll window.
        if poll_async and not text and conv_id:
            text = await self._poll_async_response(
                conv_id,
                auth_headers=operation_headers,
                initial_tool_activity=tool_activity,
            )
            return text

        return _append_tool_activity_receipt(text, tool_activity)

    async def _poll_async_response(
        self,
        conversation_id: str,
        poll_interval: float = 3.0,
        max_wait: float = 300.0,
        *,
        auth_headers: Mapping[str, str] | None = None,
        initial_tool_activity: set[str] | None = None,
    ) -> str:
        """Poll for async agent-mode response after SSE stream ends with async_status."""
        conversation_id = _backend_path_id(
            conversation_id, adapter="agent_poll", field="conversation_id"
        )
        detail_path = f"/backend-api/conversation/{conversation_id}"
        deadline = time.monotonic() + max_wait
        poll_errors = 0
        tool_activity = set(initial_tool_activity or ())

        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            fatal_unknown = False
            try:
                det = await asyncio.to_thread(
                    self._backend.get, detail_path, auth_headers=auth_headers
                )
            except BackendHTTPError as exc:
                if not exc.retryable:
                    raise
                poll_errors += 1
                if poll_errors >= 5:
                    raise
                _log.warning("Agent poll request failed — continuing")
                continue
            except BackendContractError:
                raise
            except Exception:
                poll_errors += 1
                fatal_unknown = poll_errors >= 5
                if not fatal_unknown:
                    _log.warning("Agent poll request failed — continuing")
                    continue

            if fatal_unknown:
                raise BackendHTTPError(
                    "GET",
                    "/backend-api/conversation/{id}",
                    None,
                    code="temporarily_failed",
                    retryable=True,
                ) from None

            poll_errors = 0

            if not isinstance(det, dict):
                raise BackendContractError("agent_poll", "conversation object response required")
            mapping = det.get("mapping")
            if mapping is None:
                mapping = {}
            if not isinstance(mapping, dict):
                raise BackendContractError("agent_poll", "mapping object required")
            lifecycles = _ordered_poll_lifecycles(mapping, adapter="agent_poll")
            for _, message in lifecycles:
                category = _tool_activity_category(message)
                if category is not None:
                    tool_activity.add(category)

            # Only the latest assistant/tool lifecycle can complete the poll.
            # A newer hidden dispatch, tool node, or in-progress assistant
            # invalidates every earlier visible terminal snapshot.
            if lifecycles:
                terminal_text = _poll_terminal_text(lifecycles[-1][1], adapter="agent_poll")
                if terminal_text is not None:
                    return _append_tool_activity_receipt(terminal_text, tool_activity)

        _log.warning("Agent poll timed out after %ss", max_wait)
        return _append_tool_activity_receipt("(no final assistant response)", tool_activity)

    async def image_gen(
        self,
        prompt: str,
        *,
        model: str = "gpt-5-3",
        poll_interval: float = 5.0,
        max_wait: float = 300.0,
        auth_headers: Mapping[str, str] | None = None,
    ) -> dict:
        """Generate an image via ChatGPT's built-in image generation tool.

        Uses ChatGPT's current prepare + conduit + frontend SSE flow. A returned
        asset is accepted only when it is bound to this stream by the observed
        assistant dispatch or a same-message marker and carries exact
        image-generation provenance.

        Returns dict with keys:
          conversation_id, assets (list of {asset_pointer, width, height,
          size_bytes, download_url, file_name, file_id})

        Raises RuntimeError if image gen fails or times out.
        """
        operation_headers = (
            dict(auth_headers) if auth_headers is not None else self._backend.request_headers()
        )
        headers = dict(operation_headers)
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"

        sentinel = await SentinelGate(self._backend).get_tokens(headers)
        headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel["chat-requirements"]
        if sentinel.get("proof"):
            headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
        if sentinel.get("turnstile"):
            headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]

        prepare_payload, payload = _image_payloads(prompt, model)
        prepare_path = "/backend-api/f/conversation/prepare"
        prepare = await asyncio.to_thread(
            self._backend.post,
            prepare_path,
            json=prepare_payload,
            target_path=prepare_path,
            target_route=prepare_path,
            auth_headers=headers,
        )
        if not isinstance(prepare, dict) or prepare.get("status") != "ok":
            raise BackendContractError("image_generation", "prepare response must have status ok")
        conduit_token = _private_protocol_string(prepare.get("conduit_token"), maximum=16_384)
        if conduit_token is None:
            raise BackendContractError(
                "image_generation", "prepare response requires a bounded conduit token"
            )
        route = "/backend-api/f/conversation"
        headers["X-Conduit-Token"] = conduit_token
        headers["X-Oai-Turn-Trace-Id"] = str(uuid4())
        headers["X-OpenAI-Target-Path"] = route
        headers["X-OpenAI-Target-Route"] = route

        conversation_id: str | None = None
        dispatch: dict[str, str | None] | None = None
        candidate: tuple[dict, dict[str, str | None] | None] | None = None
        marked_message_ids: set[str] = set()
        marker_protocol_seen = False
        done_received = False
        current_message_id: str | None = None

        async with _stream_session() as s:
            resp = await _post_account_stream(
                s,
                _F_CONV_URL,
                route,
                headers=headers,
                json=payload,
                timeout=300,
                stream=True,
            )
            if resp.status_code not in (200, 201):
                _raise_http_status(route, resp.status_code)

            async for raw_line in _bounded_sse_lines(resp, route=route):
                line = raw_line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done_received = True
                    break
                obj = _decode_sse_object(data, adapter="image_generation")
                if obj is None:
                    continue
                _raise_for_sse_error(obj, route=route)

                cid = obj.get("conversation_id")
                value = obj.get("v")
                if isinstance(value, dict):
                    _raise_for_sse_error(value, route=route)
                    wrapped_cid = value.get("conversation_id")
                    if cid is None and wrapped_cid is not None:
                        cid = wrapped_cid
                if cid and not conversation_id:
                    conversation_id = _backend_path_id(
                        cid,
                        adapter="image_generation",
                        field="conversation_id",
                    )

                if (
                    obj.get("type") == "message_marker"
                    and obj.get("marker") == "user_visible_token"
                ):
                    marker_protocol_seen = True
                    marker_message_id = _private_protocol_string(obj.get("message_id"))
                    if marker_message_id is not None:
                        marked_message_ids.add(marker_message_id)

                if (
                    candidate is not None
                    and current_message_id == _private_protocol_string(candidate[0].get("id"))
                    and _patch_mutates_current_message(obj)
                ):
                    # Do not reconstruct unbounded path patches in this closed
                    # projection. Any later mutation of the current message
                    # revokes the buffered candidate; polling may return a later,
                    # independently validated full snapshot.
                    candidate = None

                msg = obj.get("message")
                if not isinstance(msg, dict) and isinstance(value, dict):
                    msg = value.get("message")
                if not isinstance(msg, dict):
                    continue
                current_message_id = _private_protocol_string(msg.get("id"))
                if candidate is not None and current_message_id == _private_protocol_string(
                    candidate[0].get("id")
                ):
                    candidate = None
                observed_dispatch = _image_dispatch_binding(msg)
                if observed_dispatch is not None:
                    dispatch = observed_dispatch
                    continue
                if _is_image_result_candidate(
                    msg,
                    dispatch=dispatch,
                    marked_message_ids=marked_message_ids,
                    marker_protocol_seen=marker_protocol_seen,
                ):
                    candidate = (msg, dispatch)

        if not done_received:
            raise RuntimeError(_INCOMPLETE_RESPONSE_MESSAGE)
        if candidate is not None and _is_image_result_candidate(
            candidate[0],
            dispatch=candidate[1],
            marked_message_ids=marked_message_ids,
            marker_protocol_seen=marker_protocol_seen,
        ):
            return self._extract_image_result(conversation_id, candidate[0])

        # Image is async — poll the conversation until multimodal_text arrives
        if not conversation_id:
            raise RuntimeError("Image gen: no conversation_id returned")

        return await self._poll_image_result(
            conversation_id,
            poll_interval=poll_interval,
            max_wait=max_wait,
            auth_headers=operation_headers,
            dispatch=dispatch,
            marked_message_ids=marked_message_ids,
            marker_protocol_seen=marker_protocol_seen,
        )

    def _extract_image_result(self, conversation_id: str | None, msg: dict) -> dict:
        """Project an image response onto the documented, scalar-only contract."""
        content = msg.get("content")
        if content is None:
            content = {}
        if not isinstance(content, dict):
            raise BackendContractError("image_generation", "message content must be an object")
        parts = content.get("parts")
        if parts is None:
            parts = []
        if not isinstance(parts, list):
            raise BackendContractError("image_generation", "message parts must be an array")
        assets = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if not _is_generated_image_part(part):
                continue
            if len(assets) >= _MAX_TOOL_RECORDS:
                raise BackendContractError(
                    "image_generation", "image asset list exceeds 100 records"
                )
            assets.append(_project_tool_image_asset(part, adapter="image_generation"))

        projected_conversation_id = None
        if conversation_id is not None:
            projected_conversation_id = _bounded_tool_string(
                conversation_id,
                field="conversation_id",
                maximum=2_048,
                adapter="image_generation",
            )

        return {
            "conversation_id": projected_conversation_id,
            "assets": assets,
        }

    async def _poll_image_result(
        self,
        conversation_id: str,
        *,
        poll_interval: float = 5.0,
        max_wait: float = 300.0,
        processing_text: str = "",
        auth_headers: Mapping[str, str] | None = None,
        dispatch: dict[str, str | None] | None = None,
        marked_message_ids: set[str] | None = None,
        marker_protocol_seen: bool = False,
    ) -> dict:
        """Poll conversation until multimodal_text with image assets arrives."""
        del processing_text
        conversation_id = _backend_path_id(
            conversation_id,
            adapter="image_generation",
            field="conversation_id",
        )
        detail_path = f"/backend-api/conversation/{conversation_id}"
        deadline = time.monotonic() + max_wait
        poll_errors = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            fatal_unknown = False
            try:
                det = await asyncio.to_thread(
                    self._backend.get, detail_path, auth_headers=auth_headers
                )
                poll_errors = 0  # reset on success
            except BackendHTTPError as exc:
                if not exc.retryable:
                    raise
                poll_errors += 1
                if poll_errors >= 5:
                    raise
                backoff = min(poll_interval * (2**poll_errors), 30)
                _log.warning("Image poll request failed — retrying in %.0fs", backoff)
                await asyncio.sleep(backoff)
                continue
            except BackendContractError:
                raise
            except Exception:
                poll_errors += 1
                fatal_unknown = poll_errors >= 5
                if not fatal_unknown:
                    backoff = min(poll_interval * (2**poll_errors), 30)
                    _log.warning("Image poll request failed — retrying in %.0fs", backoff)
                    await asyncio.sleep(backoff)
                    continue

            if fatal_unknown:
                raise BackendHTTPError(
                    "GET",
                    "/backend-api/conversation/{id}",
                    None,
                    code="temporarily_failed",
                    retryable=True,
                ) from None

            if not isinstance(det, dict):
                raise BackendContractError(
                    "image_generation", "conversation object response required"
                )
            mapping = det.get("mapping")
            if mapping is None:
                mapping = {}
            if not isinstance(mapping, dict):
                raise BackendContractError("image_generation", "mapping object required")
            candidates: list[tuple[int, int | float | None, dict]] = []
            for index, node in enumerate(mapping.values()):
                if not isinstance(node, dict):
                    continue
                msg = node.get("message")
                if not isinstance(msg, dict):
                    continue
                if _is_image_result_candidate(
                    msg,
                    dispatch=dispatch,
                    marked_message_ids=marked_message_ids,
                    marker_protocol_seen=marker_protocol_seen,
                ):
                    result = self._extract_image_result(conversation_id, msg)
                    if result.get("assets"):
                        timestamp = _bounded_lifecycle_timestamp(
                            msg.get("create_time"),
                            adapter="image_generation",
                        )
                        candidates.append((index, timestamp, result))
            if candidates:
                any_missing_time = any(timestamp is None for _, timestamp, _ in candidates)
                any_present_time = any(timestamp is not None for _, timestamp, _ in candidates)
                if any_missing_time and any_present_time:
                    raise BackendContractError(
                        "image_generation",
                        "message create_time values must use one ordering mode",
                    )
                if any_missing_time:
                    candidates.sort(key=lambda candidate: candidate[0])
                else:
                    candidates.sort(key=lambda candidate: (candidate[1], candidate[0]))
                return candidates[-1][2]

        raise BackendHTTPError(
            "GET",
            "/backend-api/conversation/{id}",
            None,
            code="temporarily_failed",
            retryable=True,
        )

    async def tool_call(
        self,
        prompt: str,
        *,
        model: str = "gpt-5-3",
        temporary: bool = False,
        poll_interval: float = 5.0,
        max_wait: float = 300.0,
    ) -> dict:
        """Send a prompt that triggers a tool-based feature (code interpreter,
        canvas, image gen) and return the structured result.

        Returns dict with keys:
          conversation_id, text (assistant text response),
          tool_calls (list of {recipient, content_type, parts}),
          tool_responses (list of {content_type, parts}),
          multimodal_assets (list of image asset dicts if any)
        """
        headers = self._backend.request_headers()
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"

        sentinel = await SentinelGate(self._backend).get_tokens(headers)
        headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel["chat-requirements"]
        if sentinel.get("proof"):
            headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
        if sentinel.get("turnstile"):
            headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]

        payload = _build_payload(model, [{"role": "user", "content": prompt}], temporary=temporary)

        conversation_id: str | None = None
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_responses: list[dict] = []
        multimodal_assets: list[dict] = []
        done_received = False
        message_completed = False
        retained_projected_chars = 0
        retained_text_chars = 0

        def project_part(value: str) -> str:
            nonlocal retained_projected_chars
            projected = _redacted_tool_text(value, maximum=_MAX_TOOL_PART_CHARS)
            if retained_projected_chars + len(projected) > _MAX_TOOL_PROJECTED_CHARS:
                raise BackendContractError("tool_call", "projected string parts exceed 4 MiB")
            retained_projected_chars += len(projected)
            return projected

        def retain_text_fragment(value: str) -> str:
            nonlocal retained_text_chars
            projected = _redacted_tool_text(value, maximum=_MAX_TOOL_TEXT_CHARS)
            remaining = _MAX_TOOL_TEXT_CHARS - retained_text_chars
            fragment = projected[: max(remaining, 0)]
            retained_text_chars += len(fragment)
            return fragment

        async with _stream_session() as s:
            resp = await _post_account_stream(
                s,
                _CONV_URL,
                "/backend-api/conversation",
                headers=headers,
                json=payload,
                timeout=300,
                stream=True,
            )
            if resp.status_code not in (200, 201):
                _raise_http_status("/backend-api/conversation", resp.status_code)

            last_text = ""
            async for raw_line in _bounded_sse_lines(resp, route="/backend-api/conversation"):
                line = raw_line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done_received = True
                    break
                obj = _decode_sse_object(data, adapter="tool_call")
                if obj is None:
                    continue
                _raise_for_sse_error(obj)

                cid = obj.get("conversation_id")
                if cid and not conversation_id:
                    conversation_id = _bounded_tool_string(
                        cid, field="conversation_id", maximum=2_048
                    )

                msg = obj.get("message")
                if msg is None:
                    continue
                if not isinstance(msg, dict):
                    raise BackendContractError("tool_call", "message must be an object or null")
                # Every newer lifecycle supersedes completion from the prior
                # snapshot. A validated terminal below can establish it again.
                message_completed = False
                author = msg.get("author")
                if not isinstance(author, dict):
                    raise BackendContractError("tool_call", "message author must be an object")
                role = author.get("role")
                if not isinstance(role, str) or role not in _KNOWN_MESSAGE_ROLES:
                    raise BackendContractError("tool_call", "message author role is invalid")
                if role in ("system", "user"):
                    last_text = ""
                    text_parts.clear()
                    continue
                if not is_user_visible_message(msg):
                    last_text = ""
                    text_parts.clear()
                    continue
                raw_recipient = msg.get("recipient")
                recipient = (
                    "all"
                    if raw_recipient is None
                    else _bounded_tool_string(raw_recipient, field="recipient", maximum=2_048)
                )
                content = msg.get("content")
                if content is None:
                    content = {}
                if not isinstance(content, dict):
                    raise BackendContractError("tool_call", "message content must be an object")
                raw_content_type = content.get("content_type")
                ct = (
                    ""
                    if raw_content_type is None
                    else _bounded_tool_string(raw_content_type, field="content_type", maximum=128)
                )
                parts = content.get("parts")
                if parts is None:
                    parts = []
                if not isinstance(parts, list) or len(parts) > _MAX_TOOL_PARTS:
                    raise BackendContractError(
                        "tool_call", "message parts must be an array of at most 100 items"
                    )
                raw_status = msg.get("status")
                status = (
                    ""
                    if raw_status is None
                    else _bounded_tool_string(raw_status, field="status", maximum=128)
                )
                is_assistant_output = (
                    role == "assistant"
                    and recipient == "all"
                    and ct in ("text", "multimodal_text")
                )
                if not is_assistant_output:
                    last_text = ""
                    text_parts.clear()
                # Assistant text (to "all")
                if is_assistant_output:
                    str_parts = [p for p in parts if isinstance(p, str)]
                    message_completed = status == "finished_successfully"
                    if str_parts and status == "finished_successfully":
                        last_text = _redacted_tool_text(str_parts[0], maximum=_MAX_TOOL_TEXT_CHARS)
                    elif str_parts:
                        new = _redacted_tool_text(str_parts[0], maximum=_MAX_TOOL_TEXT_CHARS)
                        if new.startswith(last_text):
                            delta = new[len(last_text) :]
                            if delta:
                                retained = retain_text_fragment(delta)
                                if retained:
                                    text_parts.append(retained)
                        else:
                            retained = retain_text_fragment(new)
                            if retained:
                                text_parts.append(retained)
                        last_text = new

                # Tool call (assistant to non-all recipient)
                elif role == "assistant" and recipient != "all":
                    if len(tool_calls) >= _MAX_TOOL_RECORDS:
                        raise BackendContractError(
                            "tool_call", "tool call list exceeds 100 records"
                        )
                    call_parts = [
                        project_part(part) for part in parts if isinstance(part, str) and part
                    ]
                    tool_calls.append(
                        {
                            "recipient": recipient,
                            "content_type": ct,
                            "parts": call_parts,
                        }
                    )

                # Tool response
                elif role == "tool" and recipient == "all":
                    message_completed = status == "finished_successfully"
                    if len(tool_responses) >= _MAX_TOOL_RECORDS:
                        raise BackendContractError(
                            "tool_call", "tool response list exceeds 100 records"
                        )
                    resp_parts = [project_part(part) for part in parts if isinstance(part, str)]
                    img_assets = [
                        _project_tool_image_asset(part)
                        for part in parts
                        if isinstance(part, dict)
                        and part.get("content_type") == "image_asset_pointer"
                    ]
                    if len(multimodal_assets) + len(img_assets) > _MAX_TOOL_RECORDS:
                        raise BackendContractError(
                            "tool_call", "multimodal asset list exceeds 100 records"
                        )
                    tool_responses.append(
                        {
                            "content_type": ct,
                            "parts": resp_parts,
                        }
                    )
                    multimodal_assets.extend(img_assets)

        if not (done_received or message_completed):
            raise RuntimeError(_INCOMPLETE_RESPONSE_MESSAGE)
        final_text = (last_text or "".join(text_parts))[:_MAX_TOOL_TEXT_CHARS]

        return {
            "conversation_id": conversation_id,
            "text": final_text,
            "tool_calls": tool_calls,
            "tool_responses": tool_responses,
            "multimodal_assets": multimodal_assets,
        }

    async def deep_research(
        self,
        query: str,
        *,
        max_clarification_rounds: int = 2,
    ) -> AsyncIterator[dict]:
        """Stream Deep Research events for *query*.

        Yields dicts of shape:
          {"type": "progress", "text": <partial_text>}   — intermediate text deltas
          {"type": "tool", "call": "web_search", "category": "web"}
              — static receipt for search/browse dispatch
          {"type": "done",     "text": <full_text>,
           "content_references": [...], "search_result_groups": [...]}
          {"type": "clarification_auto_reply", "round": N, "question": <text>}
              — emitted when the first turn was a clarification question and the
              wrapper auto-replied with a "proceed with best interpretation"
              follow-up. Real DR continues on the next round.

        Uses model='research' + system_hints=['research'] which triggers the
        ChatGPT web-search deep-research backend (confirmed working 2026-04-24).
        Timeout is 1800 s per round to accommodate multi-minute research runs.

        ChatGPT's research mode often opens with a clarifying question instead
        of starting research immediately. ``max_clarification_rounds`` caps how
        many auto-replies the wrapper sends before giving up (default 2).
        """
        _raw_dump({}, phase="startup")
        conversation_id: str | None = None
        last_assistant_msg_id: str | None = None
        current_query = query
        operation_headers = self._backend.request_headers()

        for round_num in range(max_clarification_rounds + 1):
            # Sentinel tokens are single-use, but every round deliberately keeps
            # the same immutable bearer snapshot for this logical operation.
            headers = dict(operation_headers)
            headers["Accept"] = "text/event-stream"
            headers["Content-Type"] = "application/json"
            sentinel = await SentinelGate(self._backend).get_tokens(operation_headers)
            headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel["chat-requirements"]
            if sentinel.get("proof"):
                headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
            if sentinel.get("turnstile"):
                headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]

            payload = _build_dr_payload(
                current_query,
                conversation_id=conversation_id,
                parent_message_id=last_assistant_msg_id,
            )

            async with _stream_session() as s:
                resp = await _post_account_stream(
                    s,
                    _CONV_URL,
                    "/backend-api/conversation",
                    headers=headers,
                    json=payload,
                    timeout=1800,
                    stream=True,
                )
                if resp.status_code not in (200, 201):
                    _raise_http_status("/backend-api/conversation", resp.status_code)

                last_text = ""
                done_text = ""
                pending_progress: list[str] = []
                done_event: dict | None = None
                current_assistant_msg_id: str | None = None
                round_completed_successfully = False
                stream_succeeded = False
                try:
                    async for raw_line in _bounded_sse_lines(
                        resp, route="/backend-api/conversation"
                    ):
                        line = raw_line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        obj = _decode_sse_object(data, adapter="deep_research")
                        if obj is None:
                            continue
                        _raise_for_sse_error(obj)

                        # Capture conversation_id for multi-turn continuation.
                        cid = obj.get("conversation_id")
                        if cid and not conversation_id:
                            conversation_id = _backend_path_id(
                                cid,
                                adapter="deep_research",
                                field="conversation_id",
                            )

                        msg = obj.get("message", {})
                        if not isinstance(msg, dict):
                            continue
                        author = msg.get("author")
                        if not isinstance(author, dict):
                            raise BackendContractError(
                                "deep_research", "message author must be an object"
                            )
                        role = author.get("role")
                        if not isinstance(role, str) or role not in _KNOWN_MESSAGE_ROLES:
                            raise BackendContractError(
                                "deep_research", "message author role is invalid"
                            )
                        content = msg.get("content")
                        if content is None:
                            content = {}
                        if not isinstance(content, dict):
                            raise BackendContractError(
                                "deep_research", "message content must be an object"
                            )
                        ct = content.get("content_type", "")
                        status = msg.get("status", "")
                        meta = msg.get("metadata")
                        if meta is None:
                            meta = {}
                        if not isinstance(meta, dict):
                            raise BackendContractError(
                                "deep_research", "message metadata must be an object"
                            )
                        _validate_metadata_bounds(meta, adapter="deep_research")
                        recipient = msg.get("recipient")
                        visible = is_user_visible_message(msg)

                        # Any newer non-output lifecycle or hidden assistant proves
                        # that the earlier report candidate is stale, even though
                        # its private body must never be emitted.
                        if role != "assistant" or not visible:
                            round_completed_successfully = False
                            done_text = ""
                            last_text = ""
                            pending_progress = []
                            done_event = None

                        is_dispatch = role == "assistant" and (
                            ct == "code"
                            or recipient not in (None, "all")
                            or _message_connector_dispatch_category(msg) is not None
                        )
                        if is_dispatch:
                            round_completed_successfully = False
                            done_text = ""
                            last_text = ""
                            pending_progress = []
                            done_event = None
                            yield {
                                "type": "tool",
                                "call": "web_search",
                                "category": "web",
                            }
                            continue

                        if not visible:
                            continue

                        # Capture latest assistant message id so the next turn
                        # (auto-proceed reply) can use it as parent_message_id.
                        msg_id = msg.get("id")
                        if msg_id and role == "assistant":
                            if (
                                current_assistant_msg_id is not None
                                and msg_id != current_assistant_msg_id
                            ):
                                # A new assistant lifecycle supersedes buffered
                                # text from the prior message in this round.
                                round_completed_successfully = False
                                done_text = ""
                                last_text = ""
                                pending_progress = []
                                done_event = None
                            current_assistant_msg_id = msg_id
                            last_assistant_msg_id = msg_id

                        # Completion belongs to the latest relevant lifecycle,
                        # not to any earlier clean `done`. A later tool response
                        # proves the round continued and invalidates that candidate.
                        if role == "tool":
                            round_completed_successfully = False
                            done_text = ""
                            last_text = ""
                            pending_progress = []
                            done_event = None

                        # Tool invocation events (search/browse). Assistant
                        # messages addressed to a tool are dispatch envelopes,
                        # even when the backend represents them as plain text.
                        # Text streaming — assistant in-progress or finished
                        if role == "assistant" and ct == "text":
                            parts = content.get("parts")
                            if parts is None:
                                parts = []
                            if not isinstance(parts, list) or len(parts) > _MAX_TOOL_PARTS:
                                raise BackendContractError(
                                    "deep_research",
                                    "message parts must be an array of at most 100 items",
                                )
                            if parts and not isinstance(parts[0], str):
                                raise BackendContractError(
                                    "deep_research", "assistant first part must be a string"
                                )
                            new = parts[0] if parts else ""
                            if len(new) > _MAX_TOOL_TEXT_CHARS:
                                raise BackendContractError(
                                    "deep_research", "assistant text exceeds the size limit"
                                )

                            if status == "finished_successfully":
                                done_event = {
                                    "type": "done",
                                    "text": new,
                                    "content_references": meta.get("content_references", []),
                                    "search_result_groups": meta.get("search_result_groups", []),
                                }
                                round_completed_successfully = True
                                last_text = new
                                done_text = new
                            else:
                                # Even an empty newer in-progress snapshot
                                # supersedes an earlier completed candidate.
                                round_completed_successfully = False
                                done_text = ""
                                done_event = None
                                if status != "in_progress":
                                    last_text = new
                                    continue
                            if status == "in_progress" and new:
                                # Emit incremental text delta
                                if new.startswith(last_text):
                                    delta = new[len(last_text) :]
                                    if delta:
                                        pending_progress.append(delta)
                                else:
                                    pending_progress.append(new)
                                last_text = new
                            elif status == "in_progress":
                                last_text = ""
                    stream_succeeded = True
                finally:
                    # Emit a synthetic abnormal terminal whenever normal EOF
                    # leaves the latest relevant lifecycle incomplete, even if
                    # an older candidate had once reached finished_successfully.
                    # On exception, propagate without faking a done event,
                    # so the caller doesn't mistake partial output for a
                    # complete answer (cf. code-review medium #2).
                    if stream_succeeded:
                        # User text stays buffered until this round's stream is
                        # closed, because a later hidden/tool lifecycle can
                        # revoke every earlier visible snapshot.
                        for progress_text in pending_progress:
                            if progress_text:
                                yield {"type": "progress", "text": progress_text}
                        if round_completed_successfully and done_event is not None:
                            yield done_event
                        else:
                            terminal = {
                                "type": "done",
                                "text": last_text,
                                "content_references": [],
                                "search_result_groups": [],
                                "terminated_abnormally": True,
                            }
                            if round_num > 0:
                                terminal["clarification_followup_incomplete"] = True
                            yield terminal
                            done_text = last_text

            # End of one round — decide whether to continue.
            if not round_completed_successfully:
                return

            # If the model asked for clarification AND we have a captured
            # conversation_id (so a follow-up can land in the same thread),
            # auto-reply and keep going.
            if (
                conversation_id
                and last_assistant_msg_id
                and _looks_like_clarification(done_text)
                and round_num < max_clarification_rounds
            ):
                yield {
                    "type": "clarification_auto_reply",
                    "round": round_num + 1,
                    "question": done_text,
                }
                current_query = _DR_AUTO_PROCEED
                continue

            if _looks_like_clarification(done_text):
                # No continuation can be sent (missing thread identity or the
                # configured round limit was reached). Override the preceding
                # clarification-shaped `done` with an explicit abnormal terminal
                # event rather than reporting the question as successful output.
                yield {
                    "type": "done",
                    "text": done_text,
                    "content_references": [],
                    "search_result_groups": [],
                    "terminated_abnormally": True,
                    "clarification_unresolved": True,
                }

            return

    async def deep_research_heavy(
        self,
        query: str,
        *,
        model: str | None = None,
    ) -> AsyncIterator[dict]:
        """Stream true Pro-tier Deep Research events for *query*.

        Two-phase: (1) SSE kickoff at /backend-api/f/conversation speaking
        "delta_encoding v1" JSON-patches; (2) if the stream closes before the
        assistant message reaches finished_successfully (async DR on complex
        queries), poll /backend-api/conversation/{id} until it does.

        Payload + endpoint ground-truth reverse-engineered from
        chatgpt.com/deep-research browser traffic (2026-04-23):

            model = gpt-5-5-pro
            system_hints = ["connector:connector_openai_deep_research"]
            thinking_effort = "extended"
            message.metadata.deep_research_version = "standard"
            message.metadata.venus_model_variant = "standard"

        Yields dicts of shape:
          {"type": "progress", "text": <partial>}   — streaming text deltas
          {"type": "tool", "call": "web_search", "category": "web"}
              — static tool-activity receipt; private dispatch text is withheld
          {"type": "meta", "tool_invoked": bool,
           "tool_category": "connector"} — bounded server metadata receipt
          {"type": "done",     "text": <full_text>,
           "content_references": [...], "search_result_groups": [...]}

        Rate: consumes from the account-reported "deep_research" quota; limits
        and reset timing can vary.
        Timeout: 1800 s for initial SSE; poll phase adds up to 1800 s more.

        Note: the resolved_model_slug in user-message echo will show "i-mini-m"
        (the orchestration layer). The actual heavy reasoning runs inside the
        connector_openai_deep_research tool call.
        """
        _raw_dump({}, phase="startup")
        operation_headers = self._backend.request_headers()

        # --- Quota guard ---
        # Probe /backend-api/conversation/init (POST) to check deep_research quota.
        # Response shape: limits_progress: [{"feature_name": "deep_research", ...}].
        # Fail-open on probe error; only "remaining <= 0" aborts.
        _INIT_PATH = "/backend-api/conversation/init"
        remaining: int | None = None
        try:
            init_data = await asyncio.to_thread(
                self._backend.post,
                _INIT_PATH,
                json={"conversation_mode_kind": "primary_assistant"},
                auth_headers=operation_headers,
            )
            limits = (init_data or {}).get("limits_progress") or []
            for lim in limits:
                if isinstance(lim, dict) and lim.get("feature_name") == "deep_research":
                    raw = lim.get("remaining")
                    if raw is not None:
                        remaining = int(raw)
                    break
        except Exception:
            _log.warning("DR quota check failed — proceeding anyway")
        if remaining is not None and remaining <= 0:
            raise BackendHTTPError(
                "CHECK",
                _INIT_PATH,
                None,
                code="unavailable",
                retryable=False,
            )

        headers = dict(operation_headers)
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"

        sentinel = await SentinelGate(self._backend).get_tokens(operation_headers)
        headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel["chat-requirements"]
        if sentinel.get("proof"):
            headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
        if sentinel.get("turnstile"):
            headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]

        payload = _build_heavy_dr_payload(query, model=model)

        # --- Phase 1: SSE kickoff with JSON-patch delta parser ---
        # /f/conversation speaks "delta_encoding v1". The first assistant envelope
        # arrives as {"v": {"message": {...}}, "c": N}. Subsequent text chunks
        # arrive as {"p": "/message/content/parts/0", "o": "append", "v": "..."}
        # or the shortcut {"v": "..."} (continuation of last path).
        # Batches: {"p": "", "o": "patch", "v": [<sub_patches>]}.
        state = {
            "conversation_id": None,
            "resume_token": None,
            "current_asst_id": None,
            "asst_text": "",
            "asst_status": "",
            "asst_metadata": {},
            "asst_visible": False,
            "patch_target_visible": False,
            "pending_progress": [],
            "last_path": None,
            "tool_invoked": False,
            "web_tool_event_emitted": False,
            "tool_failed": False,
            "done_emitted": False,
            "citation_metadata": {},
            # True while the current assistant envelope is the connector-dispatch
            # JSON ({"path": ".../connector_openai_deep_research/start", ...}).
            # That envelope reaches finished_successfully almost immediately —
            # but its text is the dispatch payload, not the real report. Reset
            # when a fresh assistant envelope (the real report) arrives.
            "is_connector_dispatch": False,
        }

        def _emit_web_tool_once(events: list) -> None:
            state["tool_invoked"] = True
            if state["web_tool_event_emitted"]:
                return
            events.append({"type": "tool", "call": "web_search", "category": "web"})
            state["web_tool_event_emitted"] = True

        def _emit_done(events: list) -> None:
            if state["done_emitted"] or not state["asst_visible"]:
                return
            md = state["asst_metadata"] or {}
            citation_md = state["citation_metadata"] or {}
            refs, groups = _citation_payload(md, citation_md)
            payload: dict = {
                "type": "done",
                "text": state["asst_text"],
                "content_references": refs,
                "search_result_groups": groups,
            }
            if state["tool_failed"]:
                payload["connector_failed"] = True
            events.extend(
                {"type": "progress", "text": text} for text in state["pending_progress"] if text
            )
            state["pending_progress"] = []
            events.append(payload)
            state["done_emitted"] = True

        def _invalidate_report_candidate() -> None:
            state["current_asst_id"] = None
            state["asst_text"] = ""
            state["asst_status"] = ""
            state["asst_metadata"] = {}
            state["citation_metadata"] = {}
            state["asst_visible"] = False
            state["patch_target_visible"] = False
            state["pending_progress"] = []
            state["is_connector_dispatch"] = False

        def _on_envelope(env: dict, events: list) -> None:
            msg = env.get("message")
            if not isinstance(msg, dict):
                raise BackendContractError(
                    "heavy_deep_research", "message envelope must be an object"
                )
            author = msg.get("author")
            if not isinstance(author, dict):
                raise BackendContractError(
                    "heavy_deep_research", "message author must be an object"
                )
            role = author.get("role")
            if not isinstance(role, str) or role not in _KNOWN_MESSAGE_ROLES:
                raise BackendContractError("heavy_deep_research", "message author role is invalid")
            recipient = msg.get("recipient")
            if recipient is not None and not isinstance(recipient, str):
                raise BackendContractError(
                    "heavy_deep_research", "message recipient must be a string or null"
                )
            # A full envelope always changes the subsequent JSON-patch target.
            # Only a validated visible assistant report may accept text/status
            # deltas; tool/widget envelopes must not inherit the prior target.
            state["patch_target_visible"] = False
            content = msg.get("content")
            if content is None:
                content = {}
            if not isinstance(content, dict):
                raise BackendContractError(
                    "heavy_deep_research", "message content must be an object"
                )
            metadata = msg.get("metadata")
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, dict):
                raise BackendContractError(
                    "heavy_deep_research", "message metadata must be an object"
                )
            _validate_metadata_bounds(metadata, adapter="heavy_deep_research")
            parts = content.get("parts")
            if parts is None:
                parts = []
            if not isinstance(parts, list) or len(parts) > _MAX_TOOL_PARTS:
                raise BackendContractError(
                    "heavy_deep_research",
                    "message parts must be an array of at most 100 items",
                )
            status = msg.get("status")
            if status is None:
                status = ""
            if not isinstance(status, str) or len(status) > 128:
                raise BackendContractError(
                    "heavy_deep_research", "message status must be a bounded string"
                )
            ct = content.get("content_type")
            is_report_message = (
                role == "assistant"
                and recipient in (None, "all")
                and ct in ("text", "multimodal_text")
            )
            if role == "assistant":
                # Every new assistant envelope supersedes the previous patch
                # target. Reset even when it is hidden so later path deltas
                # cannot append to an earlier visible report.
                _invalidate_report_candidate()
                state["current_asst_id"] = msg.get("id")
            elif role == "tool":
                # A tool lifecycle proves any prior report snapshot was only
                # provisional. Keep no stale text/citations while the connector
                # is still running, even when this envelope is private.
                _invalidate_report_candidate()
                state["tool_invoked"] = True
            else:
                # User/system lifecycles supersede every earlier report candidate.
                _invalidate_report_candidate()
            if not is_user_visible_message(msg):
                # A hidden connector dispatch may still prove that polling is
                # required, but none of its private payload is public output.
                if (
                    role == "assistant"
                    and isinstance(recipient, str)
                    and recipient.startswith("api_tool")
                ):
                    state["tool_invoked"] = True
                return
            if is_report_message:
                state["asst_visible"] = True
                state["patch_target_visible"] = True
                initial = parts[0] if parts and isinstance(parts[0], str) else ""
                if len(initial) > _MAX_TOOL_TEXT_CHARS:
                    raise BackendContractError(
                        "heavy_deep_research", "assistant text exceeds the size limit"
                    )
                state["asst_text"] = initial
                state["asst_status"] = status
                state["asst_metadata"] = metadata
                if _has_citation_payload(state["asst_metadata"]):
                    state["citation_metadata"] = state["asst_metadata"]
                state["is_connector_dispatch"] = _is_connector_dispatch_text(initial)
                if state["is_connector_dispatch"]:
                    _emit_web_tool_once(events)
                if initial and not state["is_connector_dispatch"]:
                    state["pending_progress"].append(initial)
            elif (
                role == "assistant"
                and isinstance(recipient, str)
                and recipient.startswith("api_tool")
            ):
                _emit_web_tool_once(events)
            elif role == "tool" and recipient == "all":
                # Tool response — detect connector-not-available errors so
                # the caller can distinguish "DR ran" from "DR silently
                # fell through to i-mini-m because the connector isn't
                # provisioned on this account".
                text = parts[0] if parts and isinstance(parts[0], str) else ""
                if text and ("Resource not found" in text or text.startswith("Error")):
                    events.append({"type": "tool_error", "code": "connector_unavailable"})
                    state["tool_failed"] = True

        def _apply_path(path: str, op: str, value, events: list) -> None:
            if path == "/message/content/parts/0":
                if not state["patch_target_visible"]:
                    return
                if op == "append" and isinstance(value, str):
                    next_text = state["asst_text"] + value
                    if len(next_text) > _MAX_TOOL_TEXT_CHARS:
                        raise BackendContractError(
                            "heavy_deep_research",
                            "assistant text exceeds the size limit",
                        )
                    was_dispatch = bool(state["is_connector_dispatch"])
                    next_is_dispatch = was_dispatch or _is_connector_dispatch_text(next_text)
                    state["asst_text"] = next_text
                    state["is_connector_dispatch"] = next_is_dispatch
                    if next_is_dispatch and not was_dispatch:
                        state["pending_progress"] = []
                        _emit_web_tool_once(events)
                    if value and not next_is_dispatch:
                        state["pending_progress"].append(value)
                elif op == "replace" and isinstance(value, str):
                    if len(value) > _MAX_TOOL_TEXT_CHARS:
                        raise BackendContractError(
                            "heavy_deep_research",
                            "assistant text exceeds the size limit",
                        )
                    new_is_dispatch = _is_connector_dispatch_text(value)
                    if not new_is_dispatch and value.startswith(state["asst_text"]):
                        delta = value[len(state["asst_text"]) :]
                        if delta:
                            state["pending_progress"].append(delta)
                    elif value and not new_is_dispatch:
                        state["pending_progress"].append(value)
                    state["asst_text"] = value
                    state["is_connector_dispatch"] = new_is_dispatch
                    if new_is_dispatch:
                        state["pending_progress"] = []
                        _emit_web_tool_once(events)
                else:
                    _invalidate_report_candidate()
            elif path == "/message/status":
                if not state["patch_target_visible"]:
                    return
                if op in {"add", "replace"} and isinstance(value, str):
                    state["asst_status"] = value
                else:
                    _invalidate_report_candidate()
            elif path == "/message/metadata" or path.startswith("/message/metadata/"):
                if not state["patch_target_visible"]:
                    return
                if op not in {"add", "append", "patch", "replace"}:
                    _invalidate_report_candidate()
                    return
                if path == "/message/metadata" and not isinstance(value, dict):
                    _invalidate_report_candidate()
                    return
                state["asst_metadata"] = _merge_metadata_path(
                    state["asst_metadata"],
                    path,
                    op,
                    value,
                    adapter="heavy_deep_research",
                )
                if not is_user_visible_message({"metadata": state["asst_metadata"]}):
                    state["asst_visible"] = False
                    state["patch_target_visible"] = False
                    state["asst_text"] = ""
                    state["asst_status"] = ""
                    state["pending_progress"] = []
                    state["is_connector_dispatch"] = False
                    return
                if _has_citation_payload(state["asst_metadata"]):
                    state["citation_metadata"] = state["asst_metadata"]
            elif path == "/message" or path.startswith("/message/"):
                # Unsupported message mutations may change recipient, author,
                # content type, or another exposure boundary after report text
                # was buffered. They invalidate the candidate fail-closed.
                _invalidate_report_candidate()

        def _apply_patch(obj: dict, events: list) -> None:
            t = obj.get("type")
            if t is not None and any(field in obj for field in ("p", "o", "v")):
                _invalidate_report_candidate()
                raise BackendContractError(
                    "heavy_deep_research", "typed event must not contain message patch fields"
                )
            if t == "resume_conversation_token":
                state["resume_token"] = obj.get("token")
                if obj.get("conversation_id"):
                    state["conversation_id"] = _backend_path_id(
                        obj["conversation_id"],
                        adapter="heavy_deep_research",
                        field="conversation_id",
                    )
                return
            if t in ("message_marker", "message_stream_complete"):
                if obj.get("conversation_id"):
                    state["conversation_id"] = _backend_path_id(
                        obj["conversation_id"],
                        adapter="heavy_deep_research",
                        field="conversation_id",
                    )
                return
            if t == "server_ste_metadata":
                md = obj.get("metadata")
                tool_invoked = isinstance(md, dict) and md.get("tool_invoked") is True
                if tool_invoked:
                    state["tool_invoked"] = True
                event: dict = {"type": "meta", "tool_invoked": tool_invoked}
                if tool_invoked:
                    event["tool_category"] = "connector"
                events.append(event)
                return
            if t == "input_message":
                _invalidate_report_candidate()
                return
            if t is not None:
                _invalidate_report_candidate()
                raise BackendContractError(
                    "heavy_deep_research", "typed event type is not supported"
                )

            p = obj.get("p")
            o = obj.get("o")
            p, o = _validated_patch_tokens(
                p,
                o,
                adapter="heavy_deep_research",
            )
            has_v = "v" in obj
            v = obj.get("v")

            # Full envelope: explicit {"p": "", "o": "add", ...}
            # or implicit {"v": {"message": ...}, "c": N}
            if (
                isinstance(v, dict)
                and "message" in v
                and ((p == "" and o in {"add", "replace"}) or (p is None and o is None))
            ):
                _on_envelope(v, events)
                state["last_path"] = None
                return

            # Batch patch
            if p == "" and o == "patch" and isinstance(v, list):
                for sub in v:
                    if isinstance(sub, dict):
                        _apply_patch(sub, events)
                    else:
                        _invalidate_report_candidate()
                return

            if p == "":
                _invalidate_report_candidate()
                state["last_path"] = None
                return

            # Path-scoped patch
            if isinstance(p, str) and p:
                _apply_path(p, o if o is not None else "replace", v, events)
                state["last_path"] = p
                return

            # Shortcut: bare "v" continues the last path (text-append)
            if p is None and o is None and has_v and state["last_path"]:
                _apply_path(state["last_path"], "append", v, events)
                return

        async with _stream_session() as s:
            resp = await _post_account_stream(
                s,
                _F_CONV_URL,
                "/backend-api/f/conversation",
                headers=headers,
                json=payload,
                timeout=1800,
                stream=True,
            )
            if resp.status_code not in (200, 201):
                _raise_http_status("/backend-api/f/conversation", resp.status_code)

            async for raw_line in _bounded_sse_lines(resp, route="/backend-api/f/conversation"):
                line = raw_line.strip()
                if not line or line.startswith(":") or line.startswith("event:"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = _decode_sse_object(data, adapter="heavy_deep_research")
                if obj is None:
                    continue
                _raise_for_sse_error(obj, route="/backend-api/f/conversation")
                # Capture conversation_id from ANY frame that carries it at top
                # level. _apply_patch only sets it from a few typed events
                # (resume_conversation_token / message_marker /
                # message_stream_complete); the regular stream() captures it
                # generically. Without this, an async heavy-DR stream that closes
                # before one of those markers leaves state["conversation_id"]
                # None, so the Phase-2 poll gate below never fires and the report
                # is silently lost.
                if not state["conversation_id"]:
                    cid = obj.get("conversation_id")
                    if cid:
                        state["conversation_id"] = _backend_path_id(
                            cid,
                            adapter="heavy_deep_research",
                            field="conversation_id",
                        )
                events: list[dict] = []
                _apply_patch(obj, events)
                for e in events:
                    yield e

        # Do not release buffered report text until the stream has ended and
        # its final visibility/status metadata can no longer be revised by a
        # later patch. This trades incremental text timing for a fail-closed
        # hidden-message boundary.
        terminal_events: list[dict] = []
        if (
            state["asst_visible"]
            and state["asst_status"] == "finished_successfully"
            and not state["is_connector_dispatch"]
            and state["asst_text"]
        ):
            _emit_done(terminal_events)
        for event in terminal_events:
            yield event

        # --- Phase 2: Async polling fallback ---
        # If the stream closed without finished_successfully AND the DR
        # connector fired, poll /backend-api/conversation/{id} until the
        # real answer lands.
        if not state["done_emitted"] and state["conversation_id"] and state["tool_invoked"]:
            async for evt in self._poll_dr_completion(
                state["conversation_id"],
                seed_text=state["asst_text"],
                connector_failed=state["tool_failed"],
                auth_headers=operation_headers,
            ):
                yield evt
            return

        if not state["done_emitted"] and (state["asst_text"] or state["tool_invoked"]):
            # Stream ended mid-lifecycle without a pollable conversation or
            # finalized report. Emit one explicit abnormal terminal, never an
            # older report that a newer tool lifecycle invalidated.
            yield {
                "type": "done",
                "text": state["asst_text"],
                "content_references": [],
                "search_result_groups": [],
                "terminated_abnormally": True,
            }

    async def _poll_dr_completion(
        self,
        conv_id: str,
        *,
        seed_text: str = "",
        connector_failed: bool = False,
        interval: float = 120.0,
        max_wait: float = 1800.0,
        auth_headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[dict]:
        """Poll /backend-api/conversation/{id} until the DR answer lands.

        Walks mapping[*].message for the latest assistant text node; yields
        incremental progress until its status reaches finished_successfully
        (or max_wait elapses). The Deep Research App connector never writes its
        report as an assistant text node, so we also fetch the hidden widget
        state and recover the report from ``widget_state.report_message`` (see
        :func:`_dr_report_from_widget_state`).
        """
        conv_id = _backend_path_id(
            conv_id,
            adapter="heavy_deep_research",
            field="conversation_id",
        )
        detail_path = (
            f"/backend-api/conversation/{conv_id}"
            "?include_visually_hidden_messages=true&include_widget_state=true"
        )
        deadline = time.monotonic() + max_wait
        # A seed came from a stream that did not reach an authoritative
        # terminal.  Retain it only for delta comparison at a later safe
        # terminal; never emit it on its own or on timeout.
        seed_candidate = "" if _is_connector_dispatch_text(seed_text) else seed_text

        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                det = await asyncio.to_thread(
                    self._backend.get, detail_path, auth_headers=auth_headers
                )
            except BackendHTTPError as exc:
                if not exc.retryable:
                    raise
                _log.warning("DR poll request failed — continuing")
                if exc.status_code == 429:
                    await asyncio.sleep(max(interval * 2, 300.0))
                continue
            except BackendContractError:
                raise
            except Exception:
                _log.warning("DR poll request failed — continuing")
                continue
            if not isinstance(det, dict):
                raise BackendContractError(
                    "heavy_deep_research", "conversation object response required"
                )
            mapping = det.get("mapping")
            if mapping is None:
                mapping = {}
            if not isinstance(mapping, dict):
                raise BackendContractError("heavy_deep_research", "mapping object required")
            lifecycles = _ordered_poll_lifecycles(mapping, adapter="heavy_deep_research")
            citation_candidates: list[tuple[tuple[int | float, int], dict]] = []
            turn_boundary_order: tuple[int | float, int] | None = None
            for order_key, message in lifecycles:
                metadata = message.get("metadata")
                if metadata is None:
                    metadata = {}
                if not isinstance(metadata, dict):
                    raise BackendContractError(
                        "heavy_deep_research", "message metadata must be an object"
                    )
                author = message["author"]
                if author["role"] in {"system", "user"}:
                    turn_boundary_order = order_key
                if (
                    author["role"] in {"assistant", "tool"}
                    and is_user_visible_message(message)
                    and _has_citation_payload(metadata)
                ):
                    citation_candidates.append((order_key, metadata))

            if not lifecycles:
                continue
            latest_message = lifecycles[-1][1]

            # Deep Research App connector: the report is not an assistant text
            # node — it lives in the hidden widget state. A completed widget is
            # authoritative only when its carrier is also the newest lifecycle;
            # otherwise a newer tool/assistant message has superseded it.
            latest_widget_candidates = [
                (text, refs)
                for carrier, text, refs in _dr_widget_report_candidates(det)
                if carrier is latest_message
            ]
            if latest_widget_candidates:
                widget_text, widget_refs = max(
                    latest_widget_candidates, key=lambda candidate: len(candidate[0])
                )
                if widget_text != seed_candidate:
                    yield {"type": "progress", "text": widget_text}
                yield {
                    "type": "done",
                    "text": widget_text,
                    "content_references": widget_refs,
                    "search_result_groups": [],
                    "connector_failed": connector_failed,
                }
                return

            latest_text = _poll_terminal_text(latest_message, adapter="heavy_deep_research")
            if latest_text is not None:
                latest_meta = latest_message.get("metadata")
                if latest_meta is None:
                    latest_meta = {}
                assert isinstance(latest_meta, dict)
                refs = latest_meta.get("content_references") or []
                groups = latest_meta.get("search_result_groups") or []
                if citation_candidates and (not refs or not groups):
                    current_turn_candidates = [
                        (order_key, meta)
                        for order_key, meta in citation_candidates
                        if turn_boundary_order is None or order_key > turn_boundary_order
                    ]
                    turn_keys = ("working_turn_id", "turn_exchange_id")
                    same_turn = [
                        meta
                        for _, meta in current_turn_candidates
                        if any(
                            latest_meta.get(k) and latest_meta.get(k) == meta.get(k)
                            for k in turn_keys
                        )
                    ]
                    fallback_metas = same_turn or [
                        meta for _, meta in current_turn_candidates
                    ]
                    for meta in reversed(fallback_metas):
                        if not refs:
                            refs = meta.get("content_references") or []
                        if not groups:
                            groups = meta.get("search_result_groups") or []
                        if refs and groups:
                            break
                if latest_text != seed_candidate:
                    if latest_text.startswith(seed_candidate):
                        progress = latest_text[len(seed_candidate) :]
                    else:
                        progress = latest_text
                    if progress:
                        yield {"type": "progress", "text": progress}
                yield {
                    "type": "done",
                    "text": latest_text,
                    "content_references": refs,
                    "search_result_groups": groups,
                    "connector_failed": connector_failed,
                }
                return

        yield {
            "type": "done",
            "text": "",
            "content_references": [],
            "search_result_groups": [],
            "connector_failed": connector_failed,
            "terminated_abnormally": True,
            "timeout": True,
        }
