from __future__ import annotations

import asyncio

import pytest

from gpt2agent.errors import BackendContractError
from gpt2agent.tools import account, apps, automations, conversations
from tests.test_tools import FakeClient, FakeMCP


def _run(fn, *args, **kwargs):
    return asyncio.run(fn(*args, **kwargs))


def _registered(module, client: FakeClient) -> FakeMCP:
    mcp = FakeMCP()
    module.register(mcp, client)
    return mcp


def test_account_status_projects_only_bounded_redacted_contract_fields() -> None:
    secret = "sk-" + "b" * 24
    result = account.normalize_account_status(
        {
            "email": "owner@example.com",
            "country": secret,
            "groups": [secret],
            "opaque": {"access_token": secret},
        },
        {
            "accounts": {
                "account-1": {
                    "entitlement": {
                        "subscription_plan": secret,
                        "has_active_subscription": True,
                        "expires_at": secret,
                        "opaque": {"access_token": secret},
                    },
                    "features": ["one", "two"],
                    "opaque": {"access_token": secret},
                }
            },
            "opaque": {"access_token": secret},
        },
    )

    assert secret not in repr(result)
    assert result == {
        "email": "<EMAIL>",
        "country": "<APIKEY>",
        "groups": ["<APIKEY>"],
        "subscription": "<APIKEY>",
        "has_active_subscription": True,
        "expires_at": "<APIKEY>",
        "features_count": 2,
    }


@pytest.mark.parametrize(
    ("me", "check"),
    [
        (None, {"accounts": {}}),
        ({}, None),
        ({}, {}),
        ({}, {"accounts": []}),
        ({}, {"accounts": {"account-1": []}}),
        ({}, {"accounts": {"account-1": {"entitlement": [], "features": []}}}),
        ({}, {"accounts": {"account-1": {"entitlement": {}, "features": "many"}}}),
        ({"groups": [{"access_token": "nested"}]}, {"accounts": {}}),
        ({"country": {"access_token": "nested"}}, {"accounts": {}}),
    ],
)
def test_account_status_rejects_malformed_or_opaque_contract(me, check) -> None:
    with pytest.raises(RuntimeError, match="account_status"):
        account.normalize_account_status(me, check)


def test_list_apps_normalizes_strings_and_objects_without_reordering() -> None:
    client = FakeClient(
        routes={
            "/backend-api/apps/list": {
                "apps": [
                    "connector_calendar",
                    {"id": "asdk_app_demo", "enabled": True, "is_connected": False},
                    None,
                    17,
                    {"enabled": True},
                    "connector_calendar",
                ]
            }
        }
    )

    result = _run(_registered(apps, client).tools["list_apps"])

    assert result == [
        {
            "id": "connector_calendar",
            "type": "official_connector",
            "enabled": None,
            "connected": None,
        },
        {
            "id": "asdk_app_demo",
            "type": "third_party_sdk",
            "enabled": True,
            "connected": False,
        },
        {
            "id": "connector_calendar",
            "type": "official_connector",
            "enabled": None,
            "connected": None,
        },
    ]


@pytest.mark.parametrize("limit", [True, 0, 101, 1.5, "20"])
def test_list_tasks_rejects_non_integer_or_out_of_range_limit(limit) -> None:
    tool = _registered(conversations, FakeClient()).tools["list_tasks"]
    with pytest.raises(ValueError, match="limit"):
        _run(tool, limit=limit)


@pytest.mark.parametrize("max_messages", [True, 0, 101, 1.5, "20"])
def test_get_conversation_rejects_invalid_max_messages_before_request(
    max_messages,
) -> None:
    client = FakeClient()
    tool = _registered(conversations, client).tools["get_conversation"]

    with pytest.raises(ValueError, match="max_messages"):
        _run(tool, "conversation-1", max_messages=max_messages)
    assert client.gets == []


def test_get_conversation_projects_and_redacts_every_private_scalar() -> None:
    secret = "sk-" + "c" * 24
    client = FakeClient(
        routes={
            "/backend-api/conversation/conversation-1": {
                "id": "conversation-1",
                "title": secret,
                "create_time": secret,
                "update_time": secret,
                "mapping": {
                    "node-1": {
                        "message": {
                            "id": secret,
                            "author": {"role": "assistant", "opaque": secret},
                            "recipient": secret,
                            "status": secret,
                            "create_time": secret,
                            "content": {
                                "content_type": "multimodal_text",
                                "parts": [
                                    secret,
                                    {
                                        "content_type": "image_asset_pointer",
                                        "asset_pointer": "sediment://file-safe",
                                        "width": 4,
                                        "height": 2,
                                        "opaque": {"access_token": secret},
                                    },
                                ],
                                "opaque": {"access_token": secret},
                            },
                            "opaque": {"access_token": secret},
                        },
                        "opaque": {"access_token": secret},
                    }
                },
                "opaque": {"access_token": secret},
            }
        }
    )
    tool = _registered(conversations, client).tools["get_conversation"]

    result = _run(tool, "conversation-1")

    assert secret not in repr(result)
    assert result == {
        "id": "conversation-1",
        "title": "<APIKEY>",
        "create_time": "<APIKEY>",
        "update_time": "<APIKEY>",
        "message_count": 1,
        "messages": [
            {
                "id": "<APIKEY>",
                "role": "assistant",
                "recipient": "<APIKEY>",
                "content_type": "multimodal_text",
                "status": "<APIKEY>",
                "create_time": "<APIKEY>",
                "text": "<APIKEY>",
                "images": [
                    {
                        "asset_pointer": "sediment://file-safe",
                        "width": 4,
                        "height": 2,
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "conversation-1", "mapping": []},
        {
            "id": "conversation-1",
            "mapping": {
                "node-1": {
                    "message": {
                        "id": "message-1",
                        "author": {"role": "assistant"},
                        "recipient": {"access_token": "nested"},
                        "content": {"content_type": "text", "parts": ["safe"]},
                    }
                }
            },
        },
        {"id": {"access_token": "nested"}, "mapping": {}},
        {"id": "different-conversation", "mapping": {}},
        {"id": "conversation-1", "mapping": {}, "create_time": 1 << 80},
    ],
)
def test_get_conversation_rejects_malformed_or_mismatched_private_shape(payload) -> None:
    client = FakeClient(routes={"/backend-api/conversation/conversation-1": payload})
    tool = _registered(conversations, client).tools["get_conversation"]

    with pytest.raises(BackendContractError, match="conversation_detail"):
        _run(tool, "conversation-1")


@pytest.mark.parametrize(
    "parts",
    [
        ["safe"] * 101,
        [
            {
                "content_type": "image_asset_pointer",
                "asset_pointer": "sediment://file-safe",
                "width": 1 << 80,
            }
        ],
    ],
)
def test_get_conversation_bounds_message_parts_and_image_numbers(parts) -> None:
    payload = {
        "id": "conversation-1",
        "mapping": {
            "node-1": {
                "message": {
                    "id": "message-1",
                    "author": {"role": "assistant"},
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": parts,
                    },
                }
            }
        },
    }
    client = FakeClient(routes={"/backend-api/conversation/conversation-1": payload})
    tool = _registered(conversations, client).tools["get_conversation"]

    with pytest.raises(BackendContractError, match="conversation_detail"):
        _run(tool, "conversation-1")


def test_list_scheduled_tasks_returns_one_validated_page() -> None:
    class RecordingClient(FakeClient):
        fixed_probe: bool | None = None

        def get(self, path, **kwargs):
            self.fixed_probe = kwargs.get("fixed_probe")
            return super().get(path, **kwargs)

    client = RecordingClient(
        routes={
            "/backend-api/automations": {
                "items": [
                    {
                        "id": "task-1",
                        "updated_at": "2026-07-10T12:00:00Z",
                        "next_run_times": ["2026-07-11T12:00:00Z"],
                        "is_enabled": True,
                        "target_time_utc": "12:00",
                        "secret": "must-not-leak",
                    }
                ],
                "cursor": "next-2",
            }
        }
    )

    result = _run(
        _registered(automations, client).tools["list_scheduled_tasks"], cursor="next-1"
    )

    assert client.gets == ["/backend-api/automations"]
    assert result == {
        "items": [
            {
                "id": "task-1",
                "updated_at": "2026-07-10T12:00:00Z",
                "next_run_times": ["2026-07-11T12:00:00Z"],
                "is_enabled": True,
                "target_time_utc": "12:00",
            }
        ],
        "cursor": "next-2",
    }
    assert client.fixed_probe is False


def test_automations_register_accepts_unused_conversation() -> None:
    mcp = FakeMCP()

    automations.register(mcp, FakeClient(), conv=object())

    assert set(mcp.tools) == {"list_scheduled_tasks"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"items": "wrong", "cursor": None},
        {"items": [{"updated_at": "now"}], "cursor": None},
        {"items": [{"id": "x", "next_run_times": "wrong"}], "cursor": None},
        {"items": [{"id": "x", "is_enabled": "yes"}]},
        {"items": [{"id": "x", "updated_at": {"secret": "nested"}}]},
        {"items": [{"id": "x", "target_time_utc": {"secret": "nested"}}]},
    ],
)
def test_list_scheduled_tasks_rejects_malformed_contract(payload) -> None:
    client = FakeClient(routes={"/backend-api/automations": payload})
    tool = _registered(automations, client).tools["list_scheduled_tasks"]
    with pytest.raises(RuntimeError, match="automations"):
        _run(tool)


def test_list_scheduled_tasks_safely_projects_unproven_array_entries() -> None:
    client = FakeClient(
        routes={
            "/backend-api/automations": {
                "items": [
                    {
                        "id": "task-1",
                        "next_run_times": [
                            {"access_token": "nested"},
                            "2026-07-11T12:00:00Z",
                            17,
                            None,
                            "x" * 2_049,
                        ],
                    }
                ]
            }
        }
    )

    result = _run(_registered(automations, client).tools["list_scheduled_tasks"])

    assert result["items"][0]["next_run_times"] == ["2026-07-11T12:00:00Z"]
    assert "nested" not in repr(result)


def test_list_scheduled_tasks_redacts_secret_shaped_scalar_values() -> None:
    secret = "sk-" + "a" * 24
    client = FakeClient(
        routes={
            "/backend-api/automations": {
                "items": [
                    {
                        "id": "task-1",
                        "updated_at": secret,
                        "next_run_times": [secret],
                        "is_enabled": None,
                        "target_time_utc": secret,
                    }
                ],
                "cursor": None,
            }
        }
    )

    result = _run(_registered(automations, client).tools["list_scheduled_tasks"])

    assert secret not in repr(result)
    assert result["items"][0] == {
        "id": "task-1",
        "updated_at": "<APIKEY>",
        "next_run_times": ["<APIKEY>"],
        "is_enabled": None,
        "target_time_utc": "<APIKEY>",
    }


@pytest.mark.parametrize("cursor", ["line\nbreak", "x" * 2049, 4])
def test_list_scheduled_tasks_rejects_invalid_cursor(cursor) -> None:
    tool = _registered(automations, FakeClient()).tools["list_scheduled_tasks"]
    with pytest.raises(ValueError, match="cursor"):
        _run(tool, cursor=cursor)
