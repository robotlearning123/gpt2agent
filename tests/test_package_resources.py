from __future__ import annotations

import json
from importlib import resources as importlib_resources
from pathlib import Path


def test_resource_files_are_declared_as_package_data() -> None:
    pyproject = Path("pyproject.toml").read_text()
    assert '"resources/*.json"' in pyproject


def test_both_resource_files_are_readable_from_package() -> None:
    package_root = importlib_resources.files("gpt2agent")
    for name in ("feature-coverage.v1.json", "update-evidence.v1.json"):
        data = package_root.joinpath("resources", name).read_bytes()
        assert data.endswith(b"\n")
        assert json.loads(data)["schema_version"] == "1"


def test_tool_manifest_is_an_exact_ordered_provider_partition() -> None:
    from gpt2agent.tool_manifest import (
        CHATGPT_TOOL_NAMES,
        GROK_TOOL_NAMES,
        TOOL_NAMES,
    )

    assert TOOL_NAMES
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES))
    assert CHATGPT_TOOL_NAMES
    assert GROK_TOOL_NAMES
    assert set(CHATGPT_TOOL_NAMES).isdisjoint(GROK_TOOL_NAMES)
    assert CHATGPT_TOOL_NAMES + GROK_TOOL_NAMES == TOOL_NAMES
