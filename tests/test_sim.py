"""Tests for the v0.0.17 website-simulation layer.

Covers SimProfile identity consistency, the frontend payload shape, header
merging, limits pre-flight, and the v1 delta-encoding frame handler in
``ConversationClient.stream``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from gpt2agent.backend import UsageLimitError, limits_from_init
from gpt2agent.sim import IMPERSONATE, SimProfile
from gpt2agent.sse import (
    ConversationClient,
    _build_payload,
    _merge_headers,
    _raise_for_sse_error,
)


def _profile(**over) -> SimProfile:
    cfg: dict[str, Any] = {"sentinel": over}
    p = SimProfile(cfg)
    # No network in tests — pin the fields that would otherwise scrape/detect.
    p._client_version = "prod-test123"
    p._client_version_at = 9e18
    return p


# ── SimProfile identity ──────────────────────────────────────────────────────


def test_profile_geo_fields_are_consistent() -> None:
    p = _profile(timezone="America/New_York", locale="en-US")
    assert p.timezone == "America/New_York"
    assert p.timezone_offset_min in (240, 300)  # JS getTimezoneOffset for NY
    assert p.nav_languages == "en-US,en"
    assert p.accept_language == "en-US,en;q=0.9"


def test_profile_non_english_locale_adds_en_fallbacks() -> None:
    p = _profile(locale="de-DE")
    assert p.nav_languages == "de-DE,de,en-US,en"
    assert p.accept_language == "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"


def test_fingerprint_config_shape() -> None:
    p = _profile(timezone="America/New_York")
    cfg = p.fingerprint_config()
    assert len(cfg) == 18
    assert cfg[0] == 4880
    assert cfg[4] == p.ua
    assert cfg[6] == "prod-test123"
    assert cfg[7] == p.locale
    assert cfg[8] == p.nav_languages
    assert cfg[14] == p.session_id
    assert isinstance(cfg[17], int)


def test_contextual_info_uses_screen() -> None:
    p = _profile(screen="1920x1080")
    ci = p.contextual_info()
    assert ci["screen_width"] == 1920 and ci["screen_height"] == 1080
    assert ci["time_since_loaded"] >= 3


def test_echo_logs_shape() -> None:
    p = _profile()
    parts = p.echo_logs().split(",")
    assert len(parts) == 4
    assert 6000 <= int(parts[1]) <= 9000
    assert int(parts[3]) > int(parts[1])


# ── payload ──────────────────────────────────────────────────────────────────


def test_payload_matches_frontend_shape() -> None:
    p = _profile(timezone="America/New_York")
    pl = _build_payload("gpt-6-pro", [{"role": "user", "content": "hi"}], profile=p)
    assert pl["parent_message_id"] == "client-created-root"
    assert pl["model"] == "gpt-6-pro"
    assert pl["timezone"] == "America/New_York"
    assert pl["timezone_offset_min"] == p.timezone_offset_min
    assert pl["supports_buffering"] is True
    assert pl["supported_encodings"] == ["v1"]
    assert pl["enable_message_followups"] is True
    assert pl["paragen_cot_summary_display_override"] == "allow"
    assert pl["force_parallel_switch"] == "auto"
    assert pl["client_contextual_info"]["screen_width"] == p.screen_w
    msg = pl["messages"][0]
    assert msg["create_time"]
    assert msg["metadata"]["serialization_metadata"] == {"custom_symbol_offsets": []}


# ── header merge ─────────────────────────────────────────────────────────────


def test_merge_headers_replaces_case_insensitive() -> None:
    base = {"OAI-Device-Id": "old", "OAI-Client-Version": "prod-old", "X": "1"}
    merged = _merge_headers(
        base, {"oai-device-id": "new", "oai-client-version": "prod-new"}
    )
    assert merged["oai-device-id"] == "new"
    assert merged["oai-client-version"] == "prod-new"
    assert "OAI-Device-Id" not in merged  # the duplicate-casing key is gone
    assert merged["X"] == "1"


# ── limits / usage-limit errors ──────────────────────────────────────────────


_INIT = {
    "default_model_slug": "gpt-5-6-thinking",
    "intended_default_model_slug": "gpt-6-pro",
    "model_limits": [{"model_slug": "gpt-6-pro", "resets_after": "2026-09-20T01:31:58Z"}],
    "limits_progress": [
        {"feature_name": "deep_research", "remaining": 0,
         "reset_after": "2026-09-18T21:56:57Z"}
    ],
    "blocked_features": [{"name": "reason"}],
}


def test_limits_from_init_parses_model_cap() -> None:
    info = limits_from_init(_INIT, model="gpt-6-pro", feature="deep_research")
    assert info["model_resets_after"] == "2026-09-20T01:31:58Z"
    assert info["feature_remaining"] == 0
    assert info["feature_resets_after"] == "2026-09-18T21:56:57Z"
    assert info["default_model_slug"] == "gpt-5-6-thinking"


def test_limits_from_init_uncapped_model() -> None:
    info = limits_from_init(_INIT, model="gpt-5-6")
    assert info["model_resets_after"] is None


def test_sse_usage_limit_raises_typed_error() -> None:
    with pytest.raises(UsageLimitError):
        _raise_for_sse_error(
            {"error_code": "usage_limit", "error": "You've hit your limit."}
        )


def test_sse_generic_error_stays_runtime_error() -> None:
    with pytest.raises(RuntimeError) as ei:
        _raise_for_sse_error({"type": "error", "message": "boom"})
    assert not isinstance(ei.value, UsageLimitError)


class _CapBackend:
    """Minimal backend stand-in for preflight tests."""

    def __init__(self, init: dict | None) -> None:
        self._init = init

    def _reload_token_if_stale(self) -> None:
        pass

    def _token(self) -> str:
        return "tok"

    class _Sess:
        headers: dict = {}

        def __init__(self, outer):
            self._o = outer

        def post(self, url, **kw):
            outer = self._o

            class _R:
                status_code = 200

                def json(self):
                    return outer._init

            return _R()

    @property
    def _session(self):
        return _CapBackend._Sess(self)


def test_check_model_cap_raises_with_reset() -> None:
    conv = ConversationClient(_CapBackend(_INIT))
    conv._limits_cache = (0, _INIT)
    with pytest.raises(UsageLimitError) as ei:
        asyncio.run(conv._check_model_cap("gpt-6-pro"))
    assert "gpt-6-pro" in str(ei.value)
    assert ei.value.resets_after == "2026-09-20T01:31:58Z"


def test_check_model_cap_passes_uncapped() -> None:
    conv = ConversationClient(_CapBackend(_INIT))
    conv._limits_cache = (0, _INIT)
    asyncio.run(conv._check_model_cap("gpt-5-6"))


def test_check_model_cap_fails_open() -> None:
    conv = ConversationClient(_CapBackend(None))
    conv._limits_cache = (0, None)
    asyncio.run(conv._check_model_cap("gpt-6-pro"))


# ── v1 delta-encoding frame handling (f/conversation) ────────────────────────


class _FakeLine:
    def __init__(self, s: str) -> None:
        self.s = s


class _FakeResp:
    """Async line iterator over canned SSE frames."""

    status_code = 200

    def __init__(self, frames: list[str]) -> None:
        self._lines = [f"data: {f}" for f in frames] + ["data: [DONE]"]

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeSession:
    """AsyncSession stand-in capturing the posted payload."""

    last_payload: dict = {}

    def __init__(self, *a: Any, resp: _FakeResp, **k: Any) -> None:
        self._resp = resp
        self.cookies = _FakeCookies()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, *, headers=None, json=None, timeout=None, stream=None):
        type(self).last_payload = json or {}
        return self._resp


class _FakeCookies:
    def set(self, k, v):
        pass


_V1_FRAMES = [
    '"v1"',
    json.dumps({"type": "resume_conversation_token", "token": "tok",
                "conversation_id": "conv-1"}),
    json.dumps({"p": "", "o": "add", "v": {"message": {
        "id": "u1", "author": {"role": "user"},
        "content": {"content_type": "text", "parts": ["the prompt"]},
        "status": "finished_successfully"}}}),
    json.dumps({"v": {"message": {
        "id": "a1", "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": [""]},
        "status": "in_progress"}}}),
    json.dumps({"p": "/message/content/parts/0", "o": "append", "v": "PARSE"}),
    json.dumps({"p": "/message/content/parts/0", "o": "append", "v": "D-OK"}),
    json.dumps({"type": "server_ste_metadata",
                "metadata": {"model_slug": "gpt-6-pro"},
                "conversation_id": "conv-1"}),
    json.dumps({"type": "message_stream_complete",
                "conversation_id": "conv-1"}),
]


def _run_stream(frames: list[str], monkeypatch) -> list:
    """Drive ConversationClient.stream over canned frames, zero network."""
    conv = ConversationClient(_CapBackend(None))
    conv._limits_cache = (0, None)  # skip the init probe
    conv._bridge_cookies = None

    async def _no_bridge(model="auto"):
        return None

    class _NoGate:
        def __init__(self, *a, **k):
            pass

        async def get_tokens(self):
            return {"chat-requirements": "t"}

    import gpt2agent.sse as sse_mod

    monkeypatch.setattr(conv, "_bridge_headers", _no_bridge)
    monkeypatch.setattr(sse_mod, "SentinelGate", _NoGate)
    monkeypatch.setattr(
        sse_mod, "AsyncSession",
        lambda *a, **k: _FakeSession(resp=_FakeResp(frames)),
    )

    async def _collect():
        out = []
        async for ev in conv.stream("gpt-6-pro", [{"role": "user", "content": "hi"}]):
            out.append(ev)
        return out

    return asyncio.run(_collect())


def test_stream_parses_v1_delta_encoding(monkeypatch) -> None:
    events = _run_stream(_V1_FRAMES, monkeypatch)
    text = "".join(e for e in events if isinstance(e, str))
    assert text == "PARSED-OK"
    sentinel = [e for e in events if isinstance(e, dict)]
    assert sentinel[0]["_resolved_model"] == "gpt-6-pro"
    assert sentinel[0]["_conversation_id"] == "conv-1"
    # the user-message echo must not leak into the text stream
    assert "the prompt" not in text


def test_stream_downgrade_note_via_complete(monkeypatch) -> None:
    frames = _V1_FRAMES[:-1] + [
        json.dumps({"type": "server_ste_metadata",
                    "metadata": {"model_slug": "gpt-5-6-mini"},
                    "conversation_id": "conv-1"}),
        json.dumps({"type": "message_stream_complete",
                    "conversation_id": "conv-1"}),
    ]
    conv = ConversationClient(_CapBackend(None))
    conv._limits_cache = (0, None)
    conv._bridge_cookies = None

    async def _no_bridge(model="auto"):
        return None

    class _NoGate:
        def __init__(self, *a, **k):
            pass

        async def get_tokens(self):
            return {"chat-requirements": "t"}

    import gpt2agent.sse as sse_mod

    monkeypatch.setattr(conv, "_bridge_headers", _no_bridge)
    monkeypatch.setattr(sse_mod, "SentinelGate", _NoGate)
    monkeypatch.setattr(
        sse_mod, "AsyncSession",
        lambda *a, **k: _FakeSession(resp=_FakeResp(frames)),
    )
    out = asyncio.run(
        conv.complete("gpt-6-pro", [{"role": "user", "content": "hi"}])
    )
    assert "PARSED-OK" in out
    assert "gpt-5-6-mini" in out  # the downgrade note names the real slug
    assert "gpt-6-pro" in out


# ── v0.0.18 cookie continuity ────────────────────────────────────────────────


class _CookieJar(dict):
    def set(self, k, v):
        self[k] = v


class _WarmSession:
    def __init__(self, cookies=None):
        self.cookies = _CookieJar(cookies or {})

    def close(self):
        pass


def test_persist_cookies_round_trips(tmp_path, monkeypatch) -> None:
    import gpt2agent.sim as sim_mod

    monkeypatch.setattr(sim_mod, "_STATE_PATH", tmp_path / "sim-state.json")
    p = _profile()
    p.session = _WarmSession({"__cf_bm": "rotated-abc"})
    p.persist_cookies()
    saved = json.loads((tmp_path / "sim-state.json").read_text())
    assert saved["cookies"]["__cf_bm"] == "rotated-abc"


def test_drop_session_clears_persisted_cookies(tmp_path, monkeypatch) -> None:
    import gpt2agent.sim as sim_mod

    monkeypatch.setattr(sim_mod, "_STATE_PATH", tmp_path / "sim-state.json")
    p = _profile()
    p.session = _WarmSession({"__cf_bm": "flagged"})
    p.persist_cookies()
    p.drop_session()
    saved = json.loads((tmp_path / "sim-state.json").read_text())
    assert saved["cookies"] == {}
    assert p.session is None


def test_persist_cookies_noop_without_session(tmp_path, monkeypatch) -> None:
    import gpt2agent.sim as sim_mod

    monkeypatch.setattr(sim_mod, "_STATE_PATH", tmp_path / "sim-state.json")
    p = _profile()
    p.session = None
    p.persist_cookies()  # must not raise or write a cookies key
    saved = json.loads((tmp_path / "sim-state.json").read_text())
    assert "cookies" not in saved


# ── connector wiring ──────────────────────────────────────────────────────────


def test_connectors_become_system_hints() -> None:
    from gpt2agent.sse import _build_dr_payload, _build_heavy_dr_payload

    pl = _build_payload(
        "gpt-5-6",
        [{"role": "user", "content": "hi"}],
        connectors=["connector_openai_pubmed", "connector:connector_gmail"],
    )
    assert pl["system_hints"] == [
        "connector:connector_openai_pubmed",
        "connector:connector_gmail",
    ]

    dr = _build_dr_payload("q", connectors=["connector_openai_pubmed"])
    assert dr["system_hints"] == ["connector:connector_openai_pubmed"]

    heavy = _build_heavy_dr_payload("q", connectors=["connector_openai_pubmed"])
    assert heavy["system_hints"][0] == "connector:connector_openai_deep_research"
    assert "connector:connector_openai_pubmed" in heavy["system_hints"]
    assert "connector:connector_openai_pubmed" in (
        heavy["messages"][0]["metadata"]["system_hints"]
    )


def test_github_repos_in_message_metadata() -> None:
    pl = _build_payload(
        "gpt-5-6",
        [{"role": "user", "content": "hi"}],
        github_repos=["robotlearning123/chatgpt2agent"],
    )
    md = pl["messages"][0]["metadata"]
    assert md["selected_github_repos"] == ["robotlearning123/chatgpt2agent"]
    assert md["selected_all_github_repos"] is False


# ── heavy-DR quota counter (distinct from generic deep_research) ──────────────


def test_heavy_dr_quota_ignores_generic_counter() -> None:
    # Verified live 2026-09-19: the DR connector dispatched fine with the
    # generic deep_research counter at 0 — it must not gate heavy DR.
    conv = ConversationClient(_CapBackend(_INIT))
    conv._limits_cache = (0, _INIT)
    remaining, _ = asyncio.run(conv._heavy_dr_quota())
    assert remaining is None


def test_heavy_dr_quota_reads_variant_counter() -> None:
    init = {
        "limits_progress": [
            {"feature_name": "deep_research", "remaining": 0},
            {"feature_name": "deep_research_standard", "remaining": 3,
             "reset_after": "2026-10-01T00:00:00Z"},
        ]
    }
    conv = ConversationClient(_CapBackend(init))
    conv._limits_cache = (0, init)
    remaining, reset = asyncio.run(conv._heavy_dr_quota())
    assert remaining == 3
    assert reset == "2026-10-01T00:00:00Z"


def test_heavy_dr_quota_blocked_variant() -> None:
    init = {
        "blocked_features": [
            {"name": "deep_research_standard",
             "resets_after": "2026-10-01T00:00:00Z"}
        ]
    }
    conv = ConversationClient(_CapBackend(init))
    conv._limits_cache = (0, init)
    remaining, reset = asyncio.run(conv._heavy_dr_quota())
    assert remaining == 0
    assert reset == "2026-10-01T00:00:00Z"


def test_heavy_dr_quota_malformed_entries_fail_open() -> None:
    # Non-dict entries, a malformed "remaining", and the generic counter at 0
    # must not raise or gate — heavy quota is simply unknown.
    init = {
        "limits_progress": [
            "garbage",
            {"feature_name": "deep_research", "remaining": 0},
            {"feature_name": "deep_research_standard", "remaining": "many"},
        ],
        "blocked_features": [None, {"name": "reason"}],
    }
    conv = ConversationClient(_CapBackend(init))
    conv._limits_cache = (0, init)
    remaining, _ = asyncio.run(conv._heavy_dr_quota())
    assert remaining is None


def test_profile_none_config_values_fall_back() -> None:
    # _DEFAULTS["sentinel"] ships explicit Nones — they must not override
    # the real defaults (None impersonate = bare TLS = CF 403s).
    from gpt2agent.server import _DEFAULTS

    prof = SimProfile(_DEFAULTS)
    assert prof.impersonate == IMPERSONATE
    assert prof.screen_w > 0
