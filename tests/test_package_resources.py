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
