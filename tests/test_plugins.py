from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any

import pytest

from gpt2agent.tools import plugins
from tests.test_tools import FakeClient, FakeMCP


def _run(fn, *args, **kwargs):
    return asyncio.run(fn(*args, **kwargs))


def _tools(client: FakeClient):
    mcp = FakeMCP()
    plugins.register(mcp, client)
    return mcp.tools


def test_root_array_catalog_uses_deterministic_local_pagination() -> None:
    raw = [
        {"id": f"plugin-{i}", "name": f"Plugin {i}", "version": "1", "enabled": True}
        for i in range(5)
    ]
    client = FakeClient(routes={"/backend-api/plugins/list": raw})
    tool = _tools(client)["list_plugins"]

    first = _run(tool, limit=2)
    assert [item["id"] for item in first["items"]] == ["plugin-0", "plugin-1"]
    assert first["cursor"].startswith("g2a-local-v1:")
    assert "Plugin" not in first["cursor"]

    second = _run(tool, limit=2, cursor=first["cursor"])
    assert [item["id"] for item in second["items"]] == ["plugin-2", "plugin-3"]
    assert "pageToken" not in client.gets[-1]


def _produced_local_cursor(*, offset: int = 1) -> tuple[Any, str]:
    raw = [
        {"id": f"plugin-{index}", "name": f"Plugin {index}"}
        for index in range(offset + 1)
    ]
    tool = _tools(FakeClient(routes={"/backend-api/plugins/list": raw}))["list_plugins"]
    cursor = _run(tool, limit=offset)["cursor"]
    assert isinstance(cursor, str)
    return tool, cursor


def test_local_cursor_rejects_non_base64url_characters() -> None:
    tool, cursor = _produced_local_cursor()

    with pytest.raises(ValueError, match="valid g2a-local-v1 cursor"):
        _run(tool, limit=1, cursor=cursor + "!")


def test_local_cursor_rejects_explicit_base64_padding() -> None:
    tool, cursor = _produced_local_cursor()

    with pytest.raises(ValueError, match="valid g2a-local-v1 cursor"):
        _run(tool, limit=1, cursor=cursor + "=")


def test_local_cursor_rejects_noncanonical_base64_pad_bits() -> None:
    tool, cursor = _produced_local_cursor(offset=10)
    prefix, token = cursor.split(":", 1)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    final_index = alphabet.index(token[-1])
    assert final_index % 16 == 0
    noncanonical = f"{prefix}:{token[:-1]}{alphabet[final_index + 1]}"

    with pytest.raises(ValueError, match="valid g2a-local-v1 cursor"):
        _run(tool, limit=10, cursor=noncanonical)


def test_local_cursor_rejects_noncanonical_json_payload() -> None:
    tool, cursor = _produced_local_cursor()
    prefix, token = cursor.split(":", 1)
    decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    payload = json.loads(decoded)
    payload["ignored"] = True
    noncanonical_token = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")

    with pytest.raises(ValueError, match="valid g2a-local-v1 cursor"):
        _run(tool, limit=1, cursor=f"{prefix}:{noncanonical_token}")


def test_local_cursor_is_bound_to_catalog_scope() -> None:
    class ScopeClient(FakeClient):
        def get(self, path, **kwargs):
            scope = kwargs["params"]["scope"]
            return [
                {"id": "plugin-0", "name": f"{scope} zero"},
                {"id": "plugin-1", "name": f"{scope} one"},
            ]

    tool = _tools(ScopeClient())["list_plugins"]
    cursor = _run(tool, scope="USER", limit=1)["cursor"]

    with pytest.raises(RuntimeError, match="fingerprint"):
        _run(tool, scope="WORKSPACE", limit=1, cursor=cursor)


def test_current_web_catalog_maps_only_release_version_and_backend_cursor() -> None:
    client = FakeClient(
        routes={
            "/backend-api/plugins/list": {
                "plugins": [
                    {
                        "id": "p1",
                        "name": "mail owner@example.com",
                        "display_name": "Display",
                        "release": {"version": "3", "description": "private"},
                        "owner": {"id": "owner-secret"},
                    }
                ],
                "pagination": {"next_page_token": "backend-token"},
            }
        }
    )
    result = _run(_tools(client)["list_plugins"], scope="WORKSPACE", limit=5)

    assert result["cursor"] == "backend-token"
    assert result["items"][0]["release_version"] == "3"
    assert result["items"][0]["name"] == "mail <EMAIL>"
    assert "release" not in result["items"][0]
    assert "owner" not in result["items"][0]
    assert client.gets == ["/backend-api/plugins/list"]


def test_backend_cursor_is_sent_as_page_token() -> None:
    class ParamClient(FakeClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.params = []
            self.fixed_probe = []

        def get(self, path, **kwargs):
            self.params.append(kwargs.get("params"))
            self.fixed_probe.append(kwargs.get("fixed_probe"))
            return super().get(path, **kwargs)

    client = ParamClient(
        routes={
            "/backend-api/plugins/list": {"plugins": [], "pagination": {"next_page_token": None}}
        }
    )
    _run(_tools(client)["list_plugins"], cursor="opaque token")
    assert client.gets == ["/backend-api/plugins/list"]
    assert client.params == [{"scope": "USER", "limit": 50, "pageToken": "opaque token"}]
    assert client.fixed_probe == [False]


def test_stale_local_cursor_is_contract_changed() -> None:
    class ChangingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def get(self, path, **kwargs):
            self.calls += 1
            suffix = "a" if self.calls == 1 else "b"
            return [{"id": f"{suffix}-{i}", "name": suffix} for i in range(3)]

    client = ChangingClient()
    tool = _tools(client)["list_plugins"]
    cursor = _run(tool, limit=1)["cursor"]
    with pytest.raises(RuntimeError, match="fingerprint"):
        _run(tool, limit=1, cursor=cursor)


def test_local_cursor_fingerprint_uses_pre_redaction_plugin_ids() -> None:
    class ChangingSecretIdClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def get(self, path, **kwargs):
            self.calls += 1
            marker = "a" if self.calls == 1 else "b"
            return [
                {"id": "sk-" + marker * 32},
                {"id": "sk-" + "z" * 32},
            ]

    tool = _tools(ChangingSecretIdClient())["list_plugins"]
    cursor = _run(tool, limit=1)["cursor"]
    with pytest.raises(RuntimeError, match="fingerprint"):
        _run(tool, limit=1, cursor=cursor)


def test_local_cursor_fingerprint_is_keyed_not_an_offline_raw_id_oracle() -> None:
    raw = [
        {"id": "owner@example.com"},
        {"id": "sk-" + "s" * 32},
    ]
    encoded_ids = json.dumps(
        [item["id"] for item in raw], ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    unkeyed = hashlib.sha256(encoded_ids).hexdigest()

    first = plugins.normalize_plugin_catalog(raw, limit=1, cursor=None)
    second = plugins.normalize_plugin_catalog(raw, limit=1, cursor=None)
    fingerprint, offset = plugins._decode_local_cursor(first["cursor"])

    assert first["cursor"] == second["cursor"]
    assert offset == 1
    assert fingerprint != unkeyed
    assert "owner@example.com" not in repr(first)
    assert "sk-" + "s" * 32 not in repr(first)


def test_local_cursor_cannot_be_reinterpreted_after_envelope_changes() -> None:
    local_page = [{"id": f"p-{i}"} for i in range(2)]

    class ChangingEnvelopeClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def get(self, path, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return local_page
            return {"plugins": [], "pagination": {"next_page_token": None}}

    tool = _tools(ChangingEnvelopeClient())["list_plugins"]
    cursor = _run(tool, limit=1)["cursor"]
    with pytest.raises(RuntimeError, match="local cursor"):
        _run(tool, limit=1, cursor=cursor)


def test_backend_cursor_is_contract_changed_when_root_array_ignores_it() -> None:
    client = FakeClient(routes={"/backend-api/plugins/list": [{"id": "p-1"}]})
    with pytest.raises(RuntimeError, match="backend cursor"):
        _run(_tools(client)["list_plugins"], cursor="backend-token")


@pytest.mark.parametrize("limit", [0, 51, True, "5"])
def test_plugin_limit_is_bounded(limit) -> None:
    with pytest.raises(ValueError, match="limit"):
        _run(_tools(FakeClient())["list_plugins"], limit=limit)


def test_current_web_over_limit_or_missing_release_is_contract_changed() -> None:
    over = {
        "plugins": [
            {"id": "p1", "release": {}},
            {"id": "p2", "release": {}},
        ],
        "pagination": {"next_page_token": "next"},
    }
    with pytest.raises(RuntimeError, match="limit"):
        _run(_tools(FakeClient(routes={"/backend-api/plugins/list": over}))["list_plugins"], limit=1)

    missing_release = {"plugins": [{"id": "p1"}], "pagination": {"next_page_token": None}}
    with pytest.raises(RuntimeError, match="release"):
        _run(
            _tools(FakeClient(routes={"/backend-api/plugins/list": missing_release}))[
                "list_plugins"
            ]
        )


def test_installed_nested_envelope_is_normalized_without_nested_objects() -> None:
    payload = {
        "plugins": {
            "results": [
                {
                    "id": "i1",
                    "enabled": False,
                    "marketplace": {"name": "Market", "id": "secret"},
                    "apps": [{"id": "app-1", "name": "nested"}],
                    "skills": [{"name": "skill-1", "enabled": False}],
                }
            ],
            "page": {"has_more": False},
        }
    }
    result = _run(
        _tools(FakeClient(routes={"/backend-api/plugins/installed": payload}))[
            "list_installed_plugins"
        ]
    )
    assert result["items"][0]["id"] == "i1"
    assert result["items"][0]["enabled"] is False
    assert result["items"][0]["app_ids"] == ["app-1"]
    assert result["items"][0]["disabled_skill_names"] == ["skill-1"]
    assert "marketplace" not in result["items"][0]


def test_installed_plugins_redact_secrets_from_every_allowlisted_string() -> None:
    secret = "sk-" + "s" * 32
    scalar_fields = {
        field: secret
        for field in plugins._SCALAR_FIELDS
        if field not in {"enabled", "release_version"}
    }
    payload = {
        "plugins": [
            {
                **scalar_fields,
                "release_version": secret,
                **{field: [secret] for field in plugins._LIST_FIELDS},
            }
        ]
    }

    result = plugins.normalize_installed_plugins(payload)

    assert secret not in repr(result)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"plugins": {"results": [], "page": {"has_more": True}}},
        {"plugins": {"results": "wrong", "page": {}}},
    ],
)
def test_installed_rejects_unknown_or_unpageable_envelope(payload) -> None:
    tool = _tools(FakeClient(routes={"/backend-api/plugins/installed": payload}))[
        "list_installed_plugins"
    ]
    with pytest.raises(RuntimeError, match="installed_plugins"):
        _run(tool)
