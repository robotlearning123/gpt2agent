"""Unit tests for the MCP tool layer — no network.

Round-2 coverage began with the original 25-tool handler surface, which had
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

from gpt2agent.errors import BackendContractError, BackendHTTPError, backend_http_error
from gpt2agent.tools import account, apps, codex, conversations, gpts, images
from gpt2agent.tools import instructions, memory, tools_features, writes
from gpt2agent.tools._redact import redact


class FakeMCP:
    """Captures @mcp.tool()-decorated functions by name (decorator returns fn unchanged)."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a: Any, **k: Any):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
    """Canned GET responses (prefix-matched) + recorded POSTs."""

    def __init__(self, routes: dict | None = None, posts: dict | None = None) -> None:
        self.routes = routes or {}
        self.posts = posts or {}
        self.posted: list[tuple[str, Any]] = []
        self.gets: list[str] = []

    def request_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer TEST_ACCOUNT"}

    def get(self, path: str, target_path: str | None = None, **k: Any) -> Any:
        self.gets.append(path)
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

    async def image_gen(self, prompt, *, model="gpt-5-3", auth_headers=None):
        self.image_gen_calls.append(
            {"prompt": prompt, "model": model, "auth_headers": auth_headers}
        )
        return {"conversation_id": "c", "assets": [
            {"file_id": "f1", "asset_pointer": "sediment://f1", "width": 4, "height": 2}]}


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


def test_account_status_reuses_one_auth_snapshot_for_both_reads() -> None:
    class SnapshotClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(
                routes={
                    "/backend-api/me": {},
                    "/backend-api/accounts/check": {"accounts": {}},
                }
            )
            self.snapshot_calls = 0
            self.auth_headers: list[dict | None] = []

        def request_headers(self):
            self.snapshot_calls += 1
            return {"Authorization": "Bearer OPERATION_A"}

        def get(self, path: str, **kwargs: Any) -> Any:
            self.auth_headers.append(kwargs.get("auth_headers"))
            return super().get(path, **kwargs)

    client = SnapshotClient()

    _run(_reg(account, client).tools["account_status"])

    assert client.snapshot_calls == 1
    assert client.auth_headers == [
        {"Authorization": "Bearer OPERATION_A"},
        {"Authorization": "Bearer OPERATION_A"},
    ]


def test_list_models_exposes_slug() -> None:
    client = FakeClient(routes={"/backend-api/models": {"models": [
        {"slug": "gpt-5-5-pro", "title": "Pro", "max_tokens": 410000}]}})
    out = _run(_reg(account, client).tools["list_models"])
    assert out[0]["slug"] == "gpt-5-5-pro"


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


def test_list_apps_normalizes_string_ids_and_connected_fallback() -> None:
    client = FakeClient(routes={"/backend-api/apps/list": {"apps": [
        {"id": "connector_a", "enabled": True, "is_connected": True},
        {"id": "asdk_app_b", "connected": False},
        "not-a-dict"]}})
    out = _run(_reg(apps, client).tools["list_apps"])
    assert len(out) == 3
    assert out[0]["type"] == "official_connector"
    assert out[1]["connected"] is False  # `connected` fallback used
    assert out[2] == {
        "id": "not-a-dict",
        "type": "unknown",
        "enabled": None,
        "connected": None,
    }


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
        "/backend-api/files/f1/download": {
            "download_url": "https://downloads.example.com/x"
        },
        "/backend-api/files/f1": {"id": "f1", "name": "img.png"}})
    mcp = _reg(images, client, conv=object())
    assert _run(mcp.tools["get_file_info"], "f1")["name"] == "img.png"
    assert _run(mcp.tools["get_file_download_url"], "f1") == (
        "https://downloads.example.com/x"
    )
    # missing download_url -> ""
    c2 = FakeClient(routes={"/backend-api/files/f2/download": {}})
    assert _run(
        _reg(images, c2, conv=object()).tools["get_file_download_url"], "f2"
    ) == ""


def test_get_file_info_projects_only_bounded_contract_fields() -> None:
    secret = "SYNTHETIC-FILE-SECRET"
    client = FakeClient(
        routes={
            "/backend-api/files/f1": {
                "id": "f1",
                "name": "owner@example.com image.png",
                "size": 42,
                "file_size_bytes": 42,
                "use_case": "multimodal",
                "state": "ready",
                "creation_time": "2026-07-10T00:00:00Z",
                "mime_type": "image/png",
                "opaque": {"access_token": secret},
                "signed_url": f"https://private.invalid/?token={secret}",
            }
        }
    )

    result = _run(_reg(images, client, conv=object()).tools["get_file_info"], "f1")

    assert secret not in repr(result)
    assert result == {
        "id": "f1",
        "name": "<EMAIL> image.png",
        "size": 42,
        "file_size_bytes": 42,
        "use_case": "multimodal",
        "state": "ready",
        "creation_time": "2026-07-10T00:00:00Z",
        "mime_type": "image/png",
    }


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
    secret = "SYNTHETIC-CODEX-SECRET"
    client = FakeClient(
        posts={
            "/backend-api/codex/tasks": {
                "task": {"id": "task-1", "private": secret},
                "turn": {"turn_status": "queued"},
                "internal": {"access_token": secret},
            }
        }
    )
    fn = _reg(writes, client).tools["codex_task_create"]
    result = _run(
        fn, repo_label="ignored", prompt="do it", environment_id="envXYZ", branch="dev"
    )
    path, payload = client.posted[-1]
    assert path == "/backend-api/codex/tasks"
    assert payload == {
        "new_task": {"environment_id": "envXYZ", "branch": "dev"},
        "input_items": [{"type": "message", "role": "user",
                         "content": [{"content_type": "text", "text": "do it"}]}]}
    # env supplied -> no environments GET
    assert all("environments" not in g for g in client.gets)
    assert result == {"id": "task-1", "status": "queued"}
    assert secret not in repr(result)


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


@pytest.mark.parametrize("tool_name", ["custom_instructions_set", "codex_task_create"])
def test_compound_writes_reuse_one_auth_snapshot(tool_name: str) -> None:
    class SnapshotWriteClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(
                routes={
                    "/backend-api/user_system_messages": {},
                    "/backend-api/codex/environments": {
                        "environments": [{"id": "env-a", "label": "repo/a"}]
                    },
                },
                posts={
                    "/backend-api/user_system_messages": {"ok": True},
                    "/backend-api/codex/tasks": {"task": {"id": "task-1"}},
                },
            )
            self.snapshot_calls = 0
            self.auth_headers: list[dict | None] = []

        def request_headers(self) -> dict[str, str]:
            self.snapshot_calls += 1
            return {"Authorization": "Bearer OPERATION_A"}

        def get(self, path: str, **kwargs: Any) -> Any:
            self.auth_headers.append(kwargs.get("auth_headers"))
            return super().get(path, **kwargs)

        def post(self, path: str, **kwargs: Any) -> Any:
            self.auth_headers.append(kwargs.get("auth_headers"))
            return super().post(path, **kwargs)

    client = SnapshotWriteClient()
    fn = _reg(writes, client).tools[tool_name]

    if tool_name == "custom_instructions_set":
        _run(fn, about_model="new")
    else:
        _run(fn, repo_label="repo/a", prompt="do it")

    assert client.snapshot_calls == 1
    assert client.auth_headers == [
        {"Authorization": "Bearer OPERATION_A"},
        {"Authorization": "Bearer OPERATION_A"},
    ]


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
        "/backend-api/files/f1/download": {
            "download_url": "https://downloads.example.com/img",
            "file_name": "img.png",
        },
        "/backend-api/files/f1": {"name": "img.png"}})
    fn = _reg(images, client, conv).tools["generate_image"]
    out = asyncio.run(fn("a cat"))
    assert conv.image_gen_calls[-1]["prompt"] == "a cat"
    assert out["assets"][0]["download_url"] == (
        "https://downloads.example.com/img"
    )


def test_generate_image_does_not_echo_unexpected_exception_text() -> None:
    secret = "Bearer synthetic-private-download-secret"

    class FailingClient(FakeClient):
        def get(self, path: str, target_path: str | None = None, **k: Any) -> Any:
            raise RuntimeError(secret)

    fn = _reg(images, FailingClient(), _ToolConv()).tools["generate_image"]
    out = asyncio.run(fn("a cat"))

    assert secret not in repr(out)
    assert out["assets"][0]["download_error"] == "temporarily_failed"
    assert out["assets"][0]["info_error"] == "temporarily_failed"


def test_generate_image_distinguishes_contract_drift_from_temporary_failure() -> None:
    client = FakeClient(
        routes={
            "/backend-api/files/f1/download": {
                "download_url": "javascript:alert(1)",
            },
            "/backend-api/files/f1": {"id": "different-file"},
        }
    )
    fn = _reg(images, client, _ToolConv()).tools["generate_image"]

    out = asyncio.run(fn("a cat"))

    assert out["assets"][0]["download_error"] == "contract_changed"
    assert out["assets"][0]["info_error"] == "contract_changed"


def test_generate_image_preserves_typed_http_enrichment_status() -> None:
    class DeniedClient(FakeClient):
        def get(self, path: str, target_path: str | None = None, **kwargs: Any) -> Any:
            raise BackendHTTPError(
                "GET", path, 403, code="access_indeterminate", retryable=False
            )

    fn = _reg(images, DeniedClient(), _ToolConv()).tools["generate_image"]

    out = asyncio.run(fn("a cat"))

    assert out["assets"][0]["download_error"] == "access_indeterminate"
    assert out["assets"][0]["info_error"] == "access_indeterminate"


def test_generate_image_treats_backend_generated_file_reads_as_fixed_probes() -> None:
    class UnprocessableClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.fixed_probe: list[bool | None] = []

        def get(self, path: str, target_path: str | None = None, **kwargs: Any) -> Any:
            fixed_probe = kwargs.get("fixed_probe")
            self.fixed_probe.append(fixed_probe)
            raise backend_http_error(
                "GET",
                path,
                422,
                fixed_probe=bool(fixed_probe),
            )

    client = UnprocessableClient()
    fn = _reg(images, client, _ToolConv()).tools["generate_image"]

    out = asyncio.run(fn("a cat"))

    assert client.fixed_probe == [True, True]
    assert out["assets"][0]["download_error"] == "contract_changed"
    assert out["assets"][0]["info_error"] == "contract_changed"


@pytest.mark.parametrize(
    ("rejected_path", "error_field"),
    [
        ("/backend-api/files/f1/download", "download_error"),
        ("/backend-api/files/f1", "info_error"),
    ],
)
def test_generate_image_maps_backend_file_id_rejection_to_contract_changed(
    rejected_path: str,
    error_field: str,
) -> None:
    class RejectingClient(FakeClient):
        def get(self, path: str, target_path: str | None = None, **kwargs: Any) -> Any:
            self.gets.append(path)
            if path == rejected_path:
                raise BackendHTTPError(
                    "GET", path, 422, code="invalid_input", retryable=False
                )
            if path.endswith("/download"):
                return {
                    "download_url": "https://downloads.example.com/img",
                    "file_name": "",
                }
            return {"id": "f1", "name": "img.png"}

    fn = _reg(images, RejectingClient(), _ToolConv()).tools["generate_image"]

    out = asyncio.run(fn("a cat"))

    assert out["assets"][0][error_field] == "contract_changed"


def test_generate_image_rejects_backend_file_id_before_building_a_url() -> None:
    class MalformedConversation(_ToolConv):
        async def image_gen(self, prompt, *, model="gpt-5-3", auth_headers=None):
            return {
                "conversation_id": "c",
                "assets": [
                    {
                        "file_id": "../private",
                        "asset_pointer": "sediment://../private",
                    }
                ],
            }

    client = FakeClient()
    fn = _reg(images, client, MalformedConversation()).tools["generate_image"]

    with pytest.raises(BackendContractError):
        asyncio.run(fn("a cat"))
    assert client.gets == []


def test_generate_image_reuses_one_auth_snapshot_for_stream_and_file_reads() -> None:
    snapshot = {"Authorization": "Bearer OPERATION_A"}

    class SnapshotClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(
                routes={
                    "/backend-api/files/f1/download": {},
                    "/backend-api/files/f1": {"name": "img.png"},
                }
            )
            self.snapshot_calls = 0
            self.auth_headers: list[dict[str, str] | None] = []

        def request_headers(self) -> dict[str, str]:
            self.snapshot_calls += 1
            return snapshot

        def get(self, path: str, **kwargs: Any) -> Any:
            self.auth_headers.append(kwargs.get("auth_headers"))
            return super().get(path, **kwargs)

    class SnapshotConversation(_ToolConv):
        def __init__(self) -> None:
            super().__init__()
            self.auth_headers: dict[str, str] | None = None

        async def image_gen(self, prompt, *, model="gpt-5-3", auth_headers=None):
            self.auth_headers = auth_headers
            return await super().image_gen(prompt, model=model)

    client = SnapshotClient()
    conv = SnapshotConversation()
    fn = _reg(images, client, conv).tools["generate_image"]

    asyncio.run(fn("a cat"))

    assert client.snapshot_calls == 1
    assert conv.auth_headers is snapshot
    assert client.auth_headers == [snapshot, snapshot]


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
                       poll_async=False, thinking_effort=None, auth_headers=None):
        self.complete_calls.append({"model": model, "messages": messages,
                                    "temporary": temporary, "gizmo_id": gizmo_id,
                                    "poll_async": poll_async,
                                    "thinking_effort": thinking_effort,
                                    "auth_headers": auth_headers})
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
    import gpt2agent.model_catalog as model_catalog_mod
    import gpt2agent.sse as sse_mod
    from gpt2agent.server import build_server

    class _Catalog:
        def __init__(self) -> None:
            self.validations: list[tuple[str, str | None]] = []
            self.auth_snapshots: list[tuple[int, dict] | None] = []

        async def validate_general(self, model, thinking_effort, *, auth_snapshot=None):
            self.validations.append((model, thinking_effort))
            self.auth_snapshots.append(auth_snapshot)
            return {"slug": model}

        async def general(self):
            return []

        async def work(self):
            return []

    class _Backend:
        def auth_snapshot(self):
            return 7, {"Authorization": "Bearer OPERATION_ACCOUNT"}

    monkeypatch.setattr(backend_mod, "BackendClient", lambda *a, **k: _Backend())
    monkeypatch.setattr(sse_mod, "ConversationClient", lambda *a, **k: conv)
    catalog = _Catalog()
    monkeypatch.setattr(model_catalog_mod, "ModelCatalog", lambda *a, **k: catalog)
    mcp = build_server({"server": {"host": "127.0.0.1", "port": 9000},
                        "models": {"chat": "gpt-5-3", "agent": "agent-mode"}})
    conv.catalog = catalog
    return mcp._tool_manager._tools


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


def test_chat_validates_and_forwards_thinking_effort(monkeypatch) -> None:
    conv = _RecordConv()
    tools = _build_with_conv(monkeypatch, conv)

    asyncio.run(tools["chat"].fn("hi", "gpt-5-3", True, "high"))

    assert conv.catalog.validations[-1] == ("gpt-5-3", "high")
    assert conv.complete_calls[-1]["thinking_effort"] == "high"


def test_chat_model_validation_and_send_share_one_auth_snapshot(monkeypatch) -> None:
    conv = _RecordConv()
    tools = _build_with_conv(monkeypatch, conv)

    asyncio.run(tools["chat"].fn("hi"))

    generation, validation_headers = conv.catalog.auth_snapshots[-1]
    assert generation == 7
    assert validation_headers == {"Authorization": "Bearer OPERATION_ACCOUNT"}
    assert conv.complete_calls[-1]["auth_headers"] == validation_headers


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
        {"items": [{"url": "https://a.example.com", "title": "A"}, {"url": "https://a.example.com", "title": "A2"}]},
        {"items": [{"url": "https://b.example.com", "title": "B"}, {"title": "no-url"}]}]}]
    tools = _build_with_conv(monkeypatch, conv)
    out = asyncio.run(tools["deep_research"].fn("q", False))
    assert out.count("https://a.example.com") == 1  # deduped
    assert "https://b.example.com" in out
    assert "no-url" not in out  # missing url dropped


@pytest.mark.parametrize("tool_name", ["deep_research", "deep_research_heavy"])
def test_deep_research_sources_are_projected_without_secrets_or_markdown_injection(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    token_secret = "SYNTHETIC_CITATION_TOKEN_SECRET"
    userinfo_secret = "SYNTHETIC_CITATION_USERINFO_SECRET"
    fragment_secret = "SYNTHETIC_CITATION_FRAGMENT_SECRET"
    opaque_secret = "SYNTHETIC_CITATION_OPAQUE_SECRET"
    exception_secret = "SYNTHETIC_CITATION_EXCEPTION_SECRET"
    api_key = "sk-" + "a" * 24

    class _Opaque:
        def __str__(self) -> str:
            raise AssertionError(opaque_secret)

    refs = [
        {
            "items": [
                {
                    "url": (
                        "HTTPS://Example.COM:443/report?safe=1&utm_source=private"
                        f"&access_token={token_secret}#{fragment_secret}"
                    ),
                    "title": (
                        "Unsafe [source](https://evil.example) owner@example.com "
                        + api_key
                    ),
                },
                {
                    "url": "https://example.com/report?safe=1",
                    "title": "duplicate must not win",
                },
                {
                    "url": f"https://user:{userinfo_secret}@example.com/private",
                    "title": "credentialed URL",
                },
                {"url": "javascript:alert(1)", "title": "script"},
                {"url": "ftp://example.com/file", "title": "ftp"},
                {"url": _Opaque(), "title": _Opaque()},
                {
                    "url": "https://safe.example/exception-title",
                    "title": RuntimeError(exception_secret),
                },
                RuntimeError(exception_secret),
            ]
        },
        RuntimeError(exception_secret),
    ]
    conv = _RecordConv()
    event = {"type": "done", "text": "Body", "content_references": refs}
    if tool_name == "deep_research":
        conv.dr_events = [event]
    else:
        conv.heavy_events = [event]
    tools = _build_with_conv(monkeypatch, conv)

    out = asyncio.run(tools[tool_name].fn("q", False))

    assert out.count("https://example.com/report?safe=1") == 1
    assert "access_token" not in out
    assert "utm_source" not in out
    assert token_secret not in out
    assert userinfo_secret not in out
    assert fragment_secret not in out
    assert opaque_secret not in out
    assert exception_secret not in out
    assert "owner@example.com" not in out
    assert api_key not in out
    assert "javascript:" not in out
    assert "ftp://" not in out
    assert r"\[source\]\(https://evil.example\)" in out
    assert "https://safe.example/exception-title" in out
    assert "[Source]" in out


@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1:8000/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost:3000/secret",
        "http://[::1]/x",
        "https://10.0.0.1/x",
        "https://service.internal/x",
        "https://printer.local/x",
        "https://intranet/x",
        "https://public.example.com:8443/admin",
    ),
)
def test_deep_research_source_renderer_rejects_nonpublic_targets(url: str) -> None:
    from gpt2agent import server as server_mod

    refs = [{"items": [{"url": url, "title": "must be omitted"}]}]

    assert server_mod._render_dr_sources(refs) == ""


def test_deep_research_source_renderer_bounds_counts_types_and_lengths() -> None:
    from gpt2agent import server as server_mod

    items: list[object] = [
        None,
        RuntimeError("SYNTHETIC_RENDER_EXCEPTION_SECRET"),
        {"url": "https://example.com/" + "x" * 3_000, "title": "too long"},
        {"url": "https://bounded.example/first", "title": "T" * 1_000},
    ]
    items.extend(
        {"url": f"https://bounded.example/{index}", "title": f"Source {index}"}
        for index in range(200)
    )
    refs: list[object] = [None, {"items": "not-a-list"}, {"items": items}]

    rendered = server_mod._render_dr_sources(refs)

    assert rendered.count("\n- ") == server_mod._MAX_RENDERED_CITATIONS
    assert "T" * server_mod._MAX_CITATION_TITLE_LENGTH in rendered
    assert "T" * (server_mod._MAX_CITATION_TITLE_LENGTH + 1) not in rendered
    assert "too long" not in rendered
    assert "SYNTHETIC_RENDER_EXCEPTION_SECRET" not in rendered
    assert server_mod._render_dr_sources({"items": items}) == ""


@pytest.mark.parametrize(
    "query_name",
    [
        "access_token",
        "auth",
        "Authorization",
        "signature",
        "sig",
        "api_key",
        "key",
        "session_id",
        "password",
        "oauth_code",
        "X-Amz-Credential",
        "X-Amz-Signature",
        "utm_source",
    ],
)
def test_deep_research_source_renderer_strips_sensitive_query_fields(
    query_name: str,
) -> None:
    from gpt2agent import server as server_mod

    secret = "SYNTHETIC_QUERY_PARAMETER_SECRET"
    refs = [
        {
            "items": [
                {
                    "url": (
                        f"HTTP://Example.COM:80/report?safe=1&{query_name}={secret}"
                        "#SYNTHETIC_FRAGMENT_SECRET"
                    ),
                    "title": "Source",
                }
            ]
        }
    ]

    rendered = server_mod._render_dr_sources(refs)

    assert "http://example.com/report?safe=1" in rendered
    assert query_name not in rendered
    assert secret not in rendered
    assert "SYNTHETIC_FRAGMENT_SECRET" not in rendered


@pytest.mark.parametrize(
    "encoded_path",
    [
        "private/owner%40example.com",
        "private/sk%2Daaaaaaaaaaaaaaaaaaaaaaaa",
        "private/owner%2540example.com",
    ],
)
def test_deep_research_source_renderer_rejects_encoded_secrets_in_paths(
    encoded_path: str,
) -> None:
    from gpt2agent import server as server_mod

    refs = [
        {
            "items": [
                {
                    "url": f"https://example.com/{encoded_path}",
                    "title": "must be omitted",
                }
            ]
        }
    ]

    assert server_mod._render_dr_sources(refs) == ""


@pytest.mark.parametrize(
    "encoded_field",
    [
        "who=owner%2540example.com",
        "payload=sk%252Daaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ],
)
def test_deep_research_source_renderer_drops_double_encoded_query_values(
    encoded_field: str,
) -> None:
    from gpt2agent import server as server_mod

    refs = [
        {
            "items": [
                {
                    "url": f"https://example.com/report?safe=1&{encoded_field}",
                    "title": "Source",
                }
            ]
        }
    ]

    rendered = server_mod._render_dr_sources(refs)

    assert rendered == (
        "\n\n---\n**Sources:**\n"
        "- [Source](<https://example.com/report?safe=1>)"
    )


def test_deep_research_source_renderer_drops_double_encoded_query_keys() -> None:
    from gpt2agent import server as server_mod

    refs = [
        {
            "items": [
                {
                    "url": (
                        "https://example.com/report?safe=1&"
                        "access%255Ftoken=synthetic-value"
                    ),
                    "title": "Source",
                }
            ]
        }
    ]

    rendered = server_mod._render_dr_sources(refs)

    assert rendered == (
        "\n\n---\n**Sources:**\n"
        "- [Source](<https://example.com/report?safe=1>)"
    )


def test_deep_research_heavy_appends_connector_warning(monkeypatch) -> None:
    secret = "SYNTHETIC_MCP_TOOL_ERROR_SECRET account@example.com"
    conv = _RecordConv()
    conv.heavy_events = [{"type": "done", "text": "Body", "content_references": [],
                          "connector_failed": True},
                         {"type": "tool_error", "code": "connector_unavailable",
                          "message": secret}]
    tools = _build_with_conv(monkeypatch, conv)
    out = asyncio.run(tools["deep_research_heavy"].fn("q", False))
    assert "DR connector unavailable" in out
    assert "Server message" not in out
    assert secret not in out
