"""Unit tests for the MCP tool layer — no network.

Round-2 coverage: the tool handler surface (the product's actual value) had
zero direct unit tests. These exercise every `tools/*.py` handler through its
real `register(mcp, client, conv=None)` signature using a recording FakeMCP +
FakeClient, and the server.py SSE closures via FastMCP's tool manager with a
FakeConv. Invariants under test: PII redaction fires at each boundary, payload
shapes, and the `temporary=False` rule from CLAUDE.md.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gpt2agent.tools import account, apps, codex, conversations, gpts, images
from gpt2agent.tools import instructions, memory, tools_features, voice, writes
from gpt2agent.tools._redact import redact


class FakeMCP:
    """Captures @mcp.tool()-decorated functions by name (decorator returns fn unchanged)."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self.tool_options: dict[str, dict[str, Any]] = {}

    def tool(self, *a: Any, **k: Any):
        def deco(fn):
            self.tools[fn.__name__] = fn
            self.tool_options[fn.__name__] = dict(k)
            return fn
        return deco


class FakeClient:
    """Canned GET responses (prefix-matched) + recorded POSTs."""

    def __init__(self, routes: dict | None = None, posts: dict | None = None) -> None:
        self.routes = routes or {}
        self.posts = posts or {}
        self.posted: list[tuple[str, Any]] = []
        self.gets: list[str] = []
        self.get_calls: list[tuple[str, str | None]] = []

    def get(self, path: str, target_path: str | None = None, **k: Any) -> Any:
        self.gets.append(path)
        self.get_calls.append((path, target_path))
        for pat, val in self.routes.items():
            if path == pat or path.startswith(pat):
                return val
        return {}

    def post(self, path: str, json: Any = None, target_path: str | None = None, **k: Any) -> Any:
        self.posted.append((path, json))
        h = self.posts.get(path)
        if callable(h):
            return h(json)
        return h if h is not None else {}


def _reg(module, client: FakeClient, conv: Any = None) -> FakeMCP:
    mcp = FakeMCP()
    if module in (images, tools_features):
        module.register(mcp, client, conv)
    else:
        module.register(mcp, client)
    return mcp


def _run(fn, *args: Any, **kwargs: Any) -> Any:
    return asyncio.run(fn(*args, **kwargs))


class _ToolConv:
    """Records ConversationClient.tool_call / image_gen calls for the SSE-backed tools."""

    def __init__(self) -> None:
        self.tool_calls: list[dict] = []
        self.image_gen_calls: list[dict] = []

    async def tool_call(self, prompt, *, model="gpt-5-3", temporary=True):
        self.tool_calls.append({"prompt": prompt, "model": model, "temporary": temporary})
        return {"conversation_id": "c", "text": "ran", "tool_calls": [], "tool_responses": []}

    async def image_gen(self, prompt, *, model="gpt-5-3"):
        self.image_gen_calls.append({"prompt": prompt, "model": model})
        return {"conversation_id": "c", "assets": [
            {"file_id": "f1", "asset_pointer": "sediment://file-1", "width": 4, "height": 2}]}


# --------------------------------------------------------------------------- #
#  tools/_redact
# --------------------------------------------------------------------------- #


def test_redact_strips_email_and_phone() -> None:
    assert "<EMAIL>" in redact("mail me at a.b+c@example.co.uk")
    assert "@" not in redact("a@b.com")
    assert "<PHONE>" in redact("call +1 (415) 555-1212 now")
    assert redact(123) == 123  # non-str passthrough
    assert redact("plain text, nothing here") == "plain text, nothing here"


# --------------------------------------------------------------------------- #
#  tools/account
# --------------------------------------------------------------------------- #


def test_account_status_redacts_email_and_counts_features() -> None:
    client = FakeClient(routes={
        "/backend-api/me": {"email": "user@example.com", "country": "US", "groups": ["g"]},
        "/backend-api/accounts/check": {
            "accounts": {"a1": {
                "entitlement": {"subscription_plan": "plus", "has_active_subscription": True,
                                "expires_at": "2026"},
                "features": ["a", "b", "c"]}}},
    })
    out = _run(_reg(account, client).tools["account_status"])
    assert out["email"] == "<EMAIL>"
    assert out["subscription"] == "plus"
    assert out["has_active_subscription"] is True
    assert out["features_count"] == 3


def test_account_status_empty_accounts_no_crash() -> None:
    client = FakeClient(routes={
        "/backend-api/me": {"email": "x@y.com"},
        "/backend-api/accounts/check": {"accounts": {}},
    })
    out = _run(_reg(account, client).tools["account_status"])
    assert out["features_count"] == 0
    assert out["subscription"] is None


def test_list_models_exposes_slug() -> None:
    client = FakeClient(routes={"/backend-api/models": {"models": [
        {"slug": "gpt-5-5-pro", "title": "Pro", "max_tokens": 410000}]}})
    out = _run(_reg(account, client).tools["list_models"])
    assert out[0]["slug"] == "gpt-5-5-pro"


# --------------------------------------------------------------------------- #
#  tools/voice
# --------------------------------------------------------------------------- #


def _voice_item(
    voice_id: str = "fathom",
    *,
    name: str = "Arbor",
    description: str = "Easygoing and versatile",
    preview_url: str | None = "https://persistent.example.invalid/arbor.m4a",
) -> dict[str, Any]:
    return {
        "voice": voice_id,
        "name": name,
        "description": description,
        "preview_url": preview_url,
        "bloop_color": "#abcdef",
        "gain_db": None,
        "future_private_field": "must-not-escape",
    }


def test_list_voices_normalizes_live_shape_and_preserves_backend_ids() -> None:
    route = "/backend-api/settings/voices"
    client = FakeClient(routes={route: {
        "selected": "fathom",
        "voices": [
            _voice_item(),
            _voice_item(
                "glimmer",
                name="Sol",
                description="Savvy and relaxed",
                preview_url=None,
            ),
        ],
    }})

    mcp = _reg(voice, client)
    out = _run(mcp.tools["list_voices"])

    assert out == [
        {
            "id": "fathom",
            "name": "Arbor",
            "description": "Easygoing and versatile",
            "selected": True,
            "has_preview": True,
        },
        {
            "id": "glimmer",
            "name": "Sol",
            "description": "Savvy and relaxed",
            "selected": False,
            "has_preview": False,
        },
    ]
    assert client.get_calls == [(route, route)]
    rendered = repr(out)
    assert "persistent.example.invalid" not in rendered
    assert "#abcdef" not in rendered
    assert "gain_db" not in rendered
    assert "future_private_field" not in rendered
    # These IDs deliberately differ from their display names. The adapter must
    # never derive an identifier by lower-casing the name.
    assert [item["id"] for item in out] == ["fathom", "glimmer"]


def test_list_voices_redacts_display_text() -> None:
    secret = "sk-ABCDEFGHIJKLMNOPQRST"
    client = FakeClient(routes={"/backend-api/settings/voices": {
        "selected": "safe-id",
        "voices": [_voice_item(
            "safe-id",
            name="Contact voice@example.com",
            description=f"Call +1 (415) 555-1212 with {secret}",
        )],
    }})

    out = _run(_reg(voice, client).tools["list_voices"])

    rendered = repr(out)
    assert "voice@example.com" not in rendered
    assert "415" not in rendered
    assert secret not in rendered
    assert "<EMAIL>" in out[0]["name"]
    assert "<PHONE>" in out[0]["description"]
    assert "<APIKEY>" in out[0]["description"]


def test_list_voices_empty_catalog_is_not_contract_drift() -> None:
    client = FakeClient(routes={"/backend-api/settings/voices": {
        "selected": None,
        "voices": [],
    }})
    assert _run(_reg(voice, client).tools["list_voices"]) == []


def test_list_voices_accepts_catalog_at_documented_bound() -> None:
    items = [_voice_item(f"voice-{index}") for index in range(128)]
    client = FakeClient(routes={"/backend-api/settings/voices": {
        "selected": "voice-0",
        "voices": items,
    }})

    out = _run(_reg(voice, client).tools["list_voices"])

    assert len(out) == 128
    assert out[0]["selected"] is True


def test_list_voices_rejects_catalog_above_documented_bound() -> None:
    items = [_voice_item(f"voice-{index}") for index in range(129)]
    client = FakeClient(routes={"/backend-api/settings/voices": {
        "selected": "voice-0",
        "voices": items,
    }})

    with pytest.raises(RuntimeError, match="^voice catalog contract changed$"):
        _run(_reg(voice, client).tools["list_voices"])


def test_list_voices_default_sends_no_voice_mode() -> None:
    route = "/backend-api/settings/voices"
    client = FakeClient(routes={route: {"selected": None, "voices": [_voice_item()]}})

    _run(_reg(voice, client).tools["list_voices"])

    # Default preserves the bare route — no query string appended.
    assert client.get_calls == [(route, route)]


@pytest.mark.parametrize("mode", ["standard", "advanced", "wingman"])
def test_list_voices_passes_observed_voice_mode(mode: str) -> None:
    # Values accepted by the live /backend-api/settings/voices route on
    # 2026-07-11. The bare route (target_path) is unchanged; the mode rides the
    # query string. GPT-Live audio uses a separate session contract.
    route = "/backend-api/settings/voices"
    client = FakeClient(routes={route: {
        "selected": "cove",
        "voices": [_voice_item("cove", name="Breeze", description="Animated and earnest")],
    }})

    out = _run(_reg(voice, client).tools["list_voices"], voice_mode=mode)

    assert client.get_calls == [(f"{route}?voice_mode={mode}", route)]
    assert out == [{
        "id": "cove",
        "name": "Breeze",
        "description": "Animated and earnest",
        "selected": True,
        "has_preview": True,
    }]


def test_list_voices_forwards_a_bounded_future_mode_without_hard_coding() -> None:
    route = "/backend-api/settings/voices"
    client = FakeClient(routes={route: {"selected": None, "voices": []}})

    out = _run(_reg(voice, client).tools["list_voices"], voice_mode="future_mode")

    assert out == []
    assert client.get_calls == [(f"{route}?voice_mode=future_mode", route)]


@pytest.mark.parametrize(
    "bad",
    ["", " ", "Advanced", "a b", "../secret", "x?y=z", "voice_mode=1", "m" * 33],
)
def test_list_voices_rejects_malformed_voice_mode(bad: str) -> None:
    route = "/backend-api/settings/voices"
    client = FakeClient(routes={route: {"selected": None, "voices": [_voice_item()]}})

    with pytest.raises(ValueError) as exc:
        _run(_reg(voice, client).tools["list_voices"], voice_mode=bad)

    message = str(exc.value)
    assert "voice_mode" in message
    # Payload-free: a meaningful rejected value is never echoed back into the
    # error (whitespace-only inputs trivially appear in ordinary spacing).
    assert not bad.strip() or bad not in message
    # A malformed mode must be rejected before any backend call.
    assert client.get_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"voices": {}},
        {"voices": [None]},
        {"voices": [{"voice": "", "name": "Name", "description": "Desc"}]},
        {"voices": [{"voice": "   ", "name": "Name", "description": "Desc"}]},
        {"voices": [{"voice": "bad\n", "name": "Name", "description": "Desc"}]},
        {"voices": [{"voice": "v" * 129, "name": "Name", "description": "Desc"}]},
        {"voices": [{"voice": "v", "name": 3, "description": "Desc"}]},
        {"voices": [{"voice": "v", "name": "n" * 257, "description": "Desc"}]},
        {"voices": [{"voice": "v", "name": "Name", "description": "d" * 2_001}]},
        {"voices": [{"voice": "v", "name": "Name", "description": "Desc",
                      "preview_url": 3}]},
        {"voices": [_voice_item("same"), _voice_item("same")]},
    ],
)
def test_list_voices_fails_closed_on_contract_drift(payload: Any) -> None:
    client = FakeClient(routes={"/backend-api/settings/voices": payload})

    with pytest.raises(RuntimeError, match="^voice catalog contract changed$") as exc:
        _run(_reg(voice, client).tools["list_voices"])

    assert repr(payload) not in str(exc.value)


@pytest.mark.parametrize("selected", [None, 3, "missing-id"])
def test_list_voices_reports_unknown_selection_as_none(selected: Any) -> None:
    client = FakeClient(routes={"/backend-api/settings/voices": {
        "selected": selected,
        "voices": [_voice_item("voice-id")],
    }})

    out = _run(_reg(voice, client).tools["list_voices"])

    assert out[0]["selected"] is None


def test_list_voices_has_read_only_mcp_annotations() -> None:
    mcp = _reg(voice, FakeClient())
    annotations = mcp.tool_options["list_voices"]["annotations"]

    assert annotations.readOnlyHint is True
    assert annotations.destructiveHint is False
    assert annotations.idempotentHint is True
    assert annotations.openWorldHint is True


# --------------------------------------------------------------------------- #
#  tools/memory
# --------------------------------------------------------------------------- #


def test_memory_list_and_search_redact_and_filter() -> None:
    client = FakeClient(routes={"/backend-api/memories": {"memories": [
        {"id": "m1", "content": "reach jane@doe.com", "created_timestamp": 1},
        {"id": "m2", "content": "weather note", "created_timestamp": 2}]}})
    mcp = _reg(memory, client)
    listed = _run(mcp.tools["memory_list"])
    assert len(listed) == 2
    assert "<EMAIL>" in listed[0]["content"]
    hits = _run(mcp.tools["memory_search"], "WEATHER")  # case-insensitive
    assert [m["id"] for m in hits] == ["m2"]
    assert _run(mcp.tools["memory_search"], "zzz") == []


# --------------------------------------------------------------------------- #
#  tools/instructions
# --------------------------------------------------------------------------- #


def test_custom_instructions_get_redacts_both_fields() -> None:
    client = FakeClient(routes={"/backend-api/user_system_messages": {
        "enabled": True, "traits_enabled": False, "personality_type_selection": "CHILL",
        "about_user_message": "I am bob@corp.io", "about_model_message": "be terse"}})
    out = _run(_reg(instructions, client).tools["custom_instructions_get"])
    assert out["about_user"] == "I am <EMAIL>"  # only the email substring is masked
    assert "bob@corp.io" not in out["about_user"]
    assert out["about_model"] == "be terse"
    assert out["personality_type"] == "CHILL"
    assert out["enabled"] is True


# --------------------------------------------------------------------------- #
#  tools/conversations
# --------------------------------------------------------------------------- #


def test_list_conversations_redacts_title() -> None:
    client = FakeClient(routes={"/backend-api/conversations": {"items": [
        {"id": "c1", "title": "ping admin@site.com", "update_time": 1}]}})
    out = _run(_reg(conversations, client).tools["list_conversations"])
    assert out[0]["id"] == "c1"
    assert "<EMAIL>" in out[0]["title"]


def test_list_tasks_redacts_and_bool_coerces_and_slices() -> None:
    items = [{"task_id": f"t{i}", "title": "see ceo@x.com", "prompt": "call 415-555-0000",
              "image_gen_message": {"foo": 1}} for i in range(25)]
    client = FakeClient(routes={"/backend-api/tasks": {"tasks": items}})
    out = _run(_reg(conversations, client).tools["list_tasks"], limit=10)
    assert len(out) == 10
    assert "<EMAIL>" in out[0]["title"]
    assert "<PHONE>" in out[0]["prompt"]
    assert out[0]["image_gen_message"] is True  # dict -> bool coercion


def test_get_conversation_filters_roles_truncates_and_extracts_images() -> None:
    mapping = {
        "n0": {"message": {"id": "s", "author": {"role": "system"},
                           "content": {"content_type": "text", "parts": ["sys"]}}},
        "n1": {"message": {"id": "u", "author": {"role": "user"},
                           "content": {"content_type": "text", "parts": ["x" * 3000]}}},
        "n2": {"message": {"id": "a", "author": {"role": "assistant"},
                           "content": {"content_type": "code", "parts": ["y" * 900]}}},
        "n3": {"message": {"id": "m", "author": {"role": "assistant"},
                           "content": {"content_type": "multimodal_text", "parts": [
                               {"content_type": "image_asset_pointer",
                                "asset_pointer": "sediment://file-1", "width": 4, "height": 2}]}}},
    }
    client = FakeClient(routes={"/backend-api/conversation/C1": {"id": "C1", "title": "t",
                                                                 "mapping": mapping}})
    out = _run(_reg(conversations, client).tools["get_conversation"], "C1")
    roles = {m["role"] for m in out["messages"]}
    assert "system" not in roles  # filtered
    by_id = {m["id"]: m for m in out["messages"]}
    assert len(by_id["u"]["text"]) == 2000  # text truncation
    assert len(by_id["a"]["code"]) == 500   # code truncation
    assert by_id["m"]["images"][0]["asset_pointer"] == "sediment://file-1"


def test_get_conversation_empty_returns_empty_dict() -> None:
    client = FakeClient(routes={"/backend-api/conversation/X": {}})
    assert _run(_reg(conversations, client).tools["get_conversation"], "X") == {}


def test_get_conversation_redacts_message_bodies() -> None:
    """Message text/code must go through redact() like title does — a pasted
    secret or email in a chat body must not be echoed back to the MCP client."""
    jwt = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
    mapping = {
        "n1": {"message": {"id": "u", "author": {"role": "user"},
                           "content": {"content_type": "text",
                                       "parts": [f"my key {jwt} mail bob@corp.io"]}}},
        "n2": {"message": {"id": "a", "author": {"role": "assistant"},
                           "content": {"content_type": "code",
                                       "parts": [f"token = '{jwt}'"]}}},
    }
    client = FakeClient(routes={"/backend-api/conversation/C2": {"id": "C2", "title": "t",
                                                                 "mapping": mapping}})
    out = _run(_reg(conversations, client).tools["get_conversation"], "C2")
    by_id = {m["id"]: m for m in out["messages"]}
    assert jwt not in by_id["u"]["text"] and "<JWT>" in by_id["u"]["text"]
    assert "bob@corp.io" not in by_id["u"]["text"] and "<EMAIL>" in by_id["u"]["text"]
    assert jwt not in by_id["a"]["code"] and "<JWT>" in by_id["a"]["code"]


def test_get_conversation_redacts_before_truncation() -> None:
    """A secret straddling the 2000-char cut must not survive as a partial token."""
    jwt = "eyJ" + "a" * 40 + "." + "b" * 40 + "." + "c" * 40
    body = "x" * 1990 + jwt  # JWT starts before the cut, ends after it
    mapping = {
        "n1": {"message": {"id": "u", "author": {"role": "user"},
                           "content": {"content_type": "text", "parts": [body]}}},
    }
    client = FakeClient(routes={"/backend-api/conversation/C3": {"id": "C3", "title": "t",
                                                                 "mapping": mapping}})
    out = _run(_reg(conversations, client).tools["get_conversation"], "C3")
    text = out["messages"][0]["text"]
    assert "eyJa" not in text  # no partial-JWT prefix leaked
    assert text.endswith("<JWT>") or "<JWT>" in text


# --------------------------------------------------------------------------- #
#  tools/gpts, apps, codex
# --------------------------------------------------------------------------- #


def test_list_custom_gpts_redacts_name_keeps_short_url() -> None:
    client = FakeClient(routes={"/backend-api/gizmos/snorlax/sidebar": {"items": [
        {"gizmo": {"name": "reach ops@gpt.io", "short_url": "g/x"}}]}})
    out = _run(_reg(gpts, client).tools["list_custom_gpts"])
    assert "<EMAIL>" in out[0]["name"]
    assert out[0]["short_url"] == "g/x"


def test_apps_classify() -> None:
    assert apps._classify("connector_x") == "official_connector"
    assert apps._classify("asdk_app_42") == "third_party_sdk"
    assert apps._classify("weird") == "unknown"
    assert apps._classify("") == "unknown"


def test_list_apps_filters_nondict_and_connected_fallback() -> None:
    client = FakeClient(routes={"/backend-api/apps/list": {"apps": [
        {"id": "connector_a", "enabled": True, "is_connected": True},
        {"id": "asdk_app_b", "connected": False},
        "not-a-dict"]}})
    out = _run(_reg(apps, client).tools["list_apps"])
    assert len(out) == 2  # string filtered out
    assert out[0]["type"] == "official_connector"
    assert out[1]["connected"] is False  # `connected` fallback used


def test_list_codex_envs_repo_count_and_envelope() -> None:
    # list envelope
    c1 = FakeClient(routes={"/backend-api/codex/environments": [{"id": "e1", "label": "a/b",
                                                                 "repos": ["r1", "r2"]}]})
    out1 = _run(_reg(codex, c1).tools["list_codex_envs"])
    assert out1[0]["repo_count"] == 2 and out1[0]["label"] == "a/b"
    # dict envelope
    c2 = FakeClient(routes={"/backend-api/codex/environments": {"environments": [
        {"id": "e2", "repos": []}]}})
    assert _run(_reg(codex, c2).tools["list_codex_envs"])[0]["repo_count"] == 0


def test_list_codex_tasks_unwraps_redacts_and_truncates() -> None:
    items = [{"task": {"id": "t1", "title": "a@b.com"}, "turn": {"turn_status": "done"}},
             {"id": "t2", "title": "plain"}]
    client = FakeClient(routes={"/backend-api/codex/tasks": {"items": items}})
    out = _run(_reg(codex, client).tools["list_codex_tasks"], limit=1)
    assert len(out) == 1  # truncated to limit
    assert out[0]["id"] == "t1"
    assert "<EMAIL>" in out[0]["title"]
    assert out[0]["status"] == "done"


# --------------------------------------------------------------------------- #
#  tools/images (sync passthroughs)
# --------------------------------------------------------------------------- #


def test_get_file_info_and_download_url() -> None:
    client = FakeClient(routes={
        "/backend-api/files/f1/download": {"download_url": "https://dl/x"},
        "/backend-api/files/f1": {"id": "f1", "name": "img.png"}})
    mcp = _reg(images, client, conv=object())
    assert _run(mcp.tools["get_file_info"], "f1")["name"] == "img.png"
    assert _run(mcp.tools["get_file_download_url"], "f1") == "https://dl/x"
    # missing download_url -> ""
    c2 = FakeClient(routes={"/backend-api/files/f2/download": {}})
    assert _run(
        _reg(images, c2, conv=object()).tools["get_file_download_url"], "f2"
    ) == ""


# --------------------------------------------------------------------------- #
#  tools/writes
# --------------------------------------------------------------------------- #


def test_codex_task_create_raises_on_missing_label() -> None:
    client = FakeClient(routes={"/backend-api/codex/environments": {"environments": [
        {"id": "e1", "label": "a/b"}]}})
    fn = _reg(writes, client).tools["codex_task_create"]
    with pytest.raises(ValueError, match="No Codex environment"):
        _run(fn, repo_label="nope/x", prompt="hi")


def test_codex_task_create_raises_on_ambiguous_label() -> None:
    client = FakeClient(routes={"/backend-api/codex/environments": {"environments": [
        {"id": "e1", "label": "a/b"}, {"id": "e2", "label": "a/b"}]}})
    fn = _reg(writes, client).tools["codex_task_create"]
    with pytest.raises(ValueError, match="Ambiguous"):
        _run(fn, repo_label="a/b", prompt="hi")


def test_codex_task_create_builds_payload_shape() -> None:
    client = FakeClient(posts={"/backend-api/codex/tasks": {"ok": True}})
    fn = _reg(writes, client).tools["codex_task_create"]
    _run(fn, repo_label="ignored", prompt="do it", environment_id="envXYZ", branch="dev")
    path, payload = client.posted[-1]
    assert path == "/backend-api/codex/tasks"
    assert payload == {
        "new_task": {"environment_id": "envXYZ", "branch": "dev"},
        "input_items": [{"type": "message", "role": "user",
                         "content": [{"content_type": "text", "text": "do it"}]}]}
    # env supplied -> no environments GET
    assert all("environments" not in g for g in client.gets)


def test_custom_instructions_set_preserves_unsupplied_fields() -> None:
    current = {"enabled": True, "about_user_message": "keep me",
               "about_model_message": "old", "traits_enabled": False, "disabled_tools": ["t"]}
    client = FakeClient(routes={"/backend-api/user_system_messages": current},
                        posts={"/backend-api/user_system_messages": lambda p: p})
    fn = _reg(writes, client).tools["custom_instructions_set"]
    _run(fn, about_model="new")
    _, payload = client.posted[-1]
    assert payload["about_user_message"] == "keep me"  # preserved
    assert payload["enabled"] is True
    assert payload["disabled_tools"] == ["t"]
    assert payload["about_model_message"] == "new"  # overridden


# --------------------------------------------------------------------------- #
#  tools/tools_features + images.generate_image (SSE-backed; temporary=False)
# --------------------------------------------------------------------------- #


def test_code_interpreter_runs_temporary_false() -> None:
    conv = _ToolConv()
    fn = _reg(tools_features, FakeClient(), conv).tools["code_interpreter"]
    out = asyncio.run(fn("print(1)"))
    assert conv.tool_calls[-1]["temporary"] is False
    assert conv.tool_calls[-1]["prompt"] == "print(1)"
    assert out["text"] == "ran"


def test_canvas_execute_temporary_false_and_prefixes_prompt() -> None:
    conv = _ToolConv()
    fn = _reg(tools_features, FakeClient(), conv).tools["canvas_execute"]
    asyncio.run(fn("make a chart"))
    assert conv.tool_calls[-1]["temporary"] is False
    assert conv.tool_calls[-1]["prompt"].startswith("Use Canvas to:")


def test_generate_image_enriches_assets_with_download_url() -> None:
    conv = _ToolConv()
    client = FakeClient(routes={
        "/backend-api/files/f1/download": {"download_url": "https://dl/img", "file_name": "img.png"},
        "/backend-api/files/f1": {"name": "img.png"}})
    fn = _reg(images, client, conv).tools["generate_image"]
    out = asyncio.run(fn("a cat"))
    assert conv.image_gen_calls[-1]["prompt"] == "a cat"
    assert out["assets"][0]["download_url"] == "https://dl/img"


# --------------------------------------------------------------------------- #
#  server.py SSE handlers (temporary-flag + prompt invariants)
# --------------------------------------------------------------------------- #


class _RecordConv:
    def __init__(self) -> None:
        self.complete_calls: list[dict] = []
        self.reply = "ok"
        self.dr_events: list[dict] = []
        self.heavy_events: list[dict] = []

    async def complete(self, model, messages, *, temporary=True, gizmo_id=None,
                       poll_async=False):
        self.complete_calls.append({"model": model, "messages": messages,
                                    "temporary": temporary, "gizmo_id": gizmo_id,
                                    "poll_async": poll_async})
        return self.reply

    def deep_research(self, q):
        self._last_dr_query = q
        async def gen():
            for e in self.dr_events:
                yield e
        return gen()

    def deep_research_heavy(self, q, model=None):
        self._last_heavy_query = q
        async def gen():
            for e in self.heavy_events:
                yield e
        return gen()


def _build_with_conv(monkeypatch, conv):
    import gpt2agent.backend as backend_mod
    import gpt2agent.sse as sse_mod
    from gpt2agent.server import build_server

    monkeypatch.setattr(backend_mod, "BackendClient", lambda *a, **k: object())
    monkeypatch.setattr(sse_mod, "ConversationClient", lambda *a, **k: conv)
    mcp = build_server({"server": {"host": "127.0.0.1", "port": 9000},
                        "models": {"chat": "gpt-5-3", "agent": "agent-mode"}})
    return mcp._tool_manager._tools


def test_server_registers_exact_30_tool_surface_and_voice_once(monkeypatch) -> None:
    calls = 0
    original_register = voice.register

    def counted_register(mcp, client):
        nonlocal calls
        calls += 1
        return original_register(mcp, client)

    monkeypatch.setattr(voice, "register", counted_register)
    tools = _build_with_conv(monkeypatch, _RecordConv())

    assert set(tools) == {
        "account_status",
        "agent",
        "canvas_execute",
        "chat",
        "code_interpreter",
        "codex_task_create",
        "custom_instructions_get",
        "custom_instructions_set",
        "deep_research",
        "deep_research_heavy",
        "generate_image",
        "get_conversation",
        "get_file_download_url",
        "get_file_info",
        "gpt_chat",
        "list_apps",
        "list_codex_envs",
        "list_codex_tasks",
        "list_conversations",
        "list_custom_gpts",
        "list_models",
        "list_tasks",
        "list_voices",
        "memory_create_via_chat",
        "memory_list",
        "memory_search",
        # GPT-Live → coding-agent bridge, observe-only (human → agent; no audio on MCP):
        "voice_live_end",
        "voice_live_export_help",
        "voice_live_get_transcript",
        "voice_live_status",
    }
    assert len(tools) == 30
    assert calls == 1

    annotations = tools["list_voices"].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is True
    assert annotations.destructiveHint is False
    assert annotations.idempotentHint is True
    assert annotations.openWorldHint is True

    live_help = tools["voice_live_export_help"].annotations
    assert live_help is not None
    assert live_help.readOnlyHint is True


def test_agent_always_temporary_false(monkeypatch) -> None:
    conv = _RecordConv()
    tools = _build_with_conv(monkeypatch, conv)
    asyncio.run(tools["agent"].fn("do stuff"))
    assert conv.complete_calls[-1]["temporary"] is False
    assert conv.complete_calls[-1]["model"] == "agent-mode"
    # Agent mode must opt into async polling; plain chat must not (else an empty
    # chat response would hang on the 5-min poll window).
    assert conv.complete_calls[-1]["poll_async"] is True


def test_chat_does_not_poll_async(monkeypatch) -> None:
    conv = _RecordConv()
    tools = _build_with_conv(monkeypatch, conv)
    asyncio.run(tools["chat"].fn("hi"))
    assert conv.complete_calls[-1]["poll_async"] is False


def test_gpt_chat_temporary_false_and_passes_gizmo(monkeypatch) -> None:
    conv = _RecordConv()
    tools = _build_with_conv(monkeypatch, conv)
    asyncio.run(tools["gpt_chat"].fn("g/abc", "hello"))
    call = conv.complete_calls[-1]
    assert call["temporary"] is False
    assert call["gizmo_id"] == "g/abc"


def test_memory_create_via_chat_temporary_false_and_wraps_prompt(monkeypatch) -> None:
    conv = _RecordConv()
    tools = _build_with_conv(monkeypatch, conv)
    asyncio.run(tools["memory_create_via_chat"].fn("SECRET-XYZ"))
    call = conv.complete_calls[-1]
    assert call["temporary"] is False
    sent = call["messages"][0]["content"]
    assert sent.startswith("Please commit the following to memory verbatim")
    assert sent.endswith("SECRET-XYZ")


def test_chat_forwards_temporary_and_no_response_fallback(monkeypatch) -> None:
    conv = _RecordConv()
    conv.reply = ""  # empty -> "(no response)"
    tools = _build_with_conv(monkeypatch, conv)
    out = asyncio.run(tools["chat"].fn("hi", "gpt-5-3", False))
    assert conv.complete_calls[-1]["temporary"] is False
    assert out == "(no response)"


def test_deep_research_imperative_prefix_toggle(monkeypatch) -> None:
    conv = _RecordConv()
    conv.dr_events = [{"type": "done", "text": "R", "content_references": []}]
    tools = _build_with_conv(monkeypatch, conv)
    asyncio.run(tools["deep_research"].fn("topic", True))
    assert conv._last_dr_query.startswith("Begin the deep research immediately")
    asyncio.run(tools["deep_research"].fn("topic", False))
    assert conv._last_dr_query == "topic"


def test_deep_research_dedupes_sources(monkeypatch) -> None:
    conv = _RecordConv()
    conv.dr_events = [{"type": "done", "text": "Body", "content_references": [
        {"items": [{"url": "https://a", "title": "A"}, {"url": "https://a", "title": "A2"}]},
        {"items": [{"url": "https://b", "title": "B"}, {"title": "no-url"}]}]}]
    tools = _build_with_conv(monkeypatch, conv)
    out = asyncio.run(tools["deep_research"].fn("q", False))
    assert out.count("https://a") == 1  # deduped
    assert "https://b" in out
    assert "no-url" not in out  # missing url dropped


def test_deep_research_heavy_appends_connector_warning(monkeypatch) -> None:
    conv = _RecordConv()
    conv.heavy_events = [{"type": "done", "text": "Body", "content_references": [],
                          "connector_failed": True},
                         {"type": "tool_error", "message": "boom line\nsecond"}]
    tools = _build_with_conv(monkeypatch, conv)
    out = asyncio.run(tools["deep_research_heavy"].fn("q", False))
    assert "DR connector unavailable" in out
    assert "boom line" in out
