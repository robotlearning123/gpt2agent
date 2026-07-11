from __future__ import annotations

import asyncio
import threading

import pytest

from gpt2agent.errors import BackendContractError, BackendHTTPError
from gpt2agent.model_catalog import (
    ModelCatalog,
    normalize_general_models,
    normalize_work_models,
)
from gpt2agent.sse import _build_payload
from gpt2agent.tools import account, work
from tests.test_tools import FakeClient, FakeMCP


def _run(awaitable):
    return asyncio.run(awaitable)


class CatalogClient(FakeClient):
    def __init__(self) -> None:
        super().__init__(
            routes={
                "/backend-api/models": {
                    "models": [
                        {
                            "slug": "chat-only",
                            "title": "Chat",
                            "configurable_thinking_effort": True,
                            "default_thinking_effort": "medium",
                            "thinking_efforts": [
                                {"thinking_effort": "low", "label": "Low"},
                                {"thinking_effort": "high", "label": "High"},
                            ],
                        }
                    ]
                },
                "/backend-api/tpp/models/": {
                    "models": [
                        {
                            "slug": "work-only",
                            "title": "Work",
                            "max_tokens": 1000,
                            "reasoning_type": "reasoning",
                            "configurable_thinking_effort": True,
                            "default_thinking_effort": "high",
                            "thinking_efforts": [
                                {"thinking_effort": "high", "label": "High", "ui": {}}
                            ],
                        }
                    ],
                    "categories": [{"private": "ui-only"}],
                },
            }
        )
        self.auth_generation = 1


def test_general_and_work_catalogs_are_cached_separately_and_generation_bound() -> None:
    now = [0.0]
    client = CatalogClient()
    catalog = ModelCatalog(client, clock=lambda: now[0])

    first_general = _run(catalog.general())
    first_work = _run(catalog.work())
    assert first_general[0]["slug"] == "chat-only"
    assert first_work[0]["slug"] == "work-only"
    assert len(client.gets) == 2

    _run(catalog.general())
    _run(catalog.work())
    assert len(client.gets) == 2

    # Callers must not be able to mutate nested metadata held in the cache.
    first_general[0]["thinking_efforts"][0]["label"] = "mutated"
    assert _run(catalog.general())[0]["thinking_efforts"][0]["label"] == "Low"

    now[0] = 61.0
    _run(catalog.general())
    assert len(client.gets) == 3

    client.auth_generation = 2
    _run(catalog.work())
    assert len(client.gets) == 4


def test_late_old_generation_fetch_cannot_poison_new_account_cache() -> None:
    class OverlapClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.auth_generation = 0
            self.first_started = threading.Event()
            self.release_first = threading.Event()
            self.calls = 0
            self.snapshot_calls = 0

        def auth_snapshot(self):
            self.snapshot_calls += 1
            return self.auth_generation, {
                "Authorization": f"Bearer ACCOUNT_{self.auth_generation}"
            }

        def get(self, path: str, **kwargs):
            del path
            self.calls += 1
            if self.calls == 1:
                assert kwargs["auth_headers"]["Authorization"] == "Bearer ACCOUNT_0"
                self.first_started.set()
                assert self.release_first.wait(timeout=5)
                slug = "ACCOUNT_A"
            else:
                assert kwargs["auth_headers"]["Authorization"] == "Bearer ACCOUNT_1"
                slug = "ACCOUNT_B"
            return {"models": [{"slug": slug}]}

    async def overlap() -> tuple[list[dict], list[dict], list[dict], int]:
        client = OverlapClient()
        catalog = ModelCatalog(client)
        account_a = asyncio.create_task(catalog.general())
        started = await asyncio.to_thread(client.first_started.wait, 5)
        assert started

        client.auth_generation = 1
        account_b = await catalog.general()
        client.release_first.set()
        late_account_a = await account_a
        cached_for_b = await catalog.general()
        return account_b, late_account_a, cached_for_b, client.calls, client.snapshot_calls

    account_b, late_account_a, cached_for_b, calls, snapshot_calls = _run(overlap())

    assert account_b[0]["slug"] == "ACCOUNT_B"
    assert late_account_a[0]["slug"] == "ACCOUNT_A"
    assert cached_for_b[0]["slug"] == "ACCOUNT_B"
    assert calls == 2
    assert snapshot_calls >= 3


def test_validate_thinking_effort_refreshes_once_then_applies_general_metadata() -> None:
    client = CatalogClient()
    catalog = ModelCatalog(client)

    model = _run(catalog.validate_general("chat-only", "high"))
    assert model["slug"] == "chat-only"
    assert len(client.gets) == 1

    with pytest.raises(BackendHTTPError) as invalid:
        _run(catalog.validate_general("chat-only", "ultra"))
    assert invalid.value.code == "invalid_input"
    assert len(client.gets) == 2  # cached mismatch gets one forced refresh

    with pytest.raises(BackendHTTPError) as unsupported:
        _run(catalog.validate_general("work-only", None))
    assert unsupported.value.code == "unsupported"


def test_non_configurable_model_rejects_effort_as_unsupported() -> None:
    client = CatalogClient()
    client.routes["/backend-api/models"] = {
        "models": [{"slug": "plain", "configurable_thinking_effort": False}]
    }
    with pytest.raises(BackendHTTPError) as unsupported:
        _run(ModelCatalog(client).validate_general("plain", "high"))
    assert unsupported.value.code == "unsupported"


def test_general_model_projection_drops_undeclared_nested_metadata() -> None:
    [model] = normalize_general_models(
        [
            {
                "slug": "safe",
                "title": "Safe",
                "description": "Bounded metadata",
                "max_tokens": 410_000,
                "reasoning_type": "reasoning",
                "configurable_thinking_effort": True,
                "default_thinking_effort": "medium",
                "thinking_efforts": [
                    {"thinking_effort": "medium", "label": "Medium", "ui": {}}
                ],
                "tags": ["chat"],
                "capabilities": {"vision": True},
                "enabled_tools": ["canvas"],
                "product_features_keys": ["feature-a"],
                "private": {"nested": ["must", "not", "escape"]},
            }
        ]
    )

    assert model == {
        "slug": "safe",
        "title": "Safe",
        "description": "Bounded metadata",
        "max_tokens": 410_000,
        "reasoning_type": "reasoning",
        "configurable_thinking_effort": True,
        "default_thinking_effort": "medium",
        "thinking_efforts": [
            {"thinking_effort": "medium", "label": "Medium"}
        ],
        "tags": ["chat"],
        "capabilities": {"vision": True},
        "enabled_tools": ["canvas"],
        "product_features_keys": ["feature-a"],
    }


@pytest.mark.parametrize("field", ["title", "description"])
def test_general_model_projection_redacts_human_facing_secret_strings(field: str) -> None:
    secret = "sk-" + "x" * 24

    [model] = normalize_general_models([{"slug": "safe", field: secret}])

    assert secret not in model[field]
    assert model[field] == "<APIKEY>"


def test_work_model_projection_redacts_human_facing_secret_strings() -> None:
    secret = "sk-" + "x" * 24

    [model] = normalize_work_models([{"slug": "safe", "title": secret}])

    assert secret not in model["title"]
    assert model["title"] == "<APIKEY>"


@pytest.mark.parametrize(
    ("normalizer", "raw"),
    [
        (normalize_general_models, {"slug": "sk-" + "x" * 24}),
        (
            normalize_general_models,
            {"slug": "safe", "reasoning_type": "sk-" + "x" * 24},
        ),
        (
            normalize_general_models,
            {"slug": "safe", "default_thinking_effort": "sk-" + "x" * 24},
        ),
        (
            normalize_general_models,
            {
                "slug": "safe",
                "thinking_efforts": [
                    {"thinking_effort": "sk-" + "x" * 24, "label": "safe"}
                ],
            },
        ),
        (
            normalize_general_models,
            {"slug": "safe", "tags": ["sk-" + "x" * 24]},
        ),
        (
            normalize_general_models,
            {"slug": "safe", "enabled_tools": ["sk-" + "x" * 24]},
        ),
        (
            normalize_general_models,
            {"slug": "safe", "product_features_keys": ["sk-" + "x" * 24]},
        ),
        (
            normalize_general_models,
            {"slug": "safe", "capabilities": {"sk-" + "x" * 24: True}},
        ),
        (normalize_work_models, {"slug": "sk-" + "x" * 24}),
        (
            normalize_work_models,
            {"slug": "safe", "reasoning_type": "sk-" + "x" * 24},
        ),
        (
            normalize_work_models,
            {"slug": "safe", "default_thinking_effort": "sk-" + "x" * 24},
        ),
    ],
)
def test_model_projection_rejects_secret_shaped_identifier_metadata(
    normalizer, raw: dict
) -> None:
    with pytest.raises(BackendContractError):
        normalizer([raw])


def test_thinking_effort_label_is_redacted() -> None:
    secret = "sk-" + "x" * 24

    [model] = normalize_general_models(
        [
            {
                "slug": "safe",
                "thinking_efforts": [
                    {"thinking_effort": "high", "label": secret}
                ],
            }
        ]
    )

    assert model["thinking_efforts"] == [
        {"thinking_effort": "high", "label": "<APIKEY>"}
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_tokens", {"private": "payload"}),
        ("max_tokens", True),
        ("configurable_thinking_effort", "false"),
        ("configurable_thinking_effort", []),
        ("capabilities", {"vision": {"private": "payload"}}),
        ("enabled_tools", ["canvas", {"private": "payload"}]),
    ],
)
def test_general_model_projection_rejects_malformed_declared_fields(
    field: str, value: object
) -> None:
    raw = {"slug": "unsafe", field: value}
    with pytest.raises(BackendContractError, match="general_models"):
        normalize_general_models([raw])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_tokens", {"private": "payload"}),
        ("max_tokens", True),
        ("configurable_thinking_effort", "false"),
        ("configurable_thinking_effort", []),
    ],
)
def test_work_model_projection_rejects_malformed_scalars(
    field: str, value: object
) -> None:
    raw = {"slug": "unsafe", field: value}
    with pytest.raises(BackendContractError, match="work_models"):
        normalize_work_models([raw])


def test_chat_payload_omits_unset_effort_and_serializes_validated_scalar() -> None:
    omitted = _build_payload("chat-only", [{"role": "user", "content": "hi"}])
    configured = _build_payload(
        "chat-only",
        [{"role": "user", "content": "hi"}],
        thinking_effort="high",
    )

    assert "thinking_effort" not in omitted
    assert configured["thinking_effort"] == "high"


def test_account_and_work_tools_can_share_one_catalog() -> None:
    class StubCatalog:
        async def general(self) -> list[dict]:
            return [{"slug": "general", "title": "General"}]

        async def work(self) -> list[dict]:
            return [{"surface": "work", "slug": "work", "title": "Work"}]

    catalog = StubCatalog()
    client = FakeClient()
    mcp = FakeMCP()
    account.register(mcp, client, model_catalog=catalog)
    work.register(mcp, client, model_catalog=catalog)

    assert _run(mcp.tools["list_models"]())[0]["slug"] == "general"
    assert _run(mcp.tools["list_work_models"]()) == {
        "items": [{"surface": "work", "slug": "work", "title": "Work"}]
    }
    assert client.gets == []


def test_list_work_models_returns_bounded_scalar_projection_only() -> None:
    client = CatalogClient()
    mcp = FakeMCP()
    work.register(mcp, client)
    result = _run(mcp.tools["list_work_models"]())

    assert result == {
        "items": [
            {
                "surface": "work",
                "slug": "work-only",
                "title": "Work",
                "max_tokens": 1000,
                "reasoning_type": "reasoning",
                "configurable_thinking_effort": True,
                "default_thinking_effort": "high",
                "thinking_efforts": [{"thinking_effort": "high", "label": "High"}],
            }
        ]
    }
    assert "categories" not in result


@pytest.mark.parametrize("payload", [[], {}, {"models": "wrong"}])
def test_list_work_models_rejects_malformed_envelope(payload) -> None:
    client = FakeClient(routes={"/backend-api/tpp/models/": payload})
    mcp = FakeMCP()
    work.register(mcp, client)
    with pytest.raises(RuntimeError, match="work_models"):
        _run(mcp.tools["list_work_models"]())
