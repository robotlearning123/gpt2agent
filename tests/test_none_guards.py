"""backend.get() returns None on an empty 2xx body (backend.py get()).

Every REST tool call site must survive that contract: read tools degrade to
empty results (matching the already-guarded list_conversations/get_file_info
sites), and the read-modify-write in custom_instructions_set must REFUSE to
proceed — blind-overwriting with `{}` would silently clear whichever custom
instructions field the caller did not supply.

Also covers load_config: a top-level scalar key in config.toml (a user
forgetting the [server] header) must raise a clean actionable error, not
`TypeError: 'bool' object is not iterable`.
"""

from __future__ import annotations

import asyncio

import pytest

from gpt2agent.errors import BackendContractError
from gpt2agent.tools import account, apps, codex, conversations, gpts, images
from gpt2agent.tools import instructions, memory, writes

from tests.test_tools import FakeClient, FakeMCP


class NoneClient(FakeClient):
    """FakeClient whose every GET returns None — the empty-2xx contract."""

    def get(self, path: str, target_path: str | None = None, **k):
        self.gets.append(path)
        return None


def _tools(module) -> FakeMCP:
    mcp = FakeMCP()
    module.register(mcp, NoneClient())
    return mcp


def _run(fn, *args, **kwargs):
    return asyncio.run(fn(*args, **kwargs))


# ── collection reads: blank 2xx is malformed, not an honest empty result ────


def test_memory_tools_reject_blank_2xx() -> None:
    mcp = _tools(memory)
    with pytest.raises(BackendContractError, match="memories list envelope"):
        _run(mcp.tools["memory_list"])
    with pytest.raises(BackendContractError, match="memories list envelope"):
        _run(mcp.tools["memory_search"], "anything")


def test_conversation_collection_tools_reject_blank_2xx() -> None:
    mcp = _tools(conversations)
    with pytest.raises(BackendContractError, match="items list envelope"):
        _run(mcp.tools["list_conversations"])
    with pytest.raises(BackendContractError, match="tasks list envelope"):
        _run(mcp.tools["list_tasks"])


def test_file_tools_reject_blank_2xx() -> None:
    mcp = _tools(images)
    with pytest.raises(BackendContractError, match="file object response required"):
        _run(mcp.tools["get_file_info"], "f1")
    with pytest.raises(BackendContractError, match="download object response required"):
        _run(mcp.tools["get_file_download_url"], "f1")


def test_custom_instructions_get_tolerates_none() -> None:
    mcp = _tools(instructions)
    out = _run(mcp.tools["custom_instructions_get"])
    assert out["about_user"] == ""
    assert out["enabled"] is None


def test_account_tools_reject_blank_2xx() -> None:
    mcp = _tools(account)
    with pytest.raises(BackendContractError, match="account_status"):
        _run(mcp.tools["account_status"])
    # The v0.0.12 model catalog distinguishes a malformed/empty-2xx envelope
    # from an honestly empty {"models": []} catalog.
    with pytest.raises(BackendContractError, match="models list envelope"):
        _run(mcp.tools["list_models"])


def test_list_custom_gpts_rejects_blank_2xx() -> None:
    with pytest.raises(BackendContractError, match="items list envelope"):
        _run(_tools(gpts).tools["list_custom_gpts"])


def test_list_apps_rejects_blank_2xx() -> None:
    with pytest.raises(BackendContractError, match="apps list envelope"):
        _run(_tools(apps).tools["list_apps"])


def test_codex_list_tools_reject_blank_2xx() -> None:
    mcp = _tools(codex)
    with pytest.raises(BackendContractError, match="environments list envelope"):
        _run(mcp.tools["list_codex_envs"])
    with pytest.raises(BackendContractError, match="tasks items list envelope"):
        _run(mcp.tools["list_codex_tasks"])


# ── write tool: None current state → refuse, do NOT clobber ─────────────────


def test_custom_instructions_set_refuses_on_none_current() -> None:
    client = NoneClient()
    mcp = FakeMCP()
    writes.register(mcp, client)
    with pytest.raises(RuntimeError, match="custom instructions"):
        _run(mcp.tools["custom_instructions_set"], about_user="new about-user text")
    assert client.posted == []  # nothing was overwritten


def test_custom_instructions_set_known_empty_current_still_works() -> None:
    # {} is a KNOWN-empty state (fresh account) — must still allow the write.
    client = FakeClient(routes={"/backend-api/user_system_messages": {}})
    mcp = FakeMCP()
    writes.register(mcp, client)
    _run(mcp.tools["custom_instructions_set"], about_user="hello")
    assert client.posted == [
        ("/backend-api/user_system_messages", {"about_user_message": "hello"})
    ]


def test_codex_task_create_env_lookup_tolerates_none() -> None:
    client = NoneClient()
    mcp = FakeMCP()
    writes.register(mcp, client)
    with pytest.raises(BackendContractError, match="environments list envelope"):
        _run(mcp.tools["codex_task_create"], repo_label="myrepo", prompt="do things")
    assert client.posted == []


# ── load_config: top-level scalar key → clean error, not TypeError ───────────


def test_load_config_top_level_scalar_raises_clean_error(tmp_path) -> None:
    from gpt2agent.server import load_config

    cfg = tmp_path / "config.toml"
    cfg.write_text('debug = true\n\n[server]\nport = 9001\n')
    with pytest.raises(ValueError, match="debug"):
        load_config(cfg)


def test_load_config_sections_still_merge(tmp_path) -> None:
    from gpt2agent.server import load_config

    cfg = tmp_path / "config.toml"
    cfg.write_text('[server]\nport = 9001\n')
    merged = load_config(cfg)
    assert merged["server"]["port"] == 9001
    assert "models" in merged  # defaults preserved
