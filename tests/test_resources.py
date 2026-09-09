from __future__ import annotations

import asyncio
import json

from gpt2agent import resources


class FakeMCP:
    def __init__(self) -> None:
        self.resources: dict[str, tuple[dict, object]] = {}

    def resource(self, uri: str, **kwargs):
        def decorate(fn):
            self.resources[uri] = (kwargs, fn)
            return fn

        return decorate


def test_static_resources_register_exact_uris_and_mime_type() -> None:
    mcp = FakeMCP()
    resources.register(mcp)
    assert set(mcp.resources) == {
        "chatgpt://feature-coverage",
        "chatgpt://update-evidence",
    }
    assert all(meta["mime_type"] == "application/json" for meta, _ in mcp.resources.values())


def test_static_resource_reads_are_deterministic_valid_json() -> None:
    mcp = FakeMCP()
    resources.register(mcp)
    for _, reader in mcp.resources.values():
        first = asyncio.run(reader())
        second = asyncio.run(reader())
        assert first == second
        assert json.dumps(json.loads(first), sort_keys=True, separators=(",", ":")) + "\n" == first


def test_feature_coverage_matches_manifest_and_defers_voice() -> None:
    from gpt2agent.capabilities import CAPABILITY_IDS
    from gpt2agent.tool_manifest import CHATGPT_TOOL_NAMES

    coverage = json.loads(resources.read_packaged_json("feature-coverage.v1.json"))
    assert coverage["schema_version"] == "1"
    assert coverage["tools"] == list(CHATGPT_TOOL_NAMES)
    assert not any(name.startswith("grok_") for name in coverage["tools"])
    capabilities = {item["id"]: item for item in coverage["capabilities"]}
    assert list(capabilities) == list(CAPABILITY_IDS)
    expected_fields = {
        "id",
        "surface",
        "entitled",
        "reachable_now",
        "reachability_scope",
        "exposed_by_mcp",
        "officially_supported",
        "evidence_source",
        "observed_at",
        "status",
        "reason",
        "item_contract_status",
    }
    assert all(set(record) == expected_fields for record in capabilities.values())
    assert all(record["entitled"] is None for record in capabilities.values())
    assert all(record["officially_supported"] is False for record in capabilities.values())
    assert capabilities["voice_catalog"]["status"] == "deferred"
    assert capabilities["voice_catalog"]["item_contract_status"] == "not_applicable"
    assert capabilities["gpt_live"]["status"] == "deferred"
    assert capabilities["projects"]["status"] == "unsupported"
    assert capabilities["image_generation"]["status"] == "available"
    assert capabilities["image_generation"]["reason"] == (
        "MCP tool exposed; live execution unverified in this release"
    )
    for capability_id in ("conversations", "memory", "custom_instructions"):
        assert capabilities[capability_id]["reachability_scope"] == "none"
        assert "automatic probe omitted" in capabilities[capability_id]["reason"]
    assert not any(item.get("reachable_now") is True for item in capabilities.values())


def test_feature_coverage_declares_exact_bounded_status_values() -> None:
    coverage = json.loads(resources.read_packaged_json("feature-coverage.v1.json"))

    assert coverage["status_values"] == [
        "available",
        "deferred",
        "unsupported",
        "ok",
        "unavailable",
        "unverified",
        "login_required",
        "access_indeterminate",
        "contract_changed",
        "temporarily_failed",
    ]
    assert {record["status"] for record in coverage["capabilities"]} <= set(
        coverage["status_values"]
    )


def test_update_evidence_is_public_drift_only() -> None:
    evidence = json.loads(resources.read_packaged_json("update-evidence.v1.json"))
    assert evidence["scope"] == "public_surface_drift"
    assert evidence["account_contract_status"] == "not_checked"
    assert evidence["private_adapter_status"] == "not_checked"
    assert all(source["url"].startswith("https://") for source in evidence["sources"])
    urls = {source["url"] for source in evidence["sources"]}
    assert "https://help.openai.com/en/articles/20001274" in urls
    assert not any("8400625" in url for url in urls)
