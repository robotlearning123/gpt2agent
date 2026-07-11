"""Closed-corpus contract tests for installed wheel/sdist adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "installed_adapter_corpus.v1.json"


def test_corpus_covers_each_fixed_adapter_without_dynamic_import_metadata() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert set(payload) == {"schema_version", "cases"}
    assert payload["schema_version"] == "1"
    categories = {case["category"] for case in payload["cases"]}
    assert categories == {
        "chat_models",
        "work_models",
        "apps",
        "plugins",
        "installed_plugins",
        "background_jobs",
        "scheduled_automations",
        "sites_access",
        "site_catalog",
        "custom_gpts",
        "codex",
    }
    assert len({case["id"] for case in payload["cases"]}) == len(payload["cases"])
    assert all(set(case) == {"id", "category", "input", "expected"} for case in payload["cases"])
    serialized = json.dumps(payload, sort_keys=True)
    assert '"module"' not in serialized
    assert '"function"' not in serialized
    assert '"callable"' not in serialized


def test_corpus_output_is_canonical_deterministic_and_all_cases_pass(tmp_path: Path) -> None:
    from scripts.verify_installed_adapter_corpus import canonical_json, verify_corpus

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_result = verify_corpus(FIXTURE, first)
    verify_corpus(FIXTURE, second)

    assert first.read_bytes() == second.read_bytes() == canonical_json(first_result)
    assert first_result["schema_version"] == "1"
    assert first_result["counts"] == {
        "adapters_declared": 11,
        "adapters_exercised": 11,
        "adapters_passed": 11,
        "adapters_not_requested": 0,
        "cases_passed": len(first_result["cases"]),
    }
    assert {case["status"] for case in first_result["cases"]} == {"passed"}


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.update({"module": "gpt2agent.backend"}),
        lambda payload: payload["cases"][0].update({"function": "exec"}),
        lambda payload: payload["cases"][0].update({"category": "unknown"}),
        lambda payload: payload["cases"].append(dict(payload["cases"][0])),
        lambda payload: payload["cases"][0].update({"expected": {"status": "accepted", "value": []}}),
    ),
)
def test_corpus_rejects_schema_dispatch_and_expectation_tampering(
    tmp_path: Path,
    mutation,
) -> None:
    from scripts.verify_installed_adapter_corpus import CorpusError, verify_corpus

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mutation(payload)
    fixture = tmp_path / "mutated.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CorpusError, match="adapter corpus verification failed"):
        verify_corpus(fixture, tmp_path / "out.json")


def test_corpus_rejects_symlink_and_oversize_input(tmp_path: Path) -> None:
    from scripts.verify_installed_adapter_corpus import CorpusError, MAX_CORPUS_BYTES, verify_corpus

    link = tmp_path / "fixture-link.json"
    link.symlink_to(FIXTURE)
    with pytest.raises(CorpusError, match="adapter corpus verification failed"):
        verify_corpus(link, tmp_path / "link-out.json")

    huge = tmp_path / "huge.json"
    huge.write_bytes(b" " * (MAX_CORPUS_BYTES + 1))
    with pytest.raises(CorpusError, match="adapter corpus verification failed"):
        verify_corpus(huge, tmp_path / "huge-out.json")


def test_package_smoke_runs_identical_corpus_with_scrubbed_env_and_hash_guard() -> None:
    script = (PROJECT_ROOT / "scripts" / "package_smoke.sh").read_text(encoding="utf-8")

    assert script.count("verify_installed_adapter_corpus.py") == 1
    assert script.count('"$CORPUS_SCRIPT"') == 2
    assert "installed_adapter_corpus.v1.json" in script
    assert 'cmp -s "$WHEEL_CORPUS" "$SDIST_CORPUS"' in script
    assert "env -i" in script
    assert "PYTHONNOUSERSITE=1" in script
    assert "PYTHONDONTWRITEBYTECODE=1" in script
    assert "DIST_HASHES_BEFORE" in script
    assert "DIST_HASHES_AFTER" in script
    assert 'test "$DIST_HASHES_BEFORE" = "$DIST_HASHES_AFTER"' in script
    assert "GPT2AGENT_RELEASE_APP_TOKEN" not in script
    assert "CODEX_HOME=" not in script
