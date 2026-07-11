from __future__ import annotations

import asyncio

import pytest

from gpt2agent.errors import BackendContractError
from gpt2agent.tools import images
from tests.test_tools import FakeClient, FakeMCP


class _ImageConversation:
    def __init__(self, result: object) -> None:
        self.result = result

    async def image_gen(
        self,
        prompt: str,
        *,
        model: str = "gpt-5-3",
        auth_headers: dict[str, str] | None = None,
    ) -> object:
        return self.result


def _generate(result: object, client: FakeClient | None = None) -> tuple[dict, FakeClient]:
    active_client = client or FakeClient()
    mcp = FakeMCP()
    images.register(mcp, active_client, _ImageConversation(result))
    return asyncio.run(mcp.tools["generate_image"]("draw a test")), active_client


def test_generate_image_returns_a_fresh_closed_projection() -> None:
    secret = "sk-" + "z" * 24
    upstream = {
        "conversation_id": f"conversation-{secret}",
        "opaque_top_level": {"authorization": secret},
        "assets": [
            {
                "asset_pointer": "sediment://file-safe",
                "file_id": "file-safe",
                "width": 640,
                "height": 480,
                "size_bytes": 1234,
                "opaque_asset": {"access_token": secret},
            }
        ],
    }
    client = FakeClient(
        routes={
            "/backend-api/files/file-safe/download": {
                "download_url": "https://download.example/image",
                "file_name": "safe.png",
                "file_size_bytes": 1234,
                "mime_type": "image/png",
                "opaque_download": {"secret": secret},
            }
        }
    )

    result, _ = _generate(upstream, client)

    assert result == {
        "conversation_id": "conversation-<APIKEY>",
        "assets": [
            {
                "asset_pointer": "sediment://file-safe",
                "file_id": "file-safe",
                "width": 640,
                "height": 480,
                "size_bytes": 1234,
                "download_url": "https://download.example/image",
                "file_name": "safe.png",
                "file_size_bytes": 1234,
                "mime_type": "image/png",
            }
        ],
    }
    assert result is not upstream
    assert result["assets"][0] is not upstream["assets"][0]
    assert secret not in repr(result)


def test_generate_image_missing_download_url_reports_contract_changed() -> None:
    upstream = {
        "conversation_id": "conversation-safe",
        "assets": [
            {
                "asset_pointer": "sediment://file-safe",
                "file_id": "file-safe",
            }
        ],
    }
    client = FakeClient(
        routes={
            "/backend-api/files/file-safe/download": {},
            "/backend-api/files/file-safe": {"id": "file-safe", "name": "safe.png"},
        }
    )

    result, _ = _generate(upstream, client)
    asset = result["assets"][0]

    assert asset["download_error"] == "contract_changed"
    assert "download_url" not in asset


def test_generate_image_info_fallback_copies_documented_metadata() -> None:
    upstream = {
        "conversation_id": "conversation-safe",
        "assets": [
            {
                "asset_pointer": "sediment://file-safe",
                "file_id": "file-safe",
            }
        ],
    }
    client = FakeClient(
        routes={
            "/backend-api/files/file-safe/download": {
                "download_url": "https://download.example/image",
                "file_name": "",
            },
            "/backend-api/files/file-safe": {
                "id": "file-safe",
                "name": "safe.png",
                "file_size_bytes": 1234,
                "mime_type": "image/png",
                "use_case": "generated",
                "state": "ready",
                "creation_time": 1234567890,
            },
        }
    )

    result, _ = _generate(upstream, client)
    asset = result["assets"][0]

    assert asset["file_size_bytes"] == 1234
    assert asset["mime_type"] == "image/png"


@pytest.mark.parametrize(
    "result",
    [
        {"conversation_id": {"authorization": "opaque"}, "assets": []},
        {"conversation_id": "", "assets": []},
        {"conversation_id": "conversation-safe", "assets": None},
        {
            "conversation_id": "conversation-safe",
            "assets": [
                {
                    "asset_pointer": "sediment://different-file",
                    "file_id": "file-safe",
                }
            ],
        },
        {
            "conversation_id": "conversation-safe",
            "assets": [
                {
                    "asset_pointer": "sediment://file-safe",
                    "file_id": "file-safe",
                    "width": 1 << 63,
                }
            ],
        },
        {
            "conversation_id": "conversation-safe",
            "assets": [
                {
                    "asset_pointer": "sediment://file-safe",
                    "file_id": "file-safe",
                    "height": True,
                }
            ],
        },
        {
            "conversation_id": "conversation-safe",
            "assets": [
                {
                    "asset_pointer": "sediment://file-safe",
                    "file_id": "file-safe",
                    "size_bytes": -1,
                }
            ],
        },
    ],
)
def test_generate_image_rejects_malformed_projection_before_file_reads(
    result: object,
) -> None:
    client = FakeClient()
    mcp = FakeMCP()
    images.register(mcp, client, _ImageConversation(result))

    with pytest.raises(BackendContractError):
        asyncio.run(mcp.tools["generate_image"]("draw a test"))

    assert client.gets == []


def test_generate_image_rejects_more_than_100_assets_before_file_reads() -> None:
    result = {
        "conversation_id": "conversation-safe",
        "assets": [
            {
                "asset_pointer": f"sediment://file-{index}",
                "file_id": f"file-{index}",
            }
            for index in range(101)
        ],
    }
    client = FakeClient()
    mcp = FakeMCP()
    images.register(mcp, client, _ImageConversation(result))

    with pytest.raises(BackendContractError, match="100"):
        asyncio.run(mcp.tools["generate_image"]("draw a test"))

    assert client.gets == []


def test_generate_image_rejects_secret_shaped_backend_ids_without_echoing_them() -> None:
    secret = "sk-" + "i" * 24
    result = {
        "conversation_id": "conversation-safe",
        "assets": [
            {
                "asset_pointer": f"sediment://{secret}",
                "file_id": secret,
            }
        ],
    }
    client = FakeClient()
    mcp = FakeMCP()
    images.register(mcp, client, _ImageConversation(result))

    with pytest.raises(BackendContractError) as caught:
        asyncio.run(mcp.tools["generate_image"]("draw a test"))

    assert secret not in str(caught.value)
    assert client.gets == []
