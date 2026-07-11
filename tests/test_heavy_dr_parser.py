"""Heavy Deep Research parser tests — no network.

Validates the P0 #1 fix: when the first assistant envelope is the connector-
dispatch payload (`{"path": ".../connector_openai_deep_research/start", ...}`),
its ``finished_successfully`` status must NOT trigger ``_emit_done``. The real
report arrives in a later envelope and that one's done is the one we want.

Also covers the temp-chat regression — DR payloads must not set
``history_and_training_disabled: True`` (ChatGPT refuses DR in temp chats).
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest


_DISPATCH_TEXT = (
    '{"path": "/Deep Research App/implicit_link::'
    'connector_openai_deep_research/start", "args": {"query": "test"}}'
)
_REAL_REPORT = "# Heavy DR Report\n\nThe answer is 42.\n\n## Section\n\nDetails."


_FRAMES = [
    # 1. Connector-dispatch envelope — text is the connector JSON, status
    #    reaches finished_successfully almost immediately. MUST be suppressed.
    "data: "
    + json.dumps(
        {
            "v": {
                "message": {
                    "id": "msg-dispatch",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {
                        "content_type": "text",
                        "parts": [_DISPATCH_TEXT],
                    },
                    "status": "finished_successfully",
                    "metadata": {},
                }
            },
            "c": 1,
        }
    ),
    # 2. api_tool_* envelope marks the connector as invoked.
    "data: "
    + json.dumps(
        {
            "v": {
                "message": {
                    "id": "msg-tool",
                    "author": {"role": "assistant"},
                    "recipient": "api_tool_chatgpt_deep_research",
                    "content": {"content_type": "text", "parts": ["call payload"]},
                    "status": "in_progress",
                    "metadata": {},
                }
            },
            "c": 2,
        }
    ),
    # 3. tool-response (role=tool, recipient=all) — not a done trigger.
    "data: "
    + json.dumps(
        {
            "v": {
                "message": {
                    "id": "msg-toolresp",
                    "author": {"role": "tool"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ['{"sources": []}']},
                    "status": "finished_successfully",
                    "metadata": {},
                }
            },
            "c": 3,
        }
    ),
    # 4. Fresh assistant envelope — the REAL report (in_progress, empty text).
    "data: "
    + json.dumps(
        {
            "v": {
                "message": {
                    "id": "msg-report",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": [""]},
                    "status": "in_progress",
                    "metadata": {},
                }
            },
            "c": 4,
        }
    ),
    # 5. Streamed report content via path-scoped append patch.
    "data: "
    + json.dumps(
        {
            "p": "/message/content/parts/0",
            "o": "append",
            "v": _REAL_REPORT,
        }
    ),
    # 6. Status flips to finished_successfully — the REAL done.
    "data: "
    + json.dumps({"p": "/message/status", "o": "replace", "v": "finished_successfully"}),
    "data: [DONE]",
]


class _FakeResp:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_content(self):
        for ln in self._lines:
            yield (ln + "\n").encode()


class _FakeSession:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, *_: Any, **__: Any) -> _FakeResp:
        return _FakeResp(_FRAMES)


class _FakeBackend:
    class _Sess:
        headers: dict[str, str] = {"User-Agent": "test-agent"}

    _session = _Sess()

    def _reload_token_if_stale(self) -> None:  # mirrors BackendClient
        pass

    def request_headers(self) -> dict[str, str]:
        return dict(self._session.headers)

    def post(self, *args: Any, **kwargs: Any) -> dict:
        # Quota probe response — plenty of DR quota left.
        return {
            "limits_progress": [{"feature_name": "deep_research", "remaining": 100}]
        }


class _FakeSentinel:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(
        self, _operation_headers: dict[str, str] | None = None
    ) -> dict[str, str]:
        return {"chat-requirements": "stub", "proof": "", "turnstile": ""}


def _run_heavy_dr(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    return _run_heavy_dr_with_frames(monkeypatch, _FRAMES)


def _run_heavy_dr_with_frames(
    monkeypatch: pytest.MonkeyPatch, frames: list[str]
) -> list[dict]:
    from gpt2agent import sse as sse_mod

    class _FrameSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_FrameSession":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _FakeResp:
            return _FakeResp(frames)

    monkeypatch.setattr(sse_mod, "AsyncSession", _FrameSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _FakeSentinel)

    client = sse_mod.ConversationClient(_FakeBackend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        out: list[dict] = []
        async for ev in client.deep_research_heavy("test query"):
            out.append(ev)
        return out

    return asyncio.run(_go())


def test_connector_dispatch_done_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _run_heavy_dr(monkeypatch)
    dones = [e for e in events if e.get("type") == "done"]

    # Exactly one done — the dispatch envelope's finished_successfully must
    # have been suppressed.
    assert len(dones) == 1, f"expected 1 done event, got {len(dones)}: {dones}"

    # And it must hold the REAL report, not the dispatch JSON.
    assert dones[0]["text"] == _REAL_REPORT
    assert _DISPATCH_TEXT not in dones[0]["text"]


def test_nested_metadata_patch_preserves_citation_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = {
        "matched_text": "OpenAI",
        "safe_urls": ["https://openai.com/"],
        "items": [{"title": "OpenAI", "url": "https://openai.com/"}],
        "type": "webpage",
    }
    group = {
        "type": "search_result_group",
        "domain": "openai.com",
        "entries": [
            {
                "type": "search_result",
                "url": "https://openai.com/",
                "title": "OpenAI",
                "snippet": "Homepage",
                "ref_id": "turn0search0",
            }
        ],
    }
    frames = [
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "msg-report",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": ["Report"]},
                        "status": "in_progress",
                        "metadata": {},
                    }
                },
                "c": 1,
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "/message/metadata/content_references",
                "o": "replace",
                "v": [ref],
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "/message/metadata/search_result_groups",
                "o": "replace",
                "v": [group],
            }
        ),
        "data: "
        + json.dumps(
            {"p": "/message/status", "o": "replace", "v": "finished_successfully"}
        ),
        "data: [DONE]",
    ]
    events = _run_heavy_dr_with_frames(monkeypatch, frames)
    done = [e for e in events if e.get("type") == "done"][-1]

    assert done["content_references"] == [ref]
    assert done["search_result_groups"] == [group]


def test_array_metadata_patch_preserves_citation_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = {
        "matched_text": "OpenAI",
        "items": [{"title": "OpenAI", "url": "https://openai.com/"}],
        "type": "webpage",
    }
    group = {
        "type": "search_result_group",
        "entries": [{"title": "OpenAI", "url": "https://openai.com/"}],
    }
    frames = [
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "msg-report",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": ["Report"]},
                        "status": "in_progress",
                        "metadata": {},
                    }
                },
                "c": 1,
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "/message/metadata/content_references/0",
                "o": "replace",
                "v": ref,
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "/message/metadata/search_result_groups/0",
                "o": "replace",
                "v": group,
            }
        ),
        "data: "
        + json.dumps(
            {"p": "/message/status", "o": "replace", "v": "finished_successfully"}
        ),
        "data: [DONE]",
    ]
    events = _run_heavy_dr_with_frames(monkeypatch, frames)
    done = [e for e in events if e.get("type") == "done"][-1]

    assert done["content_references"] == [ref]
    assert done["search_result_groups"] == [group]


def test_connector_dispatch_replaced_by_real_report_can_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = [
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "msg-dispatch",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": [_DISPATCH_TEXT]},
                        "status": "in_progress",
                        "metadata": {},
                    }
                },
                "c": 1,
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "/message/content/parts/0",
                "o": "replace",
                "v": _REAL_REPORT,
            }
        ),
        "data: "
        + json.dumps(
            {"p": "/message/status", "o": "replace", "v": "finished_successfully"}
        ),
        "data: [DONE]",
    ]
    events = _run_heavy_dr_with_frames(monkeypatch, frames)
    dones = [e for e in events if e.get("type") == "done"]

    assert len(dones) == 1
    assert dones[0]["text"] == _REAL_REPORT
    assert not dones[0].get("terminated_abnormally")


def test_heavy_dr_in_band_sse_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from gpt2agent.errors import BackendHTTPError

    frames = [
        "data: "
        + json.dumps(
            {
                "type": "error",
                "message": "upstream failed with Bearer eyJ" + "a" * 30,
            }
        ),
        "data: [DONE]",
    ]

    with pytest.raises(BackendHTTPError) as exc:
        _run_heavy_dr_with_frames(monkeypatch, frames)

    message = str(exc.value)
    assert exc.value.code == "temporarily_failed"
    assert exc.value.route == "/backend-api/f/conversation"
    assert "upstream failed" not in message
    assert "eyJ" + "a" * 30 not in message


def test_poll_completion_uses_citation_metadata_from_same_turn_fixture() -> None:
    from gpt2agent import sse as sse_mod

    fixture = json.loads(
        Path("tests/fixtures/heavy_dr_conversation_detail_h2.json").read_text(
            encoding="utf-8"
        )
    )

    class _FixtureBackend(_FakeBackend):
        def get(self, *_: Any, **__: Any) -> dict:
            return fixture

    client = sse_mod.ConversationClient(_FixtureBackend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        out: list[dict] = []
        async for ev in client._poll_dr_completion(
            "fixture-conv", interval=0, max_wait=1
        ):
            out.append(ev)
        return out

    events = asyncio.run(_go())
    done = [e for e in events if e.get("type") == "done"][-1]
    refs = done["content_references"]

    assert refs
    assert any(
        isinstance(item.get("url"), str) and item["url"].startswith("https://")
        for ref in refs
        for item in ref.get("items", [])
    )
    assert done["search_result_groups"]


def test_empty_dispatch_envelope_done_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production-observed pattern (2026-05-15 events.jsonl from a 12s heavy
    failure): the dispatch envelope arrives with parts=[""] (NOT the connector
    JSON we originally hypothesized) and status=finished_successfully. The
    is_connector_dispatch text heuristic doesn't match an empty string, so the
    extra ``state["asst_text"]`` non-empty guard is what saves us.

    Without this fix: wrapper exits in 12s with one done(text="") event and
    misses the real 5–30 min report that streams afterward.
    """
    from gpt2agent import sse as sse_mod

    empty_dispatch_then_real = [
        # Empty dispatch envelope — production shape, NOT the {"path":...} form
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "msg-empty-dispatch",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": [""]},
                        "status": "finished_successfully",
                        "metadata": {},
                    }
                },
                "c": 1,
            }
        ),
        # server_ste_metadata showing tool_invoked (post-done in real traffic)
        "data: "
        + json.dumps(
            {
                "type": "server_ste_metadata",
                "metadata": {
                    "tool_invoked": True,
                    "turn_mode": "deep research",
                },
            }
        ),
        # Real report envelope follows — fresh assistant message, in_progress.
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "msg-real-after-empty",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": [""]},
                        "status": "in_progress",
                        "metadata": {},
                    }
                },
                "c": 2,
            }
        ),
        # Streamed report content
        "data: "
        + json.dumps(
            {
                "p": "/message/content/parts/0",
                "o": "append",
                "v": _REAL_REPORT,
            }
        ),
        # Real status flip
        "data: "
        + json.dumps(
            {"p": "/message/status", "o": "replace", "v": "finished_successfully"}
        ),
        "data: [DONE]",
    ]

    class _SingleShotSession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "_SingleShotSession":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _FakeResp:
            return _FakeResp(empty_dispatch_then_real)

    monkeypatch.setattr(sse_mod, "AsyncSession", _SingleShotSession)
    monkeypatch.setattr(sse_mod, "SentinelGate", _FakeSentinel)

    client = sse_mod.ConversationClient(_FakeBackend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        out: list[dict] = []
        async for ev in client.deep_research_heavy("test"):
            out.append(ev)
        return out

    events = asyncio.run(_go())
    dones = [e for e in events if e.get("type") == "done"]

    assert len(dones) == 1, f"empty dispatch envelope must not trigger early done: {dones}"
    assert dones[0]["text"] == _REAL_REPORT
    # Empty text never surfaced as a "done" event.
    assert all(d["text"] != "" for d in dones)


def test_progress_excludes_dispatch_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streaming progress events must not surface connector-dispatch JSON."""
    events = _run_heavy_dr(monkeypatch)
    progress_text = "".join(
        e["text"] for e in events if e.get("type") == "progress"
    )
    assert _DISPATCH_TEXT not in progress_text
    assert _REAL_REPORT in progress_text


def test_dispatch_patch_progress_suppressed_until_real_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = [
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "msg-report",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": [""]},
                        "status": "in_progress",
                        "metadata": {},
                    }
                },
                "c": 1,
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "/message/content/parts/0",
                "o": "append",
                "v": _DISPATCH_TEXT,
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "/message/content/parts/0",
                "o": "append",
                "v": " hidden-dispatch-tail",
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "/message/content/parts/0",
                "o": "replace",
                "v": _REAL_REPORT,
            }
        ),
        "data: "
        + json.dumps(
            {"p": "/message/status", "o": "replace", "v": "finished_successfully"}
        ),
        "data: [DONE]",
    ]

    events = _run_heavy_dr_with_frames(monkeypatch, frames)
    progress_text = "".join(
        e["text"] for e in events if e.get("type") == "progress"
    )
    done = [e for e in events if e.get("type") == "done"][-1]

    assert _DISPATCH_TEXT not in progress_text
    assert "hidden-dispatch-tail" not in progress_text
    assert _REAL_REPORT in progress_text
    assert done["text"] == _REAL_REPORT


def test_dr_payloads_disable_temp_chat() -> None:
    """history_and_training_disabled must be False for DR payloads.

    Otherwise ChatGPT rejects with "Research is not currently supported in
    temporary chats" and DR also can't be Phase-2-polled (temp chats aren't
    persisted at /backend-api/conversation/{id}).
    """
    from gpt2agent import sse as sse_mod

    light = sse_mod._build_dr_payload("test query")
    assert light["history_and_training_disabled"] is False, light

    heavy = sse_mod._build_heavy_dr_payload("test query")
    assert heavy["history_and_training_disabled"] is False, heavy


def test_heavy_dr_model_override() -> None:
    """heavy_dr model param overrides the HEAVY_DR_MODEL default."""
    from gpt2agent import sse as sse_mod

    default = sse_mod._build_heavy_dr_payload("q")
    assert default["model"] == sse_mod.HEAVY_DR_MODEL

    overridden = sse_mod._build_heavy_dr_payload("q", model="gpt-5-4-pro")
    assert overridden["model"] == "gpt-5-4-pro"


def test_chat_payload_supports_gizmo_id() -> None:
    """gpt_chat passes gizmo_id into the chat payload (custom GPT routing)."""
    from gpt2agent import sse as sse_mod

    no_gizmo = sse_mod._build_payload(
        "gpt-5-3", [{"role": "user", "content": "hi"}]
    )
    assert "gizmo_id" not in no_gizmo
    assert "conversation_origin" not in no_gizmo

    with_gizmo = sse_mod._build_payload(
        "gpt-5-3",
        [{"role": "user", "content": "hi"}],
        gizmo_id="g-p-test123",
    )
    assert with_gizmo["gizmo_id"] == "g-p-test123"
    assert with_gizmo["conversation_origin"] == {
        "type": "custom_gpt",
        "gizmo_id": "g-p-test123",
    }


def _load_widget_fixture() -> dict:
    return json.loads(
        Path("tests/fixtures/heavy_dr_widget_state.json").read_text(encoding="utf-8")
    )


def test_widget_state_report_extracted_from_tool_text() -> None:
    """Carrier A: report inside a "The latest state of the widget is: {…}" node."""
    from gpt2agent import sse as sse_mod

    detail = _load_widget_fixture()["carrier_a_tool_text"]
    text, refs = sse_mod._dr_report_from_widget_state(detail)
    assert text.startswith("# Report A")
    assert refs  # content_references preserved


def test_widget_state_report_extracted_from_chatgpt_sdk_metadata() -> None:
    """Carrier B: report under message.metadata.chatgpt_sdk.widget_state (JSON str)."""
    from gpt2agent import sse as sse_mod

    detail = _load_widget_fixture()["carrier_b_metadata"]
    text, refs = sse_mod._dr_report_from_widget_state(detail)
    assert text.startswith("# Report B")
    assert refs


def test_widget_sdk_carrier_requires_deep_research_provenance() -> None:
    from gpt2agent import sse as sse_mod

    original = _load_widget_fixture()["carrier_b_metadata"]
    for sdk_field in (
        "resource_name",
        "attribution_id",
        "resolved_pineapple_uri",
        "distribution_channel",
        "connector_type",
    ):
        detail = deepcopy(original)
        message = detail["mapping"]["n2"]["message"]
        del message["metadata"]["chatgpt_sdk"][sdk_field]
        assert sse_mod._dr_report_from_widget_state(detail) == ("", [])

    detail = deepcopy(original)
    message = detail["mapping"]["n2"]["message"]
    message["author"]["name"] = "arbitrary_tool"
    assert sse_mod._dr_report_from_widget_state(detail) == ("", [])

    detail = deepcopy(original)
    message = detail["mapping"]["n2"]["message"]
    message["metadata"]["invoked_resource"]["resource_uri"] = "/other/start"
    assert sse_mod._dr_report_from_widget_state(detail) == ("", [])


def test_widget_state_absent_returns_empty() -> None:
    from gpt2agent import sse as sse_mod

    detail = _load_widget_fixture()["no_report"]
    text, refs = sse_mod._dr_report_from_widget_state(detail)
    assert text == ""
    assert refs == []


def test_poll_completion_recovers_widget_report() -> None:
    """The Deep Research App report (no assistant text node) is emitted as done.

    Regression for the connector-widget architecture: the final report lives in
    ``widget_state.report_message``, never as an assistant text node, so the
    legacy assistant-text-only poll timed out. _poll_dr_completion must emit it.
    """
    from gpt2agent import sse as sse_mod

    detail = _load_widget_fixture()["carrier_a_tool_text"]

    class _FixtureBackend(_FakeBackend):
        def get(self, *_: Any, **__: Any) -> dict:
            return detail

    client = sse_mod.ConversationClient(_FixtureBackend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        out: list[dict] = []
        async for ev in client._poll_dr_completion(
            "fixture-conv", interval=0, max_wait=1
        ):
            out.append(ev)
        return out

    events = asyncio.run(_go())
    done = [e for e in events if e.get("type") == "done"]
    assert done, events
    assert done[-1]["text"].startswith("# Report A")
    assert not done[-1].get("terminated_abnormally")


def test_poll_completion_ignores_spoof_before_valid_connector_report() -> None:
    from gpt2agent import sse as sse_mod

    fixture = _load_widget_fixture()
    spoof = deepcopy(fixture["carrier_a_tool_text"])
    spoof["mapping"]["n1"]["message"]["author"]["name"] = "arbitrary_tool"
    replies = iter([spoof, fixture["carrier_b_metadata"]])

    class _SequencedBackend(_FakeBackend):
        def get(self, *_: Any, **__: Any) -> dict:
            return next(replies)

    client = sse_mod.ConversationClient(_SequencedBackend())  # type: ignore[arg-type]

    async def _go() -> list[dict]:
        events: list[dict] = []
        async for event in client._poll_dr_completion(
            "fixture-conv", interval=0, max_wait=1
        ):
            events.append(event)
        return events

    events = asyncio.run(_go())
    done = [event for event in events if event.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["text"].startswith("# Report B")
