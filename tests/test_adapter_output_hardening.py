from __future__ import annotations

import asyncio
import math

import pytest

from gpt2agent.errors import BackendContractError
from gpt2agent.tools import writes
from gpt2agent.tools.codex import normalize_codex_environments
from gpt2agent.tools.images import normalize_file_info
from gpt2agent.tools.memory import normalize_memories
from gpt2agent.tools.plugins import normalize_installed_plugins
from gpt2agent.tools.sites import normalize_sites_page
from tests.test_tools import FakeClient, FakeMCP


_MAX_ACCOUNT_INTEGER = (1 << 63) - 1


def test_codex_environment_redacts_every_private_string_projection() -> None:
    secret = "sk-" + "c" * 24

    result = normalize_codex_environments(
        {
            "environments": [
                {
                    "id": "env-1",
                    "label": f"repository-{secret}",
                    "workspace_dir": f"/workspace/{secret}",
                    "agent_network_access": secret,
                }
            ]
        }
    )

    assert secret not in repr(result)
    assert result[0] == {
        "id": "env-1",
        "label": "repository-<APIKEY>",
        "workspace_dir": "/workspace/<APIKEY>",
        "agent_network_access": "<APIKEY>",
        "repo_count": 0,
    }


def test_codex_environment_bounds_string_network_policy() -> None:
    with pytest.raises(BackendContractError, match="agent_network_access"):
        normalize_codex_environments(
            {"environments": [{"id": "env-1", "agent_network_access": "x" * 257}]}
        )


def test_memory_timestamp_redacts_secret_shaped_strings() -> None:
    secret = "sk-" + "m" * 24

    result = normalize_memories({"memories": [{"id": "memory-1", "created_timestamp": secret}]})

    assert result[0]["created_timestamp"] == "<APIKEY>"
    assert secret not in repr(result)


@pytest.mark.parametrize(
    "timestamp",
    [math.nan, math.inf, -math.inf, 1 << 63, -(1 << 63)],
)
def test_memory_timestamp_rejects_nonfinite_or_out_of_range_numbers(
    timestamp: int | float,
) -> None:
    with pytest.raises(BackendContractError, match="created_timestamp"):
        normalize_memories({"memories": [{"id": "memory-1", "created_timestamp": timestamp}]})


def test_file_info_redacts_timestamp_and_accepts_bounded_sizes() -> None:
    secret = "sk-" + "f" * 24

    result = normalize_file_info(
        {
            "id": "file-1",
            "size": _MAX_ACCOUNT_INTEGER,
            "file_size_bytes": _MAX_ACCOUNT_INTEGER,
            "creation_time": secret,
        },
        expected_id="file-1",
    )

    assert result["creation_time"] == "<APIKEY>"
    assert result["size"] == _MAX_ACCOUNT_INTEGER
    assert result["file_size_bytes"] == _MAX_ACCOUNT_INTEGER
    assert secret not in repr(result)


@pytest.mark.parametrize(
    "payload",
    [
        {"size": 1 << 63},
        {"file_size_bytes": 1 << 63},
        {"creation_time": math.nan},
        {"creation_time": math.inf},
    ],
)
def test_file_info_rejects_nonfinite_or_out_of_range_metadata(payload: dict) -> None:
    with pytest.raises(BackendContractError):
        normalize_file_info(
            {"id": "file-1", **payload},
            expected_id="file-1",
        )


def test_sites_redact_timestamp_and_accept_bounded_counts() -> None:
    secret = "sk-" + "s" * 24

    result = normalize_sites_page(
        {
            "items": [
                {
                    "id": "site-1",
                    "updated_at": secret,
                    "sharing": {
                        "user_count": _MAX_ACCOUNT_INTEGER,
                        "group_count": _MAX_ACCOUNT_INTEGER,
                    },
                }
            ],
            "cursor": None,
        }
    )

    assert result["items"][0]["updated_at"] == "<APIKEY>"
    assert result["items"][0]["sharing"] == {
        "access_mode": None,
        "user_count": _MAX_ACCOUNT_INTEGER,
        "group_count": _MAX_ACCOUNT_INTEGER,
    }
    assert secret not in repr(result)


@pytest.mark.parametrize("field", ["user_count", "group_count"])
def test_sites_reject_out_of_range_counts(field: str) -> None:
    with pytest.raises(BackendContractError, match=field):
        normalize_sites_page(
            {
                "items": [
                    {
                        "id": "site-1",
                        "sharing": {field: 1 << 63},
                    }
                ],
                "cursor": None,
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"plugins": [{"id": f"plugin-{index}"} for index in range(101)]},
        {
            "plugins": {
                "results": [{"id": f"plugin-{index}"} for index in range(101)],
                "page": {"has_more": False},
            }
        },
    ],
)
def test_installed_plugins_reject_unpageable_oversized_results(payload: dict) -> None:
    with pytest.raises(BackendContractError, match="100"):
        normalize_installed_plugins(payload)


def test_custom_instruction_write_replays_only_reviewed_fields() -> None:
    secret = "sk-" + "w" * 24
    current = {
        "enabled": True,
        "about_user_message": "keep user text",
        "about_model_message": "old model text",
        "traits_enabled": False,
        "personality_type_selection": "CHILL",
        "disabled_tools": ["tool-a"],
        "opaque": {"access_token": secret},
    }
    client = FakeClient(
        routes={"/backend-api/user_system_messages": current},
        posts={"/backend-api/user_system_messages": {"ok": True}},
    )
    mcp = FakeMCP()
    writes.register(mcp, client)

    result = asyncio.run(mcp.tools["custom_instructions_set"](about_model="new model text"))

    assert result == {"updated": True, "fields": ["about_model"]}
    assert client.posted == [
        (
            "/backend-api/user_system_messages",
            {
                "enabled": True,
                "about_user_message": "keep user text",
                "about_model_message": "new model text",
                "traits_enabled": False,
                "personality_type_selection": "CHILL",
                "disabled_tools": ["tool-a"],
            },
        )
    ]
    assert secret not in repr(client.posted)


def test_custom_instruction_write_rejects_opaque_reviewed_field() -> None:
    secret = "sk-" + "o" * 24
    client = FakeClient(
        routes={
            "/backend-api/user_system_messages": {
                "enabled": {"access_token": secret},
            }
        }
    )
    mcp = FakeMCP()
    writes.register(mcp, client)

    with pytest.raises(BackendContractError, match="enabled") as caught:
        asyncio.run(mcp.tools["custom_instructions_set"](about_user="new"))

    assert secret not in str(caught.value)
    assert client.posted == []
