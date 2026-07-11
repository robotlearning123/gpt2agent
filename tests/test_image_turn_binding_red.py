"""Red tests for binding image results to the image request that produced them."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gpt2agent import sse as sse_mod
from gpt2agent.errors import BackendHTTPError


_RESULT_ID = "result-current-turn"


def _generated_image_result(*, async_task_type: str | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if async_task_type is not None:
        metadata["async_task_type"] = async_task_type
    return {
        "id": _RESULT_ID,
        "author": {"role": "tool", "name": "opaque.image.tool"},
        "recipient": "all",
        "content": {
            "content_type": "multimodal_text",
            "parts": [
                {
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "sediment://file-current-turn",
                    "metadata": {
                        "generation": {
                            "serialization_title": "Image Generation metadata"
                        }
                    },
                }
            ],
        },
        "status": "finished_successfully",
        "metadata": metadata,
    }


class _PollingBackend:
    def __init__(self, message: dict[str, Any]) -> None:
        self._message = message

    def get(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"mapping": {self._message["id"]: {"message": self._message}}}


def test_unbound_async_image_task_cannot_satisfy_a_new_image_poll() -> None:
    """An older image task in conversation history is type evidence, not binding."""
    older_result = _generated_image_result(async_task_type="image_gen")
    client = sse_mod.ConversationClient(_PollingBackend(older_result))  # type: ignore[arg-type]

    with pytest.raises(BackendHTTPError):
        asyncio.run(
            client._poll_image_result(
                "conversation-current-turn",
                poll_interval=0,
                max_wait=0.001,
            )
        )


def test_current_stream_marker_can_bind_image_result_without_dispatch() -> None:
    """A same-stream marker/message-ID relation is sufficient turn binding."""
    current_result = _generated_image_result(async_task_type=None)
    client = sse_mod.ConversationClient(_PollingBackend(current_result))  # type: ignore[arg-type]

    result = asyncio.run(
        client._poll_image_result(
            "conversation-current-turn",
            poll_interval=0,
            max_wait=1,
            marked_message_ids={_RESULT_ID},
            marker_protocol_seen=True,
        )
    )

    assert result["assets"][0]["file_id"] == "file-current-turn"
