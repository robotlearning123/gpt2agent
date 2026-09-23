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
from datetime import datetime, timedelta, timezone
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

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


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

    def post(self, *args: Any, **kwargs: Any) -> dict:
        # Quota probe response — plenty of DR quota left.
        return {
            "limits_progress": [{"feature_name": "deep_research", "remaining": 100}]
        }


class _FakeSentinel:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def get_tokens(self) -> dict[str, str]:
        return {"chat-requirements": "stub", "proof": "", "turnstile": ""}


def _run_heavy_dr(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    return _run_heavy_dr_with_frames(monkeypatch, _FRAMES)


def _run_heavy_dr_with_frames(
    monkeypatch: pytest.MonkeyPatch, frames: list[str], backend: Any = None
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

    client = sse_mod.ConversationClient(  # type: ignore[arg-type]
        backend or _FakeBackend()
    )

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

    with pytest.raises(RuntimeError) as exc:
        _run_heavy_dr_with_frames(monkeypatch, frames)

    message = str(exc.value)
    assert "ChatGPT SSE error" in message
    assert "eyJ" + "a" * 30 not in message
    assert "Bearer <REDACTED>" in message


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
    ):
        detail = deepcopy(original)
        message = detail["mapping"]["n2"]["message"]
        del message["metadata"]["chatgpt_sdk"][sdk_field]
        assert sse_mod._dr_report_from_widget_state(detail) == ("", [])

    # ``connector_type`` is optional: account B omits it while carrying every
    # other identity field (measured 2026-09-23 on the account-B retest
    # run), so absence must still extract — but a foreign connector type
    # must not.
    detail = deepcopy(original)
    sdk = detail["mapping"]["n2"]["message"]["metadata"]["chatgpt_sdk"]
    del sdk["connector_type"]
    text, refs = sse_mod._dr_report_from_widget_state(detail)
    assert text.startswith("# Report B")
    assert refs

    detail = deepcopy(original)
    detail["mapping"]["n2"]["message"]["metadata"]["chatgpt_sdk"][
        "connector_type"
    ] = "THIRD_PARTY_ECOSYSTEM"
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


# --------------------------------------------------------------------------- #
#  Live heavy-DR delivery gate — live-matrix 2026-09-23 (lane A-dr-heavy)
#
#  Evidence: artifacts/verify/live-matrix-2026-09-23/A-dr-heavy/
#    * conversation_state.json — the fetched payload (tool node 936145e2) that
#      the shipped extractor returned ("", []) on: invoked_resource.resource_uri
#      was "/connector_openai_deep_research/start" (plain form) where the
#      extractor demanded the "implicit_link::" long form.
#    * events.jsonl / poll_events.jsonl — the 160-char async ack delivered as
#      ``done`` by BOTH the SSE stream and the Phase-2 poll, while the report sat
#      in chatgpt_sdk.widget_state.report_message.
#
#  Payloads below are GENERATED in code from the structure observed in that
#  evidence — no copied payload bytes, no hand-crafted blobs.
# --------------------------------------------------------------------------- #

_LIVE_DR_START_URI = "/connector_openai_deep_research/start"
_LONG_FORM_DR_START_URI = (
    "/connector_openai_deep_research/implicit_link::"
    "connector_openai_deep_research/start"
)
_DR_ASYNC_ACK = (
    "Deep Research has started working on this. It will first show its "
    "research plan, then produce the generated answer with a citation list."
)
_LIVE_TS_BASE = datetime(2026, 9, 23, 18, 56, 40, tzinfo=timezone.utc)


def _generated_ts(offset_s: int) -> str:
    return (_LIVE_TS_BASE + timedelta(seconds=offset_s)).isoformat()


def _generated_report(marker: str, *, findings: int = 4) -> str:
    """Build a report body programmatically (generated, never copied bytes)."""
    lines = [f"# {marker}", "", "## Executive summary", ""]
    lines += [
        f"Finding {i}: measured value {i * 137}, see generated source {i}."
        for i in range(1, findings + 1)
    ]
    return "\n".join(lines)


def _generated_refs(count: int = 3) -> list[dict]:
    """content_references in the live ``grouped_webpages`` shape."""
    return [
        {
            "type": "grouped_webpages",
            "safe_urls": [f"https://example.org/generated-{i}"],
            "items": [
                {
                    "title": f"Generated source {i}",
                    "url": f"https://example.org/generated-{i}",
                    "attribution": "example.org",
                    "snippet": None,
                    "pub_date": None,
                }
            ],
        }
        for i in range(1, count + 1)
    ]


def _generated_widget_state(
    *,
    report: str,
    refs: int = 3,
    widget_status: str = "completed",
    report_status: str = "finished_successfully",
) -> str:
    """The widget_state JSON string exactly as the live payload carries it."""
    return json.dumps(
        {
            "status": widget_status,
            "plan": {
                "plan_id": "plan-generated",
                "title": "Generated plan",
                "version": 1,
                "steps": [
                    {
                        "id": f"step-{i}",
                        "text": f"Generated step {i}",
                        "status": "completed",
                        "reason": None,
                    }
                    for i in range(1, 4)
                ],
            },
            "step_statuses_by_plan": {"plan-generated": {"step-1": "completed"}},
            "research_started_at": _generated_ts(0),
            "research_stopped_at": _generated_ts(316),
            "report_message": {
                "id": "generated-report",
                "author": {"role": "assistant", "name": None},
                "recipient": "all",
                "channel": "final",
                "status": report_status,
                "content": {"content_type": "text", "parts": [report]},
                "metadata": {
                    "content_references": _generated_refs(refs),
                    "search_result_groups": [],
                    "is_complete": True,
                },
            },
        }
    )


def _generated_live_detail(
    *,
    report: str,
    resource_uri: str = _LIVE_DR_START_URI,
    include_ack: bool = True,
    widget_status: str = "completed",
    report_status: str = "finished_successfully",
) -> dict:
    """Conversation detail in the shape captured live on 2026-09-23."""
    mapping: dict[str, dict] = {
        "generated-tool-node": {
            "message": {
                "id": "generated-tool-node",
                "author": {"role": "tool", "name": "api_tool.call_tool"},
                "recipient": "all",
                "status": "finished_successfully",
                "content": {
                    "content_type": "code",
                    "language": "json",
                    "text": json.dumps({"session_id": "generated-session"}),
                },
                "metadata": {
                    "chatgpt_sdk": {
                        "resource_name": "Deep Research App_start",
                        "attribution_id": "connector_openai_deep_research",
                        "resolved_pineapple_uri": (
                            "connectors://connector_openai_deep_research"
                        ),
                        "distribution_channel": "openai",
                        "connector_type": "FIRST_PARTY_ECOSYSTEM",
                        "widget_state": _generated_widget_state(
                            report=report,
                            widget_status=widget_status,
                            report_status=report_status,
                        ),
                        "tool_response_metadata": {
                            "venus_message_type": "initial_loading_message",
                            "openai/asyncStatus": 7,
                        },
                    },
                    "invoked_resource": {
                        "resource_uri": resource_uri,
                        "app_name": "Deep Research App",
                    },
                },
            }
        }
    }
    if include_ack:
        mapping["generated-ack-node"] = {
            "message": {
                "id": "generated-ack",
                "author": {"role": "assistant", "name": None},
                "recipient": "all",
                "status": "finished_successfully",
                "content": {"content_type": "text", "parts": [_DR_ASYNC_ACK]},
                "create_time": 2.0,
                "metadata": {"is_complete": True, "story_events": []},
            }
        }
    return {"mapping": mapping}


def test_dr_async_ack_matcher_is_prefix_only() -> None:
    """The ack matcher fires on the connector's opening claim, not on a report."""
    from gpt2agent import sse as sse_mod

    assert sse_mod._is_dr_async_ack(_DR_ASYNC_ACK)
    assert sse_mod._is_dr_async_ack(
        "Deep research has started working on their query."
    )
    assert not sse_mod._is_dr_async_ack(_generated_report("# Report"))
    assert not sse_mod._is_dr_async_ack(
        "# Report\n\nDeep Research has started working on this."
    )
    assert not sse_mod._is_dr_async_ack("")


def test_widget_report_extracted_from_live_start_uri() -> None:
    """The live plain ``/start`` carrier parses, the long form keeps working."""
    from gpt2agent import sse as sse_mod

    report = _generated_report("# Live-shape report")
    for uri in (_LIVE_DR_START_URI, _LONG_FORM_DR_START_URI):
        detail = _generated_live_detail(report=report, resource_uri=uri)
        text, refs = sse_mod._dr_report_from_widget_state(detail)
        assert text == report, uri
        assert len(refs) == 3, uri

    # Connector scope is still enforced: another connector's resource is out.
    other = _generated_live_detail(
        report=report, resource_uri="/other_connector/start"
    )
    assert sse_mod._dr_report_from_widget_state(other) == ("", [])

    # ...and so is the completion gate: a running widget is not a report.
    pending = _generated_live_detail(
        report=report, widget_status="running", report_status="in_progress"
    )
    assert sse_mod._dr_report_from_widget_state(pending) == ("", [])


def test_heavy_stream_delivers_widget_report_instead_of_async_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 1 must not ship the ack; Phase 2 must return the widget report.

    Frame order replays the live stream (raw_sse.jsonl): resume token with the
    conversation id, the assistant envelope addressed to the connector, the DR
    tool envelope carrying openai/asyncStatus, then the ack text reaching
    finished_successfully — which the shipped surface returned as the answer.
    """
    from gpt2agent import sse as sse_mod

    report = _generated_report("# Recalled from widget state")
    conversation = _generated_live_detail(report=report)
    pending_tool = deepcopy(conversation["mapping"]["generated-tool-node"])
    pending_tool["message"]["metadata"]["chatgpt_sdk"]["widget_state"] = (
        _generated_widget_state(
            report="", widget_status="running", report_status="in_progress"
        )
    )

    class _WidgetBackend(_FakeBackend):
        def get(self, *_: Any, **__: Any) -> dict:
            return conversation

    real_poll = sse_mod.ConversationClient._poll_dr_completion

    def _fast_poll(self, conv_id, *, seed_text="", connector_failed=False, **__):
        # Same real poll implementation, only the wait knobs shortened.
        return real_poll(
            self,
            conv_id,
            seed_text=seed_text,
            connector_failed=connector_failed,
            interval=0.0,
            max_wait=2.0,
        )

    monkeypatch.setattr(
        sse_mod.ConversationClient, "_poll_dr_completion", _fast_poll
    )

    frames = [
        "data: "
        + json.dumps(
            {
                "type": "resume_conversation_token",
                "token": "generated",
                "conversation_id": "generated-conv",
            }
        ),
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "generated-call",
                        "author": {"role": "assistant"},
                        "recipient": "api_tool.call_tool",
                        "content": {"content_type": "code", "text": "{}"},
                        "status": "in_progress",
                        "metadata": {},
                    }
                },
                "c": 1,
            }
        ),
        "data: " + json.dumps({"v": pending_tool, "c": 2}),
        "data: "
        + json.dumps(
            {
                "v": {
                    "message": {
                        "id": "generated-ack",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": [""]},
                        "status": "in_progress",
                        "metadata": {},
                    }
                },
                "c": 3,
            }
        ),
        "data: "
        + json.dumps(
            {
                "p": "/message/content/parts/0",
                "o": "append",
                "v": _DR_ASYNC_ACK,
            }
        ),
        "data: "
        + json.dumps(
            {"p": "/message/status", "o": "replace", "v": "finished_successfully"}
        ),
        "data: [DONE]",
    ]

    events = _run_heavy_dr_with_frames(
        monkeypatch, frames, backend=_WidgetBackend()
    )
    dones = [e for e in events if e.get("type") == "done"]

    assert len(dones) == 1, f"expected 1 done, got {dones}"
    assert dones[0]["text"] == report
    assert _DR_ASYNC_ACK not in dones[0]["text"]
    # The ack still streamed as progress — it is not hidden, just not final.
    assert any(
        e.get("type") == "progress" and e.get("text") == _DR_ASYNC_ACK
        for e in events
    )


def test_heavy_poll_ignores_async_ack_until_widget_report_lands() -> None:
    """Phase 2 keeps polling past the ack (poll_events.jsonl regression)."""
    from gpt2agent import sse as sse_mod

    report = _generated_report("# Widget report after the ack")
    pending = _generated_live_detail(
        report="", widget_status="running", report_status="in_progress"
    )
    finished = _generated_live_detail(report=report)
    replies = iter([pending, finished])
    calls: list[str] = []

    class _SequencedBackend(_FakeBackend):
        def get(self, path: str, **__: Any) -> dict:
            calls.append(path)
            return next(replies)

    client = sse_mod.ConversationClient(  # type: ignore[arg-type]
        _SequencedBackend()
    )

    async def _go() -> list[dict]:
        out: list[dict] = []
        async for ev in client._poll_dr_completion(
            "generated-conv", seed_text=_DR_ASYNC_ACK, interval=0, max_wait=2.0
        ):
            out.append(ev)
        return out

    events = asyncio.run(_go())
    dones = [e for e in events if e.get("type") == "done"]

    assert len(calls) == 2, "the poll must survive the ack-only first read"
    assert len(dones) == 1, f"expected 1 done, got {dones}"
    assert dones[0]["text"] == report
    assert not dones[0].get("terminated_abnormally")


def test_heavy_poll_timeout_drops_async_ack() -> None:
    """If the widget report never lands, ship the timeout — never the ack."""
    from gpt2agent import sse as sse_mod

    pending = _generated_live_detail(
        report="", widget_status="running", report_status="in_progress"
    )

    class _PendingBackend(_FakeBackend):
        def get(self, *_: Any, **__: Any) -> dict:
            return pending

    client = sse_mod.ConversationClient(  # type: ignore[arg-type]
        _PendingBackend()
    )

    async def _go() -> list[dict]:
        out: list[dict] = []
        async for ev in client._poll_dr_completion(
            "generated-conv", seed_text=_DR_ASYNC_ACK, interval=0, max_wait=0.0
        ):
            out.append(ev)
        return out

    events = asyncio.run(_go())
    dones = [e for e in events if e.get("type") == "done"]

    assert len(dones) == 1, f"expected 1 done, got {dones}"
    assert dones[0]["text"] == ""
    assert dones[0]["timeout"] is True
    assert dones[0]["terminated_abnormally"] is True
