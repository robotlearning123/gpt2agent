from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest

from gpt2agent import sse as sse_mod
from gpt2agent.errors import BackendContractError, BackendHTTPError
from tests.test_audit_2026_07_09_streaming import (
    _Backend,
    _FrameResponse,
    _SentinelStub,
)


_DISPATCH_ID = "dispatch-safe"
_TOOL_NAME = "opaque.image.tool"
_TURN_ID = "turn-safe"
_RESULT_ID = "result-safe"


def _dispatch() -> dict[str, Any]:
    return {
        "id": _DISPATCH_ID,
        "author": {"role": "assistant"},
        "recipient": _TOOL_NAME,
        "content": {"content_type": "code", "text": "private"},
        "status": "finished_successfully",
        "metadata": {"turn_exchange_id": _TURN_ID},
    }


def _result_message() -> dict[str, Any]:
    return {
        "id": _RESULT_ID,
        "author": {"role": "tool", "name": _TOOL_NAME},
        "recipient": "all",
        "content": {
            "content_type": "multimodal_text",
            "parts": [
                {
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "sediment://file-safe",
                    "size_bytes": 123,
                    "width": 640,
                    "height": 480,
                    "metadata": {
                        "generation": {
                            "serialization_title": "Image Generation metadata"
                        }
                    },
                }
            ],
        },
        "status": "finished_successfully",
        "metadata": {
            "parent_id": _DISPATCH_ID,
            "turn_exchange_id": _TURN_ID,
        },
    }


def _frames(*, terminal: bool = True) -> list[str]:
    frames = [
        "data: "
        + json.dumps(
            {
                "type": "resume_conversation_token",
                "conversation_id": "conversation-safe",
                "token": "private-resume-token",
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "",
                "o": "add",
                "v": {"message": _dispatch(), "conversation_id": "conversation-safe"},
            }
        ),
        "data: "
        + json.dumps(
            {
                "type": "message_marker",
                "marker": "user_visible_token",
                "message_id": _RESULT_ID,
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "",
                "o": "add",
                "v": {
                    "message": _result_message(),
                    "conversation_id": "conversation-safe",
                },
            }
        ),
    ]
    if terminal:
        frames.append("data: [DONE]")
    return frames


def _late_hidden_frames(*, batch: bool = False) -> list[str]:
    frames = _frames()
    patch = {
        "p": "/message/metadata/is_visually_hidden_from_conversation",
        "o": "replace",
        "v": True,
    }
    if batch:
        patch = {"p": "", "o": "patch", "v": [patch]}
    frames.insert(
        -1,
        "data: " + json.dumps(patch),
    )
    return frames


class _ImageBackend(_Backend):
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, **kwargs: Any) -> dict[str, str]:
        self.posts.append((path, kwargs))
        return {"status": "ok", "conduit_token": "conduit-safe"}


def _patch_image_stream(
    monkeypatch: pytest.MonkeyPatch, frames: list[str]
) -> dict[str, Any]:
    observed: dict[str, Any] = {}

    class _Session:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _FrameResponse:
            observed.update({"url": url, "kwargs": kwargs})
            return _FrameResponse(frames)

    monkeypatch.setattr(sse_mod, "AsyncSession", _Session)
    monkeypatch.setattr(sse_mod, "SentinelGate", _SentinelStub)
    return observed


def test_image_gen_accepts_current_v1_patch_carrier_after_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _patch_image_stream(monkeypatch, _frames())
    backend = _ImageBackend()
    client = sse_mod.ConversationClient(backend)  # type: ignore[arg-type]

    result = asyncio.run(client.image_gen("draw a safe image", model="gpt-5-5"))

    assert result["conversation_id"] == "conversation-safe"
    assert result["assets"] == [
        {
            "asset_pointer": "sediment://file-safe",
            "file_id": "file-safe",
            "width": 640,
            "height": 480,
            "size_bytes": 123,
        }
    ]
    assert backend.posts[0][0] == "/backend-api/f/conversation/prepare"
    assert backend.posts[0][1]["json"]["history_and_training_disabled"] is False
    assert observed["url"].endswith("/backend-api/f/conversation")
    assert observed["kwargs"]["headers"]["X-Conduit-Token"] == "conduit-safe"
    assert observed["kwargs"]["json"]["history_and_training_disabled"] is False
    assert observed["kwargs"]["json"]["system_hints"] == ["picture_v2"]


def test_image_gen_discards_candidate_when_stream_ends_without_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_image_stream(monkeypatch, _frames(terminal=False))
    client = sse_mod.ConversationClient(_ImageBackend())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="ended before completion"):
        asyncio.run(client.image_gen("draw a safe image", model="gpt-5-5"))


@pytest.mark.parametrize("batch", [False, True])
def test_image_gen_discards_candidate_revoked_by_late_metadata_patch(
    monkeypatch: pytest.MonkeyPatch,
    batch: bool,
) -> None:
    _patch_image_stream(monkeypatch, _late_hidden_frames(batch=batch))
    client = sse_mod.ConversationClient(_ImageBackend())  # type: ignore[arg-type]

    with pytest.raises(BackendHTTPError):
        asyncio.run(
            client.image_gen(
                "draw a safe image",
                model="gpt-5-5",
                poll_interval=0,
                max_wait=0.001,
            )
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"p": "/message/status", "o": "replace", "v": "in_progress"},
        {
            "p": "/message/content/parts/0",
            "o": "replace",
            "v": "replacement",
        },
        {
            "p": "",
            "o": "patch",
            "v": [
                {"p": "/message/recipient", "o": "replace", "v": "private"}
            ],
        },
        {"v": "bare continuation"},
        {"p": "", "o": "replace", "v": "malformed root"},
        {"v": {"message": None}},
    ],
)
def test_image_gen_discards_candidate_after_any_late_message_mutation(
    monkeypatch: pytest.MonkeyPatch,
    patch: dict[str, Any],
) -> None:
    frames = _frames()
    frames.insert(-1, "data: " + json.dumps(patch))
    _patch_image_stream(monkeypatch, frames)
    client = sse_mod.ConversationClient(_ImageBackend())  # type: ignore[arg-type]

    with pytest.raises(BackendHTTPError):
        asyncio.run(
            client.image_gen(
                "draw a safe image",
                model="gpt-5-5",
                poll_interval=0,
                max_wait=0.001,
            )
        )


@pytest.mark.parametrize(
    ("mutation",),
    [
        (lambda msg: msg.update(metadata={"is_visually_hidden_from_conversation": True}),),
        (lambda msg: msg.update(metadata=None),),
        (
            lambda msg: msg.update(
                metadata={"is_visually_hidden_from_conversation": "false"}
            ),
        ),
        (lambda msg: msg.update(metadata={"is_visually_hidden_from_conversation": 0}),),
        (lambda msg: msg.update(recipient="private"),),
        (lambda msg: msg.update(status="in_progress"),),
        (lambda msg: msg["author"].update(name="wrong-tool"),),
        (lambda msg: msg["metadata"].update(parent_id="wrong-parent"),),
        (lambda msg: msg["metadata"].update(turn_exchange_id="wrong-turn"),),
        (lambda msg: msg["content"]["parts"][0].update(metadata={}),),
    ],
)
def test_image_result_candidate_rejects_unbound_or_hidden_carrier(mutation) -> None:
    msg = _result_message()
    mutation(msg)

    assert not sse_mod._is_image_result_candidate(
        msg,
        dispatch=sse_mod._image_dispatch_binding(_dispatch()),
        marked_message_ids={_RESULT_ID},
        marker_protocol_seen=True,
    )


def test_image_result_candidate_requires_marker_when_protocol_is_present() -> None:
    assert not sse_mod._is_image_result_candidate(
        _result_message(),
        dispatch=sse_mod._image_dispatch_binding(_dispatch()),
        marked_message_ids={"different-result"},
        marker_protocol_seen=True,
    )


def test_image_poll_ignores_newer_spoof_and_selects_bound_result() -> None:
    valid = _result_message()
    valid["create_time"] = 1
    spoof = deepcopy(valid)
    spoof["id"] = "spoof"
    spoof["create_time"] = 2
    spoof["metadata"]["parent_id"] = "other-dispatch"
    spoof["content"]["parts"][0]["asset_pointer"] = "sediment://file-spoof"

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {
                "mapping": {
                    "valid": {"message": valid},
                    "spoof": {"message": spoof},
                }
            }

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]
    result = asyncio.run(
        client._poll_image_result(
            "conversation-safe",
            poll_interval=0,
            max_wait=1,
            dispatch=sse_mod._image_dispatch_binding(_dispatch()),
        )
    )

    assert result["assets"][0]["file_id"] == "file-safe"


def test_image_poll_preserves_adjacent_large_integer_timestamp_order() -> None:
    stale = _result_message()
    stale["id"] = "result-stale"
    stale["create_time"] = 2**53
    stale["content"]["parts"][0]["asset_pointer"] = "sediment://file-stale"
    newer = deepcopy(stale)
    newer["id"] = "result-newer"
    newer["create_time"] = 2**53 + 1
    newer["content"]["parts"][0]["asset_pointer"] = "sediment://file-newer"

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {
                "mapping": {
                    # Reverse chronological insertion order: a float conversion
                    # collapses the adjacent integers and incorrectly selects stale.
                    "newer": {"message": newer},
                    "stale": {"message": stale},
                }
            }

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]
    result = asyncio.run(
        client._poll_image_result(
            "conversation-safe",
            poll_interval=0,
            max_wait=1,
            dispatch=sse_mod._image_dispatch_binding(_dispatch()),
        )
    )

    assert result["assets"][0]["file_id"] == "file-newer"


def test_image_poll_rejects_unbounded_integer_timestamp() -> None:
    result_message = _result_message()
    result_message["create_time"] = 10**400

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {"mapping": {"result": {"message": result_message}}}

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    with pytest.raises(BackendContractError, match="create_time"):
        asyncio.run(
            client._poll_image_result(
                "conversation-safe",
                poll_interval=0,
                max_wait=1,
                dispatch=sse_mod._image_dispatch_binding(_dispatch()),
            )
        )


def test_image_poll_rejects_mixed_timestamp_modes() -> None:
    missing = _result_message()
    missing["id"] = "result-missing-time"
    numeric = deepcopy(missing)
    numeric["id"] = "result-numeric-time"
    numeric["create_time"] = 2

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {
                "mapping": {
                    "missing": {"message": missing},
                    "numeric": {"message": numeric},
                }
            }

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    with pytest.raises(BackendContractError, match="create_time"):
        asyncio.run(
            client._poll_image_result(
                "conversation-safe",
                poll_interval=0,
                max_wait=1,
                dispatch=sse_mod._image_dispatch_binding(_dispatch()),
            )
        )


def test_image_poll_without_dispatch_requires_explicit_image_task_provenance() -> None:
    spoof = _result_message()
    spoof["metadata"].pop("parent_id")

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {"mapping": {"spoof": {"message": spoof}}}

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]

    with pytest.raises(BackendHTTPError):
        asyncio.run(
            client._poll_image_result(
                "conversation-safe", poll_interval=0, max_wait=0.001
            )
        )


def test_image_poll_without_dispatch_rejects_unbound_image_task_provenance() -> None:
    result_message = _result_message()
    result_message["metadata"] = {"async_task_type": "image_gen"}

    class _PollingBackend(_Backend):
        def get(self, *_: Any, **__: Any) -> dict:
            return {"mapping": {"result": {"message": result_message}}}

    client = sse_mod.ConversationClient(_PollingBackend())  # type: ignore[arg-type]
    with pytest.raises(BackendHTTPError):
        asyncio.run(
            client._poll_image_result(
                "conversation-safe", poll_interval=0, max_wait=0.001
            )
        )
