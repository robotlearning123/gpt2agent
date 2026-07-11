"""Static packaged MCP resources; reads never contact an account or the network."""

from __future__ import annotations

from importlib import resources as importlib_resources


def read_packaged_json(filename: str) -> str:
    if filename not in {"feature-coverage.v1.json", "update-evidence.v1.json"}:
        raise ValueError("unknown packaged resource")
    return (
        importlib_resources.files("gpt2agent")
        .joinpath("resources", filename)
        .read_text(encoding="utf-8")
    )


def register(mcp) -> None:
    @mcp.resource("chatgpt://feature-coverage", mime_type="application/json")
    async def feature_coverage() -> str:
        return read_packaged_json("feature-coverage.v1.json")

    @mcp.resource("chatgpt://update-evidence", mime_type="application/json")
    async def update_evidence() -> str:
        return read_packaged_json("update-evidence.v1.json")
