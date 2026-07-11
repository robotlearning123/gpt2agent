"""Offline tests for the read-only GitHub release-governance audit."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "release_governance_pass.json"
AUDITOR = PROJECT_ROOT / "scripts" / "audit_release_governance.py"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _snapshot() -> dict:
    snapshot = _fixture()
    snapshot.pop("policy")
    return snapshot


def _policy() -> dict:
    return _fixture()["policy"]


def _checks(report: dict) -> dict[str, str]:
    return {check["id"]: check["status"] for check in report["checks"]}


def test_complete_fixture_passes_every_governance_check() -> None:
    from scripts.audit_release_governance import audit_snapshot

    report = audit_snapshot(_snapshot(), _policy())

    assert report["schema_version"] == 1
    assert report["status"] == "pass"
    assert _checks(report) == {
        "policy_identity_bound": "pass",
        "tag_creation_restricted": "pass",
        "tag_immutable": "pass",
        "tag_bypass_actor_narrow": "pass",
        "pypi_independent_gate": "pass",
        "pypi_prevent_self_review": "pass",
        "pypi_admin_bypass_disabled": "pass",
        "pypi_v_tags_only": "pass",
        "main_one_approval": "pass",
        "main_stale_reviews_dismissed": "pass",
        "main_threads_resolved": "pass",
        "main_required_checks": "pass",
        "main_strict_required_checks": "pass",
        "main_bypass_disabled": "pass",
    }


@pytest.mark.parametrize(
    ("mutation", "failed_check"),
    (
        ("remove_creation", "tag_creation_restricted"),
        ("remove_deletion", "tag_immutable"),
        ("remove_non_fast_forward", "tag_immutable"),
        ("remove_update", "tag_immutable"),
        ("add_immutability_bypass", "tag_immutable"),
        ("malformed_immutability_bypass", "tag_immutable"),
        ("broad_tag_bypass", "tag_bypass_actor_narrow"),
        ("self_reviewer", "pypi_independent_gate"),
        ("mixed_owner_reviewer", "pypi_independent_gate"),
        ("allow_self_review", "pypi_prevent_self_review"),
        ("allow_admin_bypass", "pypi_admin_bypass_disabled"),
        ("allow_release_branch", "pypi_v_tags_only"),
        ("remove_main_approval", "main_one_approval"),
        ("keep_stale_reviews", "main_stale_reviews_dismissed"),
        ("allow_unresolved_threads", "main_threads_resolved"),
        ("remove_required_checks", "main_required_checks"),
        ("wrong_required_check_app", "main_required_checks"),
        ("remove_required_checks_strictness", "main_strict_required_checks"),
        ("add_main_bypass", "main_bypass_disabled"),
        ("malformed_main_bypass", "main_bypass_disabled"),
    ),
)
def test_each_missing_control_fails_closed(mutation: str, failed_check: str) -> None:
    from scripts.audit_release_governance import audit_snapshot

    snapshot = copy.deepcopy(_snapshot())
    creation_ruleset, immutability_ruleset, main_ruleset = snapshot["rulesets"]
    reviewer_rule = snapshot["environment"]["protection_rules"][0]
    if mutation == "remove_creation":
        creation_ruleset["rules"] = [
            rule for rule in creation_ruleset["rules"] if rule["type"] != "creation"
        ]
    elif mutation in {"remove_deletion", "remove_non_fast_forward", "remove_update"}:
        removed_type = mutation.removeprefix("remove_")
        immutability_ruleset["rules"] = [
            rule for rule in immutability_ruleset["rules"] if rule["type"] != removed_type
        ]
    elif mutation == "add_immutability_bypass":
        immutability_ruleset["bypass_actors"] = [
            {"actor_id": 101, "actor_type": "Integration", "bypass_mode": "always"}
        ]
    elif mutation == "malformed_immutability_bypass":
        immutability_ruleset["bypass_actors"] = {"unexpected": "shape"}
    elif mutation == "broad_tag_bypass":
        creation_ruleset["bypass_actors"] = [
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
        ]
    elif mutation == "self_reviewer":
        reviewer_rule["reviewers"][0]["reviewer"]["login"] = "example"
    elif mutation == "mixed_owner_reviewer":
        reviewer_rule["reviewers"].append(
            {"reviewer": {"id": 42, "login": "example"}, "type": "User"}
        )
    elif mutation == "allow_self_review":
        reviewer_rule["prevent_self_review"] = False
    elif mutation == "allow_admin_bypass":
        snapshot["environment"]["can_admins_bypass"] = True
    elif mutation == "allow_release_branch":
        snapshot["deployment_branch_policies"]["branch_policies"].append(
            {"id": 302, "name": "release/*", "type": "branch"}
        )
        snapshot["deployment_branch_policies"]["total_count"] = 2
    elif mutation == "remove_main_approval":
        main_ruleset["rules"][0]["parameters"]["required_approving_review_count"] = 0
    elif mutation == "keep_stale_reviews":
        main_ruleset["rules"][0]["parameters"]["dismiss_stale_reviews_on_push"] = False
    elif mutation == "allow_unresolved_threads":
        main_ruleset["rules"][0]["parameters"]["required_review_thread_resolution"] = False
    elif mutation == "remove_required_checks":
        main_ruleset["rules"] = [
            rule for rule in main_ruleset["rules"] if rule["type"] != "required_status_checks"
        ]
    elif mutation == "wrong_required_check_app":
        main_ruleset["rules"][1]["parameters"]["required_status_checks"][0]["integration_id"] = 999
    elif mutation == "remove_required_checks_strictness":
        main_ruleset["rules"][1]["parameters"]["strict_required_status_checks_policy"] = False
    elif mutation == "add_main_bypass":
        main_ruleset["bypass_actors"] = [
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
        ]
    elif mutation == "malformed_main_bypass":
        main_ruleset["bypass_actors"] = {"unexpected": "shape"}

    report = audit_snapshot(snapshot, _policy())

    assert report["status"] == "fail"
    assert _checks(report)[failed_check] == "fail"


def test_release_app_bypass_cannot_also_bypass_tag_immutability() -> None:
    from scripts.audit_release_governance import audit_snapshot

    snapshot = _snapshot()
    creation_ruleset = snapshot["rulesets"][0]
    creation_ruleset["rules"].extend(
        [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "update"},
        ]
    )
    snapshot["rulesets"].pop(1)

    report = audit_snapshot(snapshot, _policy())

    assert _checks(report)["tag_creation_restricted"] == "pass"
    assert _checks(report)["tag_bypass_actor_narrow"] == "pass"
    assert _checks(report)["tag_immutable"] == "fail"


def test_expected_custom_protection_app_is_an_independent_gate() -> None:
    from scripts.audit_release_governance import audit_snapshot

    snapshot = _snapshot()
    policy = _policy()
    policy["pypi_gate"] = {
        "id": 501,
        "kind": "protection_app",
        "slug": "release-gate",
    }
    snapshot["environment"]["protection_rules"] = [{"id": 202, "type": "branch_policy"}]
    snapshot["custom_deployment_protection_rules"] = {
        "total_count": 1,
        "custom_deployment_protection_rules": [
            {"id": 401, "enabled": True, "app": {"id": 501, "slug": "release-gate"}}
        ],
    }

    checks = _checks(audit_snapshot(snapshot, policy))

    assert checks["pypi_independent_gate"] == "pass"
    assert checks["pypi_prevent_self_review"] == "pass"


def test_release_app_cannot_also_be_the_pypi_protection_gate() -> None:
    from scripts.audit_release_governance import audit_snapshot

    snapshot = _snapshot()
    policy = _policy()
    policy["pypi_gate"] = {
        "id": 101,
        "kind": "protection_app",
        "slug": "release-gate",
    }
    snapshot["environment"]["protection_rules"] = [{"id": 202, "type": "branch_policy"}]
    snapshot["custom_deployment_protection_rules"] = {
        "total_count": 1,
        "custom_deployment_protection_rules": [
            {"id": 401, "enabled": True, "app": {"id": 101, "slug": "release-gate"}}
        ],
    }

    report = audit_snapshot(snapshot, policy)

    assert report["status"] == "fail"
    assert _checks(report)["policy_identity_bound"] == "fail"
    assert _checks(report)["pypi_independent_gate"] == "fail"


def test_expected_reviewer_does_not_require_an_unreviewed_numeric_id() -> None:
    from scripts.audit_release_governance import audit_snapshot

    snapshot = _snapshot()
    snapshot["environment"]["protection_rules"][0]["reviewers"][0]["reviewer"].pop("id")

    report = audit_snapshot(snapshot, _policy())

    assert report["status"] == "pass"


def test_arbitrary_custom_protection_app_cannot_satisfy_reviewed_policy() -> None:
    from scripts.audit_release_governance import audit_snapshot

    snapshot = _snapshot()
    policy = _policy()
    policy["pypi_gate"] = {
        "id": 999,
        "kind": "protection_app",
        "slug": "reviewed-release-gate",
    }
    snapshot["environment"]["protection_rules"] = [{"id": 202, "type": "branch_policy"}]
    snapshot["custom_deployment_protection_rules"] = {
        "total_count": 1,
        "custom_deployment_protection_rules": [
            {"id": 401, "enabled": True, "app": {"id": 501, "slug": "arbitrary-app"}}
        ],
    }

    report = audit_snapshot(snapshot, policy)

    assert report["status"] == "fail"
    assert _checks(report)["pypi_independent_gate"] == "fail"


def test_wrong_release_integration_id_cannot_satisfy_reviewed_policy() -> None:
    from scripts.audit_release_governance import audit_snapshot

    snapshot = _snapshot()
    snapshot["rulesets"][0]["bypass_actors"][0]["actor_id"] = 999

    report = audit_snapshot(snapshot, _policy())

    assert report["status"] == "fail"
    assert _checks(report)["tag_bypass_actor_narrow"] == "fail"


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_release_app",
        "missing_required_check_app",
        "missing_reviewer",
        "repository_mismatch",
    ),
)
def test_incomplete_or_mismatched_policy_fails_closed(mutation: str) -> None:
    from scripts.audit_release_governance import audit_snapshot

    policy = _policy()
    if mutation == "missing_release_app":
        policy["release_tag_app"] = {}
    elif mutation == "missing_required_check_app":
        policy["required_check_app"] = {}
    elif mutation == "missing_reviewer":
        policy["pypi_gate"] = {"kind": "reviewer"}
    elif mutation == "repository_mismatch":
        policy["repository"] = "different/gpt2agent"

    report = audit_snapshot(_snapshot(), policy)

    assert report["status"] == "fail"
    assert _checks(report)["policy_identity_bound"] == "fail"


def test_cli_output_is_deterministic_machine_readable_and_secret_safe(tmp_path: Path) -> None:
    first = subprocess.run(
        [sys.executable, str(AUDITOR), "--snapshot", str(FIXTURE)],
        capture_output=True,
        text=True,
        check=False,
    )
    second = subprocess.run(
        [sys.executable, str(AUDITOR), "--snapshot", str(FIXTURE)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["status"] == "pass"
    assert first.stderr == ""

    secret = "SYNTHETIC-GOVERNANCE-SECRET-7f3b"
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({"rulesets": secret}), encoding="utf-8")
    failed = subprocess.run(
        [sys.executable, str(AUDITOR), "--snapshot", str(malformed)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert failed.returncode == 1
    assert json.loads(failed.stdout)["status"] == "fail"
    assert secret not in failed.stdout
    assert secret not in failed.stderr


def test_live_cli_requires_reviewed_policy_before_fetching(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.audit_release_governance as governance

    def unexpected_fetch(_repository: str) -> dict:
        pytest.fail("live snapshot was fetched before policy validation")

    monkeypatch.setattr(governance, "fetch_live_snapshot", unexpected_fetch)

    result = governance.main(["--live", "example/gpt2agent"])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "--policy" in captured.err


def test_live_cli_rejects_policy_for_another_repository_before_fetching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.audit_release_governance as governance

    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(_policy()), encoding="utf-8")

    def unexpected_fetch(_repository: str) -> dict:
        pytest.fail("live snapshot was fetched with a mismatched policy")

    monkeypatch.setattr(governance, "fetch_live_snapshot", unexpected_fetch)

    result = governance.main(["--live", "different/gpt2agent", "--policy", str(policy_path)])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "repository" in captured.err


def test_cli_rejects_duplicate_keys_in_reviewed_policy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.audit_release_governance as governance

    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        '{"schema_version":1,"repository":"example/gpt2agent",'
        '"release_tag_app":{"id":101},"release_tag_app":{"id":999},'
        '"required_check_app":{"id":202},'
        '"pypi_gate":{"kind":"reviewer","login":"release-reviewer"}}',
        encoding="utf-8",
    )

    result = governance.main(
        ["--snapshot", str(FIXTURE), "--policy", str(policy_path)]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "governance policy is invalid" in captured.err


def test_cli_rejects_duplicate_keys_in_governance_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.audit_release_governance as governance

    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        '{"schema_version":1,"schema_version":2}', encoding="utf-8"
    )

    result = governance.main(["--snapshot", str(snapshot_path)])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "governance fixture is invalid" in captured.err


def test_live_snapshot_fetches_only_reviewed_read_endpoints() -> None:
    from scripts.audit_release_governance import fetch_live_snapshot

    responses = {
        "repos/example/gpt2agent": {
            "full_name": "example/gpt2agent",
            "default_branch": "main",
        },
        "repos/example/gpt2agent/rulesets?includes_parents=true&per_page=100": [
            {"id": 1},
            {"id": 2},
        ],
        "repos/example/gpt2agent/rulesets/1": {"id": 1, "target": "tag"},
        "repos/example/gpt2agent/rulesets/2": {"id": 2, "target": "branch"},
        "repos/example/gpt2agent/environments/pypi": {"name": "pypi"},
        "repos/example/gpt2agent/environments/pypi/deployment-branch-policies?per_page=100": {
            "total_count": 0,
            "branch_policies": [],
        },
        "repos/example/gpt2agent/environments/pypi/deployment_protection_rules?per_page=100": {
            "total_count": 0,
            "custom_deployment_protection_rules": [],
        },
    }
    calls: list[str] = []

    def requester(endpoint: str):
        calls.append(endpoint)
        return responses[endpoint]

    snapshot = fetch_live_snapshot("example/gpt2agent", requester=requester)

    assert calls == list(responses)
    assert snapshot["repository"] == responses["repos/example/gpt2agent"]
    assert snapshot["rulesets"] == [
        responses["repos/example/gpt2agent/rulesets/1"],
        responses["repos/example/gpt2agent/rulesets/2"],
    ]


def test_live_snapshot_rejects_malformed_ruleset_ids_without_echoing_values() -> None:
    from scripts.audit_release_governance import GovernanceError, fetch_live_snapshot

    secret = "SYNTHETIC-RULESET-VALUE-9aa1"

    def requester(endpoint: str):
        if endpoint == "repos/example/gpt2agent":
            return {"full_name": "example/gpt2agent", "default_branch": "main"}
        return [{"id": {"secret": secret}}]

    with pytest.raises(GovernanceError) as caught:
        fetch_live_snapshot("example/gpt2agent", requester=requester)

    assert secret not in str(caught.value)
