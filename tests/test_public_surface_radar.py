"""Tests for the no-secret public compatibility radar."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_source_catalog_covers_normative_surfaces_and_bundle_contract() -> None:
    from scripts.public_surface_radar import CHATGPT_CONTRACT_MARKERS, SOURCES

    sources = {source.source_id: source for source in SOURCES}
    source_ids = set(sources)
    assert {
        "chatgpt-release-notes",
        "chatgpt-whats-new",
        "chatgpt-voice",
        "chatgpt-work",
        "chatgpt-sites",
        "chatgpt-plugins",
        "codex-changelog",
        "chatgpt-manifest-bundles",
        "mcp-python-sdk-release",
        "mcp-specification-release",
    } <= source_ids
    assert sources["chatgpt-whats-new"].url == "https://learn.chatgpt.com/docs/whats-new"
    assert {
        "models-route",
        "apps-route",
        "automations-route",
        "automations-query",
        "plugins-catalog-route",
        "plugins-installed-route",
        "work-models-route",
        "sites-route",
        "scheduled-envelope",
        "plugin-pagination-envelope",
        "work-model-envelope",
        "sites-access-envelope",
    } <= set(CHATGPT_CONTRACT_MARKERS)


def test_url_validation_rejects_non_https_credentials_ports_and_unknown_hosts() -> None:
    from scripts.public_surface_radar import RadarError, validate_url

    for url in (
        "http://help.openai.com/release-notes",
        "https://user:secret@help.openai.com/release-notes",
        "https://help.openai.com:444/release-notes",
        "https://evil.example/release-notes",
    ):
        with pytest.raises(RadarError):
            validate_url(url)

    validated = validate_url("https://help.openai.com/en/articles/6825453-chatgpt-release-notes")
    assert validated.hostname == "help.openai.com"


def test_fetch_rejects_cross_host_redirect_content_type_and_oversize() -> None:
    from scripts.public_surface_radar import MAX_BYTES, RadarError, SourceSpec, fetch_source

    class Response:
        status = 200

        def __init__(self, *, final_url: str, content_type: str, body: bytes) -> None:
            self._final_url = final_url
            self.headers = {
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
            }
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self) -> str:
            return self._final_url

        def read(self, amount: int = -1) -> bytes:
            return self._body if amount < 0 else self._body[:amount]

    source = SourceSpec(
        source_id="release-notes",
        url="https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
        kind="documentation",
        markers=("ChatGPT",),
    )

    def opener(response: Response):
        return lambda _request, timeout: response

    with pytest.raises(RadarError, match="cross-host redirect"):
        fetch_source(
            source,
            opener=opener(
                Response(
                    final_url="https://openai.com/release-notes",
                    content_type="text/html",
                    body=b"ChatGPT",
                )
            ),
        )
    with pytest.raises(RadarError, match="content type"):
        fetch_source(
            source,
            opener=opener(
                Response(
                    final_url=source.url,
                    content_type="application/octet-stream",
                    body=b"ChatGPT",
                )
            ),
        )
    with pytest.raises(RadarError, match="too large"):
        fetch_source(
            source,
            opener=opener(
                Response(
                    final_url=source.url,
                    content_type="text/html",
                    body=b"x" * (MAX_BYTES + 1),
                )
            ),
        )


def test_marker_loss_fails_but_document_fingerprint_drift_only_warns() -> None:
    from scripts.public_surface_radar import SourceSpec, build_report, evaluate_source

    contract = SourceSpec(
        source_id="mcp-release",
        url="https://api.github.com/repos/modelcontextprotocol/python-sdk/releases/latest",
        kind="contract",
        markers=("tag_name",),
    )
    documentation = SourceSpec(
        source_id="chatgpt-release-notes",
        url="https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
        kind="documentation",
        markers=("ChatGPT",),
        expected_sha256="0" * 64,
    )

    missing = evaluate_source(contract, b'{"name": "release"}')
    drifted = evaluate_source(documentation, b"ChatGPT release notes changed")
    report = build_report([missing, drifted], observed_at="2026-07-10T13:17:00Z")

    assert missing["status"] == "contract_marker_missing"
    assert drifted["status"] == "review_needed"
    assert report["account_contract_status"] == "not_checked"
    assert report["private_adapter_status"] == "not_checked"
    assert report["exit_code"] == 1


def test_previous_artifact_fingerprint_detects_ordinary_document_change(
    tmp_path: Path,
) -> None:
    from scripts.public_surface_radar import (
        SourceSpec,
        evaluate_source,
        load_previous_fingerprints,
    )

    source = SourceSpec(
        source_id="chatgpt-release-notes",
        url="https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
        kind="documentation",
        surface="chatgpt",
        markers=("ChatGPT",),
    )
    original = evaluate_source(source, b"<h1>ChatGPT</h1><p>Original notes</p>")
    assert original["status"] == "review_needed"
    assert original["drift_reason"] == "baseline_missing"

    previous = tmp_path / "previous.json"
    previous.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "scope": "public_surface_drift",
                "results": [original],
            }
        ),
        encoding="utf-8",
    )
    fingerprints = load_previous_fingerprints(previous)

    unchanged = evaluate_source(
        source,
        b"<h1>ChatGPT</h1><p>Original notes</p>",
        previous_fingerprints=fingerprints,
    )
    changed = evaluate_source(
        source,
        b"<h1>ChatGPT</h1><p>New ordinary product note</p>",
        previous_fingerprints=fingerprints,
    )

    assert unchanged["status"] == "ok"
    assert changed["status"] == "review_needed"
    assert changed["drift_reason"] == "normalized_fingerprint_changed"


def test_document_fingerprint_ignores_downloaded_executable_text() -> None:
    from scripts.public_surface_radar import SourceSpec, evaluate_source

    source = SourceSpec(
        source_id="codex-changelog",
        url="https://learn.chatgpt.com/docs/changelog",
        kind="documentation",
        markers=("Codex",),
    )
    first = evaluate_source(
        source,
        b"<h1>Codex</h1><script>bundle-v1-secret</script><p>Release</p>",
    )
    second = evaluate_source(
        source,
        b"<h1>Codex</h1><script>bundle-v2-secret</script><p>Release</p>",
        previous_fingerprints={source.source_id: first["normalized_sha256"]},
    )

    assert second["status"] == "ok"


def test_document_markers_must_be_present_in_visible_text() -> None:
    from scripts.public_surface_radar import SourceSpec, evaluate_source

    source = SourceSpec(
        source_id="chatgpt-release-notes",
        url="https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
        kind="documentation",
        markers=("ChatGPT",),
    )
    result = evaluate_source(
        source,
        b"<script>ChatGPT</script><h1>Unrelated challenge page</h1>",
    )

    assert result["missing_markers"] == ["ChatGPT"]
    assert result["drift_reason"] == "documentation_marker_missing"


def test_bundle_probe_recurses_without_execution_and_checks_fallback_metadata() -> None:
    from scripts.public_surface_radar import (
        CHATGPT_CONTRACT_MARKERS,
        SourceSpec,
        evaluate_chatgpt_bundles,
    )

    source = SourceSpec(
        source_id="chatgpt-manifest-bundles",
        url="https://chatgpt.com/",
        kind="contract",
        surface="chatgpt",
        parser="chatgpt_bundles",
    )
    marker_values = [variants[0] for variants in CHATGPT_CONTRACT_MARKERS.values()]
    split = len(marker_values) // 2
    manifest = b'<script src="/cdn/assets/main.js"></script>'
    bodies = {
        "https://chatgpt.com/cdn/assets/main.js": (
            " ".join(marker_values[:split]) + ' "/cdn/assets/lazy.js"'
        ).encode(),
        "https://chatgpt.com/cdn/assets/lazy.js": (
            " ".join(marker_values[split:])
            + " prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1234567"
        ).encode(),
    }
    fetched: list[str] = []

    def fetch_asset(url: str) -> bytes:
        fetched.append(url)
        return bodies[url]

    result = evaluate_chatgpt_bundles(
        source,
        manifest,
        fetch_asset=fetch_asset,
        fallback_client_version="prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        fallback_client_build="1234567",
    )

    assert fetched == list(bodies)
    assert result["static_assets_observed"] == 2
    assert result["missing_marker_groups"] == []
    assert result["fallback_metadata_status"] == "current"
    assert result["executed_downloaded_code"] is False
    assert "prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in json.dumps(result)


def test_bundle_probe_accepts_any_javascript_path_on_the_exact_official_cdn() -> None:
    from scripts.public_surface_radar import (
        CHATGPT_CONTRACT_MARKERS,
        SourceSpec,
        evaluate_chatgpt_bundles,
    )

    source = SourceSpec(
        source_id="chatgpt-manifest-bundles",
        url="https://chatgpt.com/",
        kind="contract",
        surface="chatgpt",
        parser="chatgpt_bundles",
    )
    content = (
        " ".join(variants[0] for variants in CHATGPT_CONTRACT_MARKERS.values())
        + " prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1234567"
    ).encode()
    fetched: list[str] = []

    def fetch_asset(url: str) -> bytes:
        fetched.append(url)
        return content

    result = evaluate_chatgpt_bundles(
        source,
        b'<script src="https://cdn.oaistatic.com/unversioned/app-hash.js"></script>',
        fetch_asset=fetch_asset,
        fallback_client_version="prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        fallback_client_build="1234567",
    )

    assert fetched == ["https://cdn.oaistatic.com/unversioned/app-hash.js"]
    assert result["static_assets_observed"] == 1
    assert result["missing_marker_groups"] == []


def test_bundle_fingerprint_is_normalized_before_prior_artifact_comparison() -> None:
    from scripts.public_surface_radar import (
        CHATGPT_CONTRACT_MARKERS,
        SourceSpec,
        evaluate_chatgpt_bundles,
    )

    source = SourceSpec(
        source_id="chatgpt-manifest-bundles",
        url="https://chatgpt.com/",
        kind="contract",
        surface="chatgpt",
        parser="chatgpt_bundles",
    )
    content = (
        " ".join(variants[0] for variants in CHATGPT_CONTRACT_MARKERS.values())
        + " prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1234567"
    )
    first = evaluate_chatgpt_bundles(
        source,
        b'<script src="/cdn/assets/main.js"></script>',
        fetch_asset=lambda _url: content.encode(),
        fallback_client_version="prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        fallback_client_build="1234567",
    )
    second = evaluate_chatgpt_bundles(
        source,
        b'<script  src="/cdn/assets/main.js"></script>',
        fetch_asset=lambda _url: ("\n\t" + content.replace(" ", "  ") + "\n").encode(),
        fallback_client_version="prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        fallback_client_build="1234567",
        previous_fingerprints={source.source_id: first["normalized_sha256"]},
    )

    assert second["status"] == "ok"


def test_bundle_marker_or_fallback_loss_is_a_hard_failure() -> None:
    from scripts.public_surface_radar import (
        CHATGPT_CONTRACT_MARKERS,
        SourceSpec,
        build_report,
        evaluate_chatgpt_bundles,
    )

    source = SourceSpec(
        source_id="chatgpt-manifest-bundles",
        url="https://chatgpt.com/",
        kind="contract",
        surface="chatgpt",
        parser="chatgpt_bundles",
    )
    all_markers = " ".join(variants[0] for variants in CHATGPT_CONTRACT_MARKERS.values()).encode()
    missing_route = evaluate_chatgpt_bundles(
        source,
        b'<script src="/cdn/assets/main.js"></script>',
        fetch_asset=lambda _url: all_markers.replace(b"/backend-api/models", b"gone"),
        fallback_client_version="prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        fallback_client_build="1234567",
    )
    stale_fallback = evaluate_chatgpt_bundles(
        source,
        b'<script src="/cdn/assets/main.js"></script>',
        fetch_asset=lambda _url: all_markers + b" prod-other 9999999",
        fallback_client_version="prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        fallback_client_build="1234567",
    )

    assert missing_route["status"] == "contract_marker_missing"
    assert "models-route" in missing_route["missing_marker_groups"]
    assert stale_fallback["status"] == "fallback_metadata_stale"
    assert (
        build_report([missing_route, stale_fallback], observed_at="2026-07-10T13:17:00Z")[
            "exit_code"
        ]
        == 1
    )


def test_bundle_fetch_attempts_are_bounded_even_when_every_asset_fails() -> None:
    from scripts.public_surface_radar import (
        MAX_STATIC_ASSETS,
        RadarError,
        SourceSpec,
        evaluate_chatgpt_bundles,
    )

    source = SourceSpec(
        source_id="chatgpt-manifest-bundles",
        url="https://chatgpt.com/",
        kind="contract",
        surface="chatgpt",
        parser="chatgpt_bundles",
    )
    manifest = "".join(
        f'<script src="/cdn/assets/chunk-{index}.js"></script>'
        for index in range(MAX_STATIC_ASSETS + 5)
    ).encode()
    attempted: list[str] = []

    def fail(url: str) -> bytes:
        attempted.append(url)
        raise RadarError("synthetic fetch failure")

    result = evaluate_chatgpt_bundles(
        source,
        manifest,
        fetch_asset=fail,
        fallback_client_version="prod-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        fallback_client_build="1234567",
    )

    assert len(attempted) == MAX_STATIC_ASSETS
    assert result["static_assets_attempted"] == MAX_STATIC_ASSETS
    assert result["status"] == "fetch_failed"


def test_mcp_release_and_specification_date_are_structurally_checked() -> None:
    from scripts.public_surface_radar import SourceSpec, evaluate_source

    sdk = SourceSpec(
        source_id="mcp-python-sdk-release",
        url="https://api.github.com/repos/modelcontextprotocol/python-sdk/releases/latest",
        kind="contract",
        surface="mcp",
        parser="mcp_v1_release",
    )
    spec = SourceSpec(
        source_id="mcp-specification-release",
        url=(
            "https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/releases/latest"
        ),
        kind="contract",
        surface="mcp",
        parser="mcp_spec_release",
    )

    sdk_result = evaluate_source(sdk, b'{"tag_name":"v1.31.0"}')
    spec_result = evaluate_source(spec, b'{"tag_name":"2025-11-25"}')
    unsupported = evaluate_source(sdk, b'{"tag_name":"v2.0.0"}')

    assert sdk_result["observed_value"] == "1.31.0"
    assert spec_result["observed_value"] == "2025-11-25"
    assert unsupported["status"] == "contract_marker_missing"


def test_packaged_fallback_metadata_is_read_from_the_release_source() -> None:
    from scripts.public_surface_radar import _fallback_metadata

    assert _fallback_metadata() == (
        "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad",
        "5955942",
    )


def test_zero_observable_chatgpt_surfaces_is_insufficient_coverage() -> None:
    from scripts.public_surface_radar import (
        RadarError,
        SourceSpec,
        build_report,
        evaluate_source,
        failed_source,
    )

    chatgpt = SourceSpec(
        source_id="chatgpt-release-notes",
        url="https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
        kind="documentation",
        surface="chatgpt",
    )
    codex = SourceSpec(
        source_id="codex-changelog",
        url="https://learn.chatgpt.com/docs/changelog",
        kind="documentation",
        surface="codex",
        markers=("Codex",),
    )
    results = [
        failed_source(chatgpt, RadarError("public fetch failed")),
        evaluate_source(codex, b"<h1>Codex</h1>"),
    ]
    report = build_report(results, observed_at="2026-07-10T13:17:00Z")

    assert report["coverage"] == {
        "expected_sources": 2,
        "observed_sources": 1,
        "failed_sources": 1,
        "hard_failure_sources": 0,
        "chatgpt_expected_sources": 1,
        "chatgpt_observed_sources": 0,
    }
    assert report["coverage_status"] == "insufficient_coverage"
    assert report["exit_code"] == 1


def test_contract_fetch_failure_is_reviewable_coverage_not_marker_loss() -> None:
    from scripts.public_surface_radar import RadarError, SourceSpec, build_report, failed_source

    source = SourceSpec(
        source_id="mcp-python-sdk-release",
        url="https://api.github.com/repos/modelcontextprotocol/python-sdk/releases/latest",
        kind="contract",
        surface="mcp",
    )
    result = failed_source(source, RadarError("synthetic network failure"))
    report = build_report([result], observed_at="2026-07-10T13:17:00Z")

    assert result["status"] == "fetch_failed"
    assert report["coverage_status"] == "partial"
    assert report["coverage"]["hard_failure_sources"] == 0
    assert report["status"] == "review_needed"
    assert report["exit_code"] == 0


def test_report_output_is_deterministic_and_contains_no_downloaded_body() -> None:
    from scripts.public_surface_radar import (
        SourceSpec,
        build_report,
        canonical_json,
        evaluate_source,
    )

    body = b"ChatGPT marker SECRET-DOWNLOADED-BODY"
    source = SourceSpec(
        source_id="release-notes",
        url="https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
        kind="documentation",
        markers=("ChatGPT",),
    )
    result = evaluate_source(source, body)
    report = build_report([result], observed_at="2026-07-10T13:17:00Z")
    encoded = canonical_json(report)

    assert canonical_json(json.loads(encoded)) == encoded
    assert hashlib.sha256(b"ChatGPT marker SECRET-DOWNLOADED-BODY").hexdigest() in encoded
    assert "SECRET-DOWNLOADED-BODY" not in encoded


def test_main_writes_canonical_redacted_evidence_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts import public_surface_radar as radar

    source = radar.SourceSpec(
        source_id="chatgpt-release-notes",
        url="https://help.openai.com/en/articles/6825453-chatgpt-release-notes",
        kind="documentation",
        surface="chatgpt",
        markers=("ChatGPT",),
    )
    json_output = tmp_path / "radar.json"
    markdown_output = tmp_path / "radar.md"
    monkeypatch.setattr(radar, "SOURCES", (source,))
    monkeypatch.setattr(radar, "fetch_source", lambda *_args, **_kwargs: b"ChatGPT notes")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "public_surface_radar.py",
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ],
    )

    assert radar.main() == 0
    encoded = json_output.read_text(encoding="utf-8")
    report = json.loads(encoded)
    assert radar.canonical_json(report) == encoded
    assert report["status"] == "review_needed"
    assert report["coverage"]["chatgpt_observed_sources"] == 1
    assert "ChatGPT notes" not in encoded
    assert "Private adapter status: `not_checked`" in markdown_output.read_text(encoding="utf-8")


def test_public_surface_workflow_is_scheduled_advisory_and_credential_free() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "public-surface-radar.yml").read_text(
        encoding="utf-8"
    )
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'cron: "17 13 * * *"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "contents: read" in workflow
    assert "actions: read" in workflow
    assert "retention-days: 30" in workflow
    assert "secrets." not in workflow
    assert "--previous-report" in workflow
    assert "github.token" in workflow
    assert "public-surface-radar" not in ci
