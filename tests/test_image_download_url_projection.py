from __future__ import annotations

import pytest

from gpt2agent.errors import BackendContractError
from gpt2agent.tools.images import normalize_download_info


def test_download_projection_preserves_valid_signed_https_url_exactly() -> None:
    signed_url = (
        "https://files.oaiusercontent.com/file-safe/image.png"
        "?se=2026-07-10T18%3A00%3A00Z&sp=r&sv=2024-11-04&sr=b"
        "&sig=a%2Bb%2Fc%3D&response-content-disposition="
        "attachment%3B%20filename%3Dimage.png"
    )

    projected = normalize_download_info({"download_url": signed_url})

    assert projected["download_url"] == signed_url


@pytest.mark.parametrize(
    "download_url",
    (
        "http://download.example.com/file",
        "//download.example.com/file",
        "/backend-api/files/file-safe/download",
        "https://user:password@download.example.com/file",
        "https://download.example.com/file#fragment",
        "https://download.example.com/file\nX-Injected: value",
        "https://download.example.com\\@localhost/file",
        "https://download.example.com/%5C..%5Cadmin",
        "https://download.example.com/file%0D%0AX-Injected%3Avalue",
        "https://download.example.com/file%ZZ",
        "https://download.example.com:bad/file",
        "https://download.example.com:8443/file",
        "https:///missing-host",
        "https://localhost/file",
        "https://service.internal/file",
        "https://printer.local/file",
        "https://intranet/file",
        "https://127.0.0.1/file",
        "https://10.0.0.8/file",
        "https://8.8.8.8/file",
        "https://[::1]/file",
        "https://[2606:4700:4700::1111]/file",
        "https://0x7f.0.0.1/file",
        "https://0177.0.0.1/file",
        "https://127.1/file",
        "https://2130706433/file",
    ),
)
def test_download_projection_rejects_unsafe_or_malformed_destinations(
    download_url: str,
) -> None:
    with pytest.raises(BackendContractError):
        normalize_download_info({"download_url": download_url})
