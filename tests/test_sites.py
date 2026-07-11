from __future__ import annotations

import asyncio

import pytest

from gpt2agent.tools import sites
from tests.test_tools import FakeClient, FakeMCP


def _run(fn, *args, **kwargs):
    return asyncio.run(fn(*args, **kwargs))


def _tools(client: FakeClient):
    mcp = FakeMCP()
    sites.register(mcp, client)
    return mcp.tools


def test_sites_register_accepts_unused_conversation() -> None:
    mcp = FakeMCP()

    sites.register(mcp, FakeClient(), conv=object())

    assert set(mcp.tools) == {"sites_access", "list_sites"}


def test_sites_access_exposes_only_allowlisted_nullable_booleans() -> None:
    payload = {
        "enabled": True,
        "custom_domains_enabled": False,
        "requires_workspace_slug": None,
        "workspace_slug": "identifying-workspace",
        "unknown": "secret",
    }
    result = _run(
        _tools(FakeClient(routes={"/backend-api/websites/access": payload}))["sites_access"]
    )
    assert result == {
        "enabled": True,
        "custom_domains_enabled": False,
        "requires_workspace_slug": None,
    }


@pytest.mark.parametrize("payload", [[], {"enabled": "yes"}, {"enabled": 1}])
def test_sites_access_rejects_invalid_known_shape(payload) -> None:
    tool = _tools(FakeClient(routes={"/backend-api/websites/access": payload}))[
        "sites_access"
    ]
    with pytest.raises(RuntimeError, match="sites_access"):
        _run(tool)


def test_list_sites_returns_one_page_and_reduces_urls_to_presence() -> None:
    payload = {
        "items": [
            {
                "id": "site-1",
                "title": "owner@example.com site",
                "slug": "private-slug",
                "status": "published",
                "updated_at": "2026-07-10T12:00:00Z",
                "disabled_by": None,
                "sharing": {"access_mode": "private", "user_count": 2, "group_count": 1},
                "live_url": "https://secret.example/site",
                "preview_url": None,
                "screenshot_url": "https://secret.example/image.png",
                "content": "must-not-leak",
            }
        ],
        "cursor": "after-2",
    }
    class RecordingClient(FakeClient):
        fixed_probe: bool | None = None

        def get(self, path, **kwargs):
            self.fixed_probe = kwargs.get("fixed_probe")
            return super().get(path, **kwargs)

    client = RecordingClient(routes={"/backend-api/websites": payload})
    result = _run(_tools(client)["list_sites"], limit=7, cursor="after-1")

    assert client.gets == ["/backend-api/websites"]
    item = result["items"][0]
    assert item["title"] == "<EMAIL> site"
    assert item["has_live_url"] is True
    assert item["has_preview"] is False
    assert item["has_screenshot"] is True
    assert "live_url" not in item and "content" not in item
    assert result["cursor"] == "after-2"
    assert client.fixed_probe is False


@pytest.mark.parametrize("limit", [0, 101, True, 2.5])
def test_list_sites_rejects_invalid_limit(limit) -> None:
    with pytest.raises(ValueError, match="limit"):
        _run(_tools(FakeClient())["list_sites"], limit=limit)


@pytest.mark.parametrize(
    "payload",
    [{}, {"items": "wrong", "cursor": None}, {"items": [{"title": "missing id"}]}],
)
def test_list_sites_rejects_malformed_page(payload) -> None:
    tool = _tools(FakeClient(routes={"/backend-api/websites": payload}))["list_sites"]
    with pytest.raises(RuntimeError, match="list_sites"):
        _run(tool)
