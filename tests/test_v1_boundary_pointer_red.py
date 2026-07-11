"""Regression tests for fail-closed v1 message and metadata patches."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest

from gpt2agent import sse as sse_mod
from gpt2agent.errors import BackendContractError


_PRIVATE_BODY = "SYNTHETIC_BUFFERED_PRIVATE_BODY"
_MAX_METADATA_POINTER_CHARS = 2_048
_MAX_METADATA_POINTER_DEPTH = 32
_MAX_METADATA_ARRAY_INDEX = 100
_MAX_METADATA_NODES = 4_096

_MALFORMED_PATCH_TOKENS = (
    pytest.param([], id="list"),
    pytest.param({}, id="empty-dict"),
    pytest.param({"malformed": True}, id="dict"),
    pytest.param(7, id="integer"),
    pytest.param(True, id="boolean"),
)


class _Response:
    status_code = 200

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames

    async def aiter_content(self):
        for frame in self._frames:
            yield (frame + "\n").encode()


class _Backend:
    def request_headers(self) -> dict[str, str]:
        return {"User-Agent": "test-agent"}

    def post(self, *_: Any, **__: Any) -> dict[str, list[dict[str, int | str]]]:
        return {"limits_progress": [{"feature_name": "deep_research", "remaining": 100}]}


class _Sentinel:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(self, _operation_headers: dict[str, str] | None = None) -> dict[str, str]:
        return {"chat-requirements": "stub", "proof": "", "turnstile": ""}


def _install_frames(
    monkeypatch: pytest.MonkeyPatch, frames: list[str]
) -> sse_mod.ConversationClient:
    class _Session:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _Response:
            return _Response(frames)

    monkeypatch.setattr(sse_mod, "AsyncSession", _Session)
    monkeypatch.setattr(sse_mod, "SentinelGate", _Sentinel)
    return sse_mod.ConversationClient(_Backend())  # type: ignore[arg-type]


def _visible_envelope(*, status: str = "finished_successfully") -> dict[str, object]:
    return {
        "v": {
            "message": {
                "id": "message-under-test",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": [_PRIVATE_BODY]},
                "status": status,
                "metadata": {},
            }
        }
    }


def _sse(payload: object) -> str:
    return "data: " + json.dumps(payload)


async def _regular_events(client: sse_mod.ConversationClient) -> list[str | dict]:
    return [
        event async for event in client.stream("gpt-5-3", [{"role": "user", "content": "question"}])
    ]


async def _heavy_events(client: sse_mod.ConversationClient) -> list[dict]:
    return [event async for event in client.deep_research_heavy("question")]


async def _light_events(client: sse_mod.ConversationClient) -> list[dict]:
    return [
        event
        async for event in client.deep_research(
            "question",
            max_clarification_rounds=0,
        )
    ]


def _lifecycle_after_visible(role: str) -> dict[str, object]:
    envelope = deepcopy(_visible_envelope())
    message = envelope["v"]["message"]  # type: ignore[index]
    message.update(  # type: ignore[union-attr]
        {
            "id": f"{role}-lifecycle",
            "author": {"role": role},
            "content": {"content_type": "text", "parts": ["new lifecycle"]},
            "status": "in_progress",
        }
    )
    return envelope


def _tool_call_message(
    role: str,
    *,
    message_id: str,
    text: str,
    status: str,
) -> dict[str, object]:
    return {
        "id": message_id,
        "author": {"role": role},
        "recipient": "all",
        "content": {"content_type": "text", "parts": [text]},
        "status": status,
        "metadata": {},
    }


_LATE_PRIVATE_PATCHES = (
    pytest.param("/message/recipient", "api_tool.private", id="recipient"),
    pytest.param("/message/author/role", "tool", id="author-role"),
    pytest.param("/message/content/content_type", "code", id="content-type"),
)

_RAW_EOF_REVOCATION_PATCHES = (
    pytest.param(
        {"p": "/message/recipient", "o": "replace", "v": "api_tool.private"},
        id="recipient",
    ),
    pytest.param(
        {"p": "/message/author/role", "o": "replace", "v": "tool"},
        id="author-role",
    ),
    pytest.param(
        {"p": "/message/content/content_type", "o": "replace", "v": "code"},
        id="content-type",
    ),
)


@pytest.mark.parametrize(("path", "value"), _LATE_PRIVATE_PATCHES)
def test_regular_v1_late_projection_patch_revokes_buffered_body(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    value: str,
) -> None:
    frames = [
        _sse(_visible_envelope()),
        _sse({"p": path, "o": "replace", "v": value}),
        "data: [DONE]",
    ]
    client = _install_frames(monkeypatch, frames)

    events = asyncio.run(_regular_events(client))

    assert _PRIVATE_BODY not in repr(events)


@pytest.mark.parametrize(
    "patch",
    (
        *_RAW_EOF_REVOCATION_PATCHES,
        pytest.param(
            {
                "p": "/message/metadata/is_visually_hidden_from_conversation",
                "o": "replace",
                "v": True,
            },
            id="hidden-metadata",
        ),
        pytest.param(
            {"p": "/message/content/parts/0", "o": "remove"},
            id="text-remove",
        ),
        pytest.param(
            {"p": "/message/status", "o": "remove"},
            id="status-remove",
        ),
        pytest.param({"p": "", "o": "remove"}, id="root-remove"),
        pytest.param(
            {"p": "", "o": "replace", "v": "malformed-root"},
            id="root-malformed-replace",
        ),
        pytest.param(
            {
                "p": "",
                "o": "patch",
                "v": [
                    {
                        "p": "/message/recipient",
                        "o": "replace",
                        "v": "api_tool.private",
                    }
                ],
            },
            id="root-batch-private",
        ),
    ),
)
def test_regular_raw_eof_rejects_terminal_revoked_by_late_mutation(
    monkeypatch: pytest.MonkeyPatch,
    patch: dict[str, object],
) -> None:
    client = _install_frames(
        monkeypatch,
        [_sse(_visible_envelope()), _sse(patch)],
    )

    with pytest.raises(RuntimeError, match="ended before completion"):
        asyncio.run(_regular_events(client))


@pytest.mark.parametrize("role", ("user", "system"))
def test_tool_call_raw_eof_rejects_terminal_superseded_by_non_output_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    terminal = _tool_call_message(
        "assistant",
        message_id="assistant-terminal",
        text=_PRIVATE_BODY,
        status="finished_successfully",
    )
    newer = _tool_call_message(
        role,
        message_id=f"{role}-newer",
        text="new lifecycle",
        status="in_progress",
    )
    client = _install_frames(
        monkeypatch,
        [_sse({"message": terminal}), _sse({"message": newer})],
    )

    with pytest.raises(RuntimeError, match="ended before completion"):
        asyncio.run(client.tool_call("run the tool"))


@pytest.mark.parametrize("role", ("user", "system"))
def test_tool_call_done_does_not_return_text_from_superseded_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    terminal = _tool_call_message(
        "assistant",
        message_id="assistant-terminal",
        text=_PRIVATE_BODY,
        status="finished_successfully",
    )
    newer = _tool_call_message(
        role,
        message_id=f"{role}-newer",
        text="new lifecycle",
        status="in_progress",
    )
    client = _install_frames(
        monkeypatch,
        [
            _sse({"message": terminal}),
            _sse({"message": newer}),
            "data: [DONE]",
        ],
    )

    result = asyncio.run(client.tool_call("run the tool"))

    assert result["text"] == ""
    assert _PRIVATE_BODY not in repr(result)


@pytest.mark.parametrize("parser", ("regular", "heavy"))
@pytest.mark.parametrize("field", ("p", "o"), ids=("path", "operation"))
@pytest.mark.parametrize("malformed", _MALFORMED_PATCH_TOKENS)
def test_stream_patch_parsers_reject_non_string_path_or_operation(
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
    field: str,
    malformed: object,
) -> None:
    patch: dict[str, object] = {
        "p": "/message/status",
        "o": "replace",
        "v": "finished_successfully",
    }
    patch[field] = malformed
    client = _install_frames(
        monkeypatch,
        [_sse(_visible_envelope(status="in_progress")), _sse(patch)],
    )

    with pytest.raises(BackendContractError, match="patch (path|operation)"):
        asyncio.run(
            _regular_events(client) if parser == "regular" else _heavy_events(client)
        )


@pytest.mark.parametrize("field", ("path", "operation"))
@pytest.mark.parametrize("malformed", ([], {}, {"malformed"}, object()))
def test_patch_token_validator_rejects_direct_unhashable_containers(
    field: str,
    malformed: object,
) -> None:
    path: object = "/message/status"
    operation: object = "replace"
    if field == "path":
        path = malformed
    else:
        operation = malformed

    with pytest.raises(BackendContractError, match=f"patch {field}"):
        sse_mod._validated_patch_tokens(
            path,
            operation,
            adapter="test_patch",
        )


@pytest.mark.parametrize(
    "event",
    (
        {"p": [], "o": "replace", "v": "value"},
        {"p": "/not-message", "o": {}, "v": "value"},
        {"p": "/not-message", "o": {"replace"}, "v": "value"},
        {"p": "", "o": "patch", "v": {}},
        {"p": "", "o": "patch", "v": [[]]},
        {"p": "", "o": "replace", "v": "malformed-root"},
        {"v": {"message": None}},
        {"v": {"message": {"malformed"}}},
        {"v": {"message": {"author": {"role": []}}}},
        {"v": {"message": {"author": {"role": {}}}}},
        {"v": {"message": {"author": {"role": {"assistant"}}}}},
    ),
)
def test_image_patch_scanner_revokes_malformed_container_tokens(
    event: dict[str, object],
) -> None:
    assert sse_mod._patch_mutates_current_message(event) is True


@pytest.mark.parametrize("malformed", ([], {}, {"malformed"}, object()))
def test_sse_error_classifier_is_hash_safe_for_container_event_types(
    malformed: object,
) -> None:
    sse_mod._raise_for_sse_error({"type": malformed})


@pytest.mark.parametrize("malformed", ({"malformed"}, object()))
def test_metadata_patch_normalizes_non_json_value_containers(
    malformed: object,
) -> None:
    with pytest.raises(BackendContractError, match="JSON values"):
        sse_mod._merge_metadata_path(
            {},
            "/message/metadata/value",
            "replace",
            malformed,
            adapter="test_patch",
        )


@pytest.mark.parametrize(("path", "value"), _LATE_PRIVATE_PATCHES)
def test_heavy_v1_late_projection_patch_revokes_buffered_report(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    value: str,
) -> None:
    frames = [
        _sse(_visible_envelope()),
        _sse({"p": path, "o": "replace", "v": value}),
        "data: [DONE]",
    ]
    client = _install_frames(monkeypatch, frames)

    events = asyncio.run(_heavy_events(client))

    assert _PRIVATE_BODY not in repr(events)


_METADATA_POINTERS = (
    pytest.param(
        f"/message/metadata/content_references/{_MAX_METADATA_ARRAY_INDEX + 1}/title",
        id="numeric-index",
    ),
    pytest.param(
        "/message/metadata/"
        + "/".join(f"level-{index}" for index in range(_MAX_METADATA_POINTER_DEPTH + 1)),
        id="depth",
    ),
    pytest.param(
        "/message/metadata/" + "x" * (_MAX_METADATA_POINTER_CHARS - len("/message/metadata/") + 1),
        id="size",
    ),
    pytest.param(
        "/message/metadata/content_references/²/title",
        id="unicode-superscript-index",
    ),
    pytest.param(
        "/message/metadata/content_references/①/title",
        id="unicode-circled-index",
    ),
)

_METADATA_POINTER_BOUNDARIES = (
    pytest.param(
        f"/message/metadata/content_references/{_MAX_METADATA_ARRAY_INDEX}/title",
        id="numeric-index-at-limit",
    ),
    pytest.param(
        "/message/metadata/"
        + "/".join(f"level-{index}" for index in range(_MAX_METADATA_POINTER_DEPTH)),
        id="depth-at-limit",
    ),
    pytest.param(
        "/message/metadata/" + "x" * (_MAX_METADATA_POINTER_CHARS - len("/message/metadata/")),
        id="size-at-limit",
    ),
)


@pytest.mark.parametrize("path", _METADATA_POINTERS)
def test_metadata_pointer_helper_rejects_bounded_resource_abuse(path: str) -> None:
    with pytest.raises(BackendContractError, match="metadata|pointer"):
        sse_mod._merge_metadata_path({}, path, "replace", {"safe": True})


@pytest.mark.parametrize("path", _METADATA_POINTER_BOUNDARIES)
def test_metadata_pointer_helper_accepts_exact_resource_boundaries(path: str) -> None:
    result = sse_mod._merge_metadata_path({}, path, "replace", {"safe": True})

    assert isinstance(result, dict)


def test_metadata_merge_rejects_cumulative_node_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = {f"field-{index}": index for index in range(_MAX_METADATA_NODES)}

    with pytest.raises(BackendContractError, match="metadata"):
        sse_mod._merge_metadata_path({}, "/message/metadata", "replace", oversized)

    incremental_limit = 16
    monkeypatch.setattr(sse_mod, "_MAX_METADATA_NODES", incremental_limit)
    accumulated: dict[str, object] = {}
    for index in range(incremental_limit - 1):
        accumulated = sse_mod._merge_metadata_path(
            accumulated,
            f"/message/metadata/field-{index}",
            "replace",
            index,
        )
    with pytest.raises(BackendContractError, match="metadata"):
        sse_mod._merge_metadata_path(
            accumulated,
            f"/message/metadata/field-{incremental_limit}",
            "replace",
            True,
        )


def test_metadata_string_budget_accepts_boundary_and_rejects_cumulative_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sse_mod, "_MAX_METADATA_STRING_CHARS", 20)
    at_limit = {"body": "x" * 16}

    sse_mod._validate_metadata_bounds(at_limit, adapter="test_metadata")

    with pytest.raises(BackendContractError, match="metadata"):
        sse_mod._merge_metadata_path(
            at_limit,
            "/message/metadata/body",
            "append",
            "x",
        )


@pytest.mark.parametrize("parser", ("regular", "heavy"))
def test_v1_full_envelope_rejects_oversized_metadata(
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
) -> None:
    monkeypatch.setattr(sse_mod, "_MAX_METADATA_NODES", 3)
    envelope = _visible_envelope()
    message = envelope["v"]["message"]  # type: ignore[index]
    message["metadata"] = {"one": 1, "two": 2, "three": 3}  # type: ignore[index]
    client = _install_frames(monkeypatch, [_sse(envelope), "data: [DONE]"])

    with pytest.raises(BackendContractError, match="metadata"):
        asyncio.run(_regular_events(client) if parser == "regular" else _heavy_events(client))


@pytest.mark.parametrize("parser", ("regular", "heavy"))
def test_v1_full_envelope_rejects_oversized_assistant_text(
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
) -> None:
    monkeypatch.setattr(sse_mod, "_MAX_TOOL_TEXT_CHARS", 16)
    envelope = _visible_envelope()
    message = envelope["v"]["message"]  # type: ignore[index]
    message["content"]["parts"] = ["x" * 17]  # type: ignore[index]
    client = _install_frames(monkeypatch, [_sse(envelope), "data: [DONE]"])

    with pytest.raises(BackendContractError, match="text|size"):
        asyncio.run(_regular_events(client) if parser == "regular" else _heavy_events(client))


@pytest.mark.parametrize("parser", ("regular", "heavy"))
def test_v1_append_rejects_cumulative_assistant_text_growth(
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
) -> None:
    monkeypatch.setattr(sse_mod, "_MAX_TOOL_TEXT_CHARS", 16)
    envelope = _visible_envelope(status="in_progress")
    message = envelope["v"]["message"]  # type: ignore[index]
    message["content"]["parts"] = ["x" * 10]  # type: ignore[index]
    frames = [
        _sse(envelope),
        _sse({"p": "/message/content/parts/0", "o": "append", "v": "y" * 7}),
        "data: [DONE]",
    ]
    client = _install_frames(monkeypatch, frames)

    with pytest.raises(BackendContractError, match="text|size"):
        asyncio.run(_regular_events(client) if parser == "regular" else _heavy_events(client))


@pytest.mark.parametrize("parser", ("regular", "heavy"))
def test_v1_full_envelope_rejects_too_many_content_parts(
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
) -> None:
    envelope = _visible_envelope()
    message = envelope["v"]["message"]  # type: ignore[index]
    message["content"]["parts"] = ["x"] * (sse_mod._MAX_TOOL_PARTS + 1)  # type: ignore[index]
    client = _install_frames(monkeypatch, [_sse(envelope), "data: [DONE]"])

    with pytest.raises(BackendContractError, match="parts|array"):
        asyncio.run(_regular_events(client) if parser == "regular" else _heavy_events(client))


@pytest.mark.parametrize("parser", ("regular", "light", "heavy"))
@pytest.mark.parametrize("role", ("user", "system"))
def test_newer_non_output_lifecycle_invalidates_stale_terminal(
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
    role: str,
) -> None:
    stale = _visible_envelope()
    newer = _lifecycle_after_visible(role)
    if parser == "light":
        stale = {"message": stale["v"]["message"]}  # type: ignore[index]
        newer = {"message": newer["v"]["message"]}  # type: ignore[index]
    client = _install_frames(monkeypatch, [_sse(stale), _sse(newer), "data: [DONE]"])

    if parser == "regular":
        events = asyncio.run(_regular_events(client))
    elif parser == "light":
        events = asyncio.run(_light_events(client))
    else:
        events = asyncio.run(_heavy_events(client))

    assert _PRIVATE_BODY not in repr(events)


@pytest.mark.parametrize("id_mode", ("same-current", "reused-old", "missing"))
def test_regular_non_output_lifecycle_invalidates_all_candidates_regardless_of_id(
    monkeypatch: pytest.MonkeyPatch,
    id_mode: str,
) -> None:
    first = _visible_envelope()
    first_message = first["v"]["message"]  # type: ignore[index]
    first_message["id"] = "assistant-a"  # type: ignore[index]
    first_message["content"]["parts"] = ["STALE-A"]  # type: ignore[index]
    second = deepcopy(first)
    second_message = second["v"]["message"]  # type: ignore[index]
    second_message["id"] = "assistant-b"  # type: ignore[index]
    second_message["content"]["parts"] = ["STALE-B"]  # type: ignore[index]
    newer = _lifecycle_after_visible("user")
    newer_message = newer["v"]["message"]  # type: ignore[index]
    if id_mode == "same-current":
        newer_message["id"] = "assistant-b"  # type: ignore[index]
    elif id_mode == "reused-old":
        newer_message["id"] = "assistant-a"  # type: ignore[index]
    else:
        del newer_message["id"]  # type: ignore[attr-defined]
    client = _install_frames(
        monkeypatch,
        [_sse(first), _sse(second), _sse(newer), "data: [DONE]"],
    )

    events = asyncio.run(_regular_events(client))

    assert "STALE-A" not in repr(events)
    assert "STALE-B" not in repr(events)


@pytest.mark.parametrize("parser", ("regular", "light", "heavy"))
def test_stream_parsers_reject_unknown_message_roles(
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
) -> None:
    unknown = _lifecycle_after_visible("future_backend_role")
    if parser == "light":
        unknown = {"message": unknown["v"]["message"]}  # type: ignore[index]
    client = _install_frames(monkeypatch, [_sse(unknown), "data: [DONE]"])

    with pytest.raises(BackendContractError, match="role"):
        if parser == "regular":
            asyncio.run(_regular_events(client))
        elif parser == "light":
            asyncio.run(_light_events(client))
        else:
            asyncio.run(_heavy_events(client))


@pytest.mark.parametrize("event_type", ("input_message", "message_marker", "future_event"))
def test_heavy_rejects_typed_frames_that_smuggle_message_patches(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    hybrid = {
        "type": event_type,
        "p": "/message/recipient",
        "o": "replace",
        "v": "api_tool.private",
    }
    client = _install_frames(
        monkeypatch,
        [_sse(_visible_envelope()), _sse(hybrid), "data: [DONE]"],
    )

    with pytest.raises(BackendContractError, match="event|patch|type"):
        asyncio.run(_heavy_events(client))


def test_heavy_rejects_unknown_typed_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_frames(
        monkeypatch,
        [_sse(_visible_envelope()), _sse({"type": "future_event"}), "data: [DONE]"],
    )

    with pytest.raises(BackendContractError, match="event|type"):
        asyncio.run(_heavy_events(client))


@pytest.mark.parametrize("parser", ("regular", "light", "heavy"))
@pytest.mark.parametrize("decoder_error", (ValueError, RecursionError))
def test_stream_parsers_normalize_json_decoder_resource_failures(
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
    decoder_error: type[Exception],
) -> None:
    marker = "synthetic-resource-exhaustion-payload"
    real_loads = json.loads

    def _loads(value: str) -> object:
        if value == marker:
            raise decoder_error("synthetic decoder failure")
        return real_loads(value)

    monkeypatch.setattr(sse_mod.json, "loads", _loads)
    client = _install_frames(monkeypatch, [f"data: {marker}", "data: [DONE]"])

    with pytest.raises(BackendContractError, match="JSON|json"):
        if parser == "regular":
            asyncio.run(_regular_events(client))
        elif parser == "light":
            asyncio.run(_light_events(client))
        else:
            asyncio.run(_heavy_events(client))


@pytest.mark.parametrize("path", _METADATA_POINTERS)
def test_regular_v1_rejects_bounded_metadata_pointer_abuse(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    frames = [
        _sse(_visible_envelope(status="in_progress")),
        _sse({"p": path, "o": "replace", "v": {"safe": True}}),
        _sse(
            {
                "p": "/message/status",
                "o": "replace",
                "v": "finished_successfully",
            }
        ),
        "data: [DONE]",
    ]
    client = _install_frames(monkeypatch, frames)

    with pytest.raises(BackendContractError, match="metadata|pointer"):
        asyncio.run(_regular_events(client))


@pytest.mark.parametrize("path", _METADATA_POINTERS)
def test_heavy_v1_rejects_bounded_metadata_pointer_abuse(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    frames = [
        _sse(_visible_envelope(status="in_progress")),
        _sse({"p": path, "o": "replace", "v": {"safe": True}}),
        _sse(
            {
                "p": "/message/status",
                "o": "replace",
                "v": "finished_successfully",
            }
        ),
        "data: [DONE]",
    ]
    client = _install_frames(monkeypatch, frames)

    with pytest.raises(BackendContractError, match="metadata|pointer"):
        asyncio.run(_heavy_events(client))


@pytest.mark.parametrize("parser", ("regular", "heavy"))
def test_v1_root_replacement_revalidates_message_projection(
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
) -> None:
    replacement = _visible_envelope()["v"]
    assert isinstance(replacement, dict)
    message = replacement["message"]
    assert isinstance(message, dict)
    message["recipient"] = "api_tool.private"
    frames = [
        _sse(_visible_envelope()),
        _sse({"p": "", "o": "replace", "v": replacement}),
        "data: [DONE]",
    ]
    client = _install_frames(monkeypatch, frames)

    events = asyncio.run(_regular_events(client) if parser == "regular" else _heavy_events(client))

    assert _PRIVATE_BODY not in repr(events)


@pytest.mark.parametrize("parser", ("regular", "heavy"))
@pytest.mark.parametrize(
    "patch",
    (
        {"p": "", "o": "remove"},
        {"p": "", "o": "replace", "v": "malformed-root"},
    ),
)
def test_v1_unsupported_root_mutation_revokes_buffered_projection(
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
    patch: dict[str, object],
) -> None:
    client = _install_frames(
        monkeypatch,
        [_sse(_visible_envelope()), _sse(patch), "data: [DONE]"],
    )

    events = asyncio.run(_regular_events(client) if parser == "regular" else _heavy_events(client))

    assert _PRIVATE_BODY not in repr(events)
