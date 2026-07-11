"""Trusted-local, exact-commit account receipt contract tests."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def test_request_policy_accepts_only_the_normative_get_allowlist() -> None:
    from scripts.verify_account_receipt import (
        ReceiptError,
        expected_probe,
        validate_probe_request,
    )

    models = validate_probe_request(
        "GET",
        "/backend-api/models",
        {"history_and_training_disabled": "false"},
    )
    assert models.category == "chat_models"
    assert expected_probe("site_catalog").path == "/backend-api/websites"

    rejected = (
        ("POST", "/backend-api/models", {"history_and_training_disabled": "false"}),
        ("GET", "/backend-api/models", {}),
        ("GET", "/backend-api/unknown", {}),
        ("GET", "/backend-api/settings/voices", {}),
        ("GET", "/backend-api/realtime/session", {}),
        ("GET", "/backend-api/call/abc", {}),
        ("GET", "/backend-api/webrtc", {}),
        ("GET", "/backend-api/transcript/abc", {}),
        ("GET", "/backend-api/conversation/secret-id", {}),
        (
            "GET",
            "/backend-api/conversations",
            {"limit": "1", "offset": "0", "order": "updated"},
        ),
        ("GET", "/backend-api/conversations/secret-id", {}),
        ("GET", "/backend-api/memories", {}),
        ("GET", "/backend-api/user_system_messages", {}),
    )
    for method, path, query in rejected:
        with pytest.raises(ReceiptError, match="not permitted"):
            validate_probe_request(method, path, query)


def test_probe_definitions_are_complete_unique_and_voice_free() -> None:
    from scripts.verify_account_receipt import PROBES, _RUNTIME_ADAPTER_CATEGORIES

    expected_categories = {
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
        "projects_candidate",
    }
    assert {probe.category for probe in PROBES} == expected_categories
    assert set(_RUNTIME_ADAPTER_CATEGORIES) == expected_categories - {"projects_candidate"}
    assert len({(probe.path, probe.query) for probe in PROBES}) == len(PROBES)
    serialized = repr(PROBES).lower()
    for denied in ("voice", "realtime", "call", "session", "webrtc", "transcript"):
        assert denied not in serialized


def test_live_receipt_does_not_probe_private_content_routes() -> None:
    from scripts.verify_account_receipt import PROBES

    assert {probe.path for probe in PROBES}.isdisjoint(
        {
            "/backend-api/conversations",
            "/backend-api/memories",
            "/backend-api/user_system_messages",
        }
    )


def _response_for(category: str, url: str, secret: str, *, sites_enabled=True):
    from scripts.verify_account_receipt import RawResponse

    payloads = {
        "chat_models": {"models": [{"slug": secret}]},
        "work_models": {"models": [{"slug": secret}]},
        "apps": {"apps": [{"id": secret}]},
        "plugins": [{"id": secret}],
        "installed_plugins": {
            "plugins": {"results": [{"id": secret}], "page": {"has_more": False}}
        },
        "background_jobs": {"tasks": [{"task_id": secret}]},
        "scheduled_automations": {"items": [{"id": secret}], "cursor": None},
        "sites_access": {
            "enabled": sites_enabled,
            "custom_domains_enabled": False,
            "requires_workspace_slug": False,
            "workspace_slug": secret,
        },
        "site_catalog": {"items": [{"id": secret, "title": secret}], "cursor": None},
        "custom_gpts": {"items": [{"gizmo": {"short_url": secret, "name": secret}}]},
        "codex": {"environments": [{"id": secret, "label": secret}]},
    }
    if category == "projects_candidate":
        return RawResponse(
            status=404,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"detail": secret}).encode(),
            url=url,
        )
    body = json.dumps(payloads[category]).encode()
    return RawResponse(
        status=200,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
        url=url,
    )


def test_probe_response_records_only_fixed_shape_and_count(capsys) -> None:
    from scripts.verify_account_receipt import execute_probe, expected_probe

    secret = "SYNTHETIC-ACCOUNT-CONTENT-7f3b"

    def requester(**request):
        return _response_for("apps", request["url"], secret)

    outcome = execute_probe(
        expected_probe("apps"),
        requester=requester,
        auth_headers={"Authorization": f"Bearer {secret}"},
    )

    assert outcome.receipt_record() == {
        "route_category": "apps",
        "status_class": "2xx",
        "shape": "valid_nonempty",
        "item_count": 1,
        "status": "ok",
    }
    rendered = json.dumps(outcome.receipt_record(), sort_keys=True)
    captured = capsys.readouterr()
    assert secret not in rendered
    assert secret not in captured.out
    assert secret not in captured.err


def test_probe_uses_request_local_target_path_without_mutating_auth_snapshot() -> None:
    from scripts.verify_account_receipt import execute_probe, expected_probe

    original_headers = {"Authorization": "Bearer SYNTHETIC-TOKEN"}
    seen_headers: list[dict[str, str]] = []

    def requester(**request):
        seen_headers.append(request["headers"])
        return _response_for(
            "apps",
            request["url"],
            "SYNTHETIC-APP-ID",
        )

    execute_probe(
        expected_probe("apps"),
        requester=requester,
        auth_headers=original_headers,
    )

    assert seen_headers == [
        {
            "Authorization": "Bearer SYNTHETIC-TOKEN",
            "X-OpenAI-Target-Path": "/backend-api/apps/list",
        }
    ]
    assert original_headers == {"Authorization": "Bearer SYNTHETIC-TOKEN"}


def test_probe_fetch_fails_closed_without_leaking_response_or_redirect_values() -> None:
    from scripts.verify_account_receipt import (
        MAX_RESPONSE_BYTES,
        RawResponse,
        ReceiptError,
        execute_probe,
        expected_probe,
    )

    secret = "SYNTHETIC-SECRET-REDIRECT-9aa1"
    probe = expected_probe("apps")

    bad_responses = (
        RawResponse(
            status=200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(MAX_RESPONSE_BYTES + 1),
            },
            body=b"{}",
            url="https://chatgpt.com/backend-api/apps/list",
        ),
        RawResponse(
            status=302,
            headers={"Location": f"https://evil.example/{secret}"},
            body=b"",
            url="https://chatgpt.com/backend-api/apps/list",
        ),
        RawResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"apps": [{"name": secret}]}).encode(),
            url="https://chatgpt.com/backend-api/apps/list",
        ),
    )
    for raw in bad_responses:
        with pytest.raises(ReceiptError) as caught:
            execute_probe(
                probe,
                requester=lambda **_request: raw,
                auth_headers={"Authorization": f"Bearer {secret}"},
            )
        assert secret not in str(caught.value)


def test_probe_sequence_is_fixed_and_sites_false_skips_only_the_site_catalog() -> None:
    from scripts.verify_account_receipt import PROBES, run_probe_sequence

    secret = "SYNTHETIC-PERSONAL-VALUE-2e8c"
    requested: list[str] = []

    def requester(**request):
        from scripts.verify_account_receipt import probe_from_url

        probe = probe_from_url(request["url"])
        requested.append(probe.category)
        return _response_for(
            probe.category,
            request["url"],
            secret,
            sites_enabled=False,
        )

    records = run_probe_sequence(
        requester=requester,
        auth_headers={"Authorization": f"Bearer {secret}"},
    )

    assert requested == [probe.category for probe in PROBES if probe.category != "site_catalog"]
    assert [record["route_category"] for record in records] == [probe.category for probe in PROBES]
    site_catalog = next(record for record in records if record["route_category"] == "site_catalog")
    assert site_catalog == {
        "route_category": "site_catalog",
        "status_class": "not_requested",
        "shape": "not_requested",
        "item_count": None,
        "status": "not_requested",
    }
    assert secret not in json.dumps(records, sort_keys=True)


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _create_checkout(tmp_path: Path) -> tuple[Path, str, str]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.email", "receipt-tests@example.invalid")
    _git(checkout, "config", "user.name", "Receipt Tests")
    (checkout / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "gpt2agent"\nversion = "0.0.12"\n',
        encoding="utf-8",
    )
    (checkout / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "--quiet", "-m", "fixture")
    return checkout, _git(checkout, "rev-parse", "HEAD"), _git(checkout, "rev-parse", "HEAD^{tree}")


def _create_artifacts(parent: Path) -> Path:
    dist = parent / "candidate-dist"
    dist.mkdir()
    (dist / "gpt2agent-0.0.12-py3-none-any.whl").write_bytes(b"wheel candidate")
    (dist / "gpt2agent-0.0.12.tar.gz").write_bytes(b"sdist candidate")
    return dist


def _all_valid_records(secret: str, *, sites_enabled: bool = True) -> list[dict]:
    from scripts.verify_account_receipt import run_probe_sequence

    def requester(**request):
        from scripts.verify_account_receipt import probe_from_url

        probe = probe_from_url(request["url"])
        return _response_for(
            probe.category,
            request["url"],
            secret,
            sites_enabled=sites_enabled,
        )

    return run_probe_sequence(
        requester=requester,
        auth_headers={"Authorization": f"Bearer {secret}"},
    )


def _all_adapter_counts() -> dict[str, int]:
    return {
        "adapters_declared": 11,
        "adapters_exercised": 11,
        "adapters_passed": 11,
        "adapters_not_requested": 0,
    }


def _create_receipt_fixture(tmp_path: Path, *, secret: str = "SYNTHETIC-SECRET"):
    from scripts.verify_account_receipt import (
        build_receipt,
        collect_local_candidate_artifacts,
        write_receipt,
    )

    checkout, commit, tree = _create_checkout(tmp_path)
    dist = _create_artifacts(tmp_path)
    artifacts = collect_local_candidate_artifacts(
        dist,
        package_version="0.0.12",
        source_commit=commit,
        source_tree=tree,
        repository="robotlearning123/gpt2agent",
        run_id="12345",
        run_attempt="2",
        artifact_id="67890",
        artifact_digest="sha256:" + "a" * 64,
        artifact_size="31415",
        artifact_expires_at="2099-07-10T13:17:42Z",
    )
    completed = datetime.now(timezone.utc)
    started = completed - timedelta(seconds=1)
    receipt = build_receipt(
        package_version="0.0.12",
        plan_class="pro",
        started_at=started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        completed_at=completed.isoformat(timespec="seconds").replace("+00:00", "Z"),
        source_commit=commit,
        source_tree=tree,
        local_candidate_artifacts=artifacts,
        adapter_status="passed",
        adapter_counts=_all_adapter_counts(),
        shape_results=_all_valid_records(secret),
    )
    receipt_path = tmp_path / "account-receipt.json"
    digest = write_receipt(receipt_path, receipt)
    return checkout, dist, commit, tree, receipt, receipt_path, digest


def test_receipt_is_closed_canonical_secret_free_and_sha256_bound(tmp_path: Path, capsys) -> None:
    from scripts.verify_account_receipt import ReceiptError, canonical_json, validate_receipt

    secret = "SYNTHETIC-ACCOUNT-SECRET-41d8"
    _checkout, _dist, _commit, _tree, receipt, receipt_path, digest = _create_receipt_fixture(
        tmp_path, secret=secret
    )

    assert receipt_path.read_bytes() == canonical_json(receipt)
    assert len(digest) == 64
    assert secret not in receipt_path.read_text(encoding="utf-8")
    assert set(receipt) == {
        "schema_version",
        "verifier",
        "package_version",
        "plan_class",
        "started_at",
        "completed_at",
        "source",
        "local_candidate_artifacts",
        "adapter_status",
        "counts",
        "shape_results",
    }
    assert receipt["schema_version"] == "4"
    assert receipt["verifier"] == {
        "name": "gpt2agent-account-receipt",
        "version": "5",
    }
    assert receipt["adapter_status"] == "passed"
    assert {
        key: receipt["counts"][key]
        for key in (
            "adapters_declared",
            "adapters_exercised",
            "adapters_passed",
            "adapters_not_requested",
        )
    } == _all_adapter_counts()
    assert "items_observed" not in receipt["counts"]
    assert all("item_count" not in result for result in receipt["shape_results"])
    assert validate_receipt(receipt) is None
    rendered = receipt_path.read_text(encoding="utf-8")
    assert "https://" not in rendered
    assert "/backend-api/" not in rendered

    poisoned = dict(receipt)
    poisoned["raw_account_value"] = secret
    with pytest.raises(ReceiptError) as caught:
        validate_receipt(poisoned)
    assert secret not in str(caught.value)
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_checkout_validation_rejects_wrong_or_dirty_state_without_name_leak(
    tmp_path: Path,
) -> None:
    from scripts.verify_account_receipt import ReceiptError, verify_checkout

    checkout, commit, tree = _create_checkout(tmp_path)
    assert verify_checkout(checkout, declared_commit=commit, declared_tree=tree) == (
        commit,
        tree,
    )

    ignored_dist = checkout / "dist"
    ignored_dist.mkdir()
    (ignored_dist / "stale-wheel.whl").write_bytes(b"stale")
    with pytest.raises(ReceiptError, match="clean"):
        verify_checkout(checkout, declared_commit=commit, declared_tree=tree)
    (ignored_dist / "stale-wheel.whl").unlink()
    ignored_dist.rmdir()

    with pytest.raises(ReceiptError, match="declared source"):
        verify_checkout(checkout, declared_commit="f" * 40, declared_tree=tree)
    with pytest.raises(ReceiptError, match="declared source"):
        verify_checkout(checkout, declared_commit=commit, declared_tree="e" * 40)

    secret_name = "SYNTHETIC-SECRET-FILENAME-612c.txt"
    (checkout / secret_name).write_text("private", encoding="utf-8")
    with pytest.raises(ReceiptError) as caught:
        verify_checkout(checkout, declared_commit=commit, declared_tree=tree)
    assert secret_name not in str(caught.value)


def test_verify_receipt_invalidates_later_artifact_or_source_change(tmp_path: Path) -> None:
    from scripts.verify_account_receipt import ReceiptError, verify_receipt_file

    checkout, dist, commit, tree, _receipt, receipt_path, digest = _create_receipt_fixture(tmp_path)
    assert (
        verify_receipt_file(
            receipt_path,
            checkout=checkout,
            dist=dist,
            declared_commit=commit,
            declared_tree=tree,
            expected_sha256=digest,
            ci_repository="robotlearning123/gpt2agent",
            ci_run_id="12345",
            ci_run_attempt="2",
            ci_artifact_id="67890",
            ci_artifact_digest="sha256:" + "a" * 64,
            ci_artifact_size="31415",
            ci_artifact_expires_at="2099-07-10T13:17:42Z",
        )
        == digest
    )

    wheel = dist / "gpt2agent-0.0.12-py3-none-any.whl"
    original_wheel = wheel.read_bytes()
    wheel.write_bytes(original_wheel + b"changed")
    with pytest.raises(ReceiptError, match="artifact"):
        verify_receipt_file(
            receipt_path,
            checkout=checkout,
            dist=dist,
            declared_commit=commit,
            declared_tree=tree,
            expected_sha256=digest,
            ci_repository="robotlearning123/gpt2agent",
            ci_run_id="12345",
            ci_run_attempt="2",
            ci_artifact_id="67890",
            ci_artifact_digest="sha256:" + "a" * 64,
            ci_artifact_size="31415",
            ci_artifact_expires_at="2099-07-10T13:17:42Z",
        )
    wheel.write_bytes(original_wheel)

    extra_name = "SYNTHETIC-SECRET-EXTRA-ARTIFACT.txt"
    (dist / extra_name).write_text("unexpected", encoding="utf-8")
    with pytest.raises(ReceiptError, match="artifact") as caught:
        verify_receipt_file(
            receipt_path,
            checkout=checkout,
            dist=dist,
            declared_commit=commit,
            declared_tree=tree,
            expected_sha256=digest,
            ci_repository="robotlearning123/gpt2agent",
            ci_run_id="12345",
            ci_run_attempt="2",
            ci_artifact_id="67890",
            ci_artifact_digest="sha256:" + "a" * 64,
            ci_artifact_size="31415",
            ci_artifact_expires_at="2099-07-10T13:17:42Z",
        )
    assert extra_name not in str(caught.value)
    (dist / extra_name).unlink()

    (checkout / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(ReceiptError, match="clean"):
        verify_receipt_file(
            receipt_path,
            checkout=checkout,
            dist=dist,
            declared_commit=commit,
            declared_tree=tree,
            expected_sha256=digest,
            ci_repository="robotlearning123/gpt2agent",
            ci_run_id="12345",
            ci_run_attempt="2",
            ci_artifact_id="67890",
            ci_artifact_digest="sha256:" + "a" * 64,
            ci_artifact_size="31415",
            ci_artifact_expires_at="2099-07-10T13:17:42Z",
        )


def test_fresh_receipt_verification_rejects_actions_artifact_expiry(
    tmp_path: Path,
) -> None:
    from scripts.verify_account_receipt import verify_receipt_file, write_receipt

    checkout, dist, commit, tree, receipt, _receipt_path, _digest = _create_receipt_fixture(
        tmp_path
    )
    expired_at = "2020-01-01T00:00:00Z"
    receipt["local_candidate_artifacts"]["workflow"]["artifact_expires_at"] = expired_at
    historical_receipt = tmp_path / "historical-account-receipt.json"
    digest = write_receipt(historical_receipt, receipt)

    with pytest.raises(ValueError, match="expired"):
        verify_receipt_file(
            historical_receipt,
            checkout=checkout,
            dist=dist,
            declared_commit=commit,
            declared_tree=tree,
            expected_sha256=digest,
            ci_repository="robotlearning123/gpt2agent",
            ci_run_id="12345",
            ci_run_attempt="2",
            ci_artifact_id="67890",
            ci_artifact_digest="sha256:" + "a" * 64,
            ci_artifact_size="31415",
            ci_artifact_expires_at=expired_at,
        )


@pytest.mark.parametrize("timing", ("stale", "future", "too-long"))
def test_verify_receipt_rejects_replayed_or_implausible_timestamps(
    tmp_path: Path,
    timing: str,
) -> None:
    from scripts.verify_account_receipt import ReceiptError, verify_receipt_file, write_receipt

    checkout, dist, commit, tree, receipt, _path, _digest = _create_receipt_fixture(tmp_path)
    now = datetime.now(timezone.utc)
    if timing == "stale":
        completed = now - timedelta(minutes=31)
        started = completed - timedelta(seconds=1)
    elif timing == "future":
        completed = now + timedelta(minutes=2)
        started = completed - timedelta(seconds=1)
    else:
        completed = now
        started = completed - timedelta(minutes=11)
    receipt["started_at"] = started.isoformat(timespec="seconds").replace("+00:00", "Z")
    receipt["completed_at"] = completed.isoformat(timespec="seconds").replace("+00:00", "Z")
    path = tmp_path / f"{timing}-receipt.json"
    digest = write_receipt(path, receipt)

    with pytest.raises(ReceiptError, match="freshness"):
        verify_receipt_file(
            path,
            checkout=checkout,
            dist=dist,
            declared_commit=commit,
            declared_tree=tree,
            expected_sha256=digest,
            ci_repository="robotlearning123/gpt2agent",
            ci_run_id="12345",
            ci_run_attempt="2",
            ci_artifact_id="67890",
            ci_artifact_digest="sha256:" + "a" * 64,
            ci_artifact_size="31415",
            ci_artifact_expires_at="2099-07-10T13:17:42Z",
        )


def test_verify_receipt_rejects_noncanonical_or_wrong_digest_without_value_leak(
    tmp_path: Path,
) -> None:
    from scripts.verify_account_receipt import ReceiptError, verify_receipt_file

    secret = "SYNTHETIC-RECEIPT-" + "SECRET-3ac7"
    checkout, dist, commit, tree, receipt, receipt_path, digest = _create_receipt_fixture(
        tmp_path, secret=secret
    )
    with pytest.raises(ReceiptError, match="digest") as caught:
        verify_receipt_file(
            receipt_path,
            checkout=checkout,
            dist=dist,
            declared_commit=commit,
            declared_tree=tree,
            expected_sha256="0" * 64,
            ci_repository="robotlearning123/gpt2agent",
            ci_run_id="12345",
            ci_run_attempt="2",
            ci_artifact_id="67890",
            ci_artifact_digest="sha256:" + "a" * 64,
            ci_artifact_size="31415",
            ci_artifact_expires_at="2099-07-10T13:17:42Z",
        )
    assert secret not in str(caught.value)

    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    with pytest.raises(ReceiptError, match="canonical") as caught:
        verify_receipt_file(
            receipt_path,
            checkout=checkout,
            dist=dist,
            declared_commit=commit,
            declared_tree=tree,
            expected_sha256=digest,
            ci_repository="robotlearning123/gpt2agent",
            ci_run_id="12345",
            ci_run_attempt="2",
            ci_artifact_id="67890",
            ci_artifact_digest="sha256:" + "a" * 64,
            ci_artifact_size="31415",
            ci_artifact_expires_at="2099-07-10T13:17:42Z",
        )
    assert secret not in str(caught.value)


def test_create_gate_tests_existing_main_ci_artifacts_before_probe(tmp_path: Path) -> None:
    from scripts.verify_account_receipt import (
        canonical_json,
        run_create_gate,
        validate_receipt,
    )

    checkout, commit, tree = _create_checkout(tmp_path)
    inside_dist = checkout / "dist"
    dist = _create_artifacts(tmp_path)
    output = tmp_path / "created-account-receipt.json"
    events: list[str] = []

    def probe_runner(expected_plan: str) -> dict:
        events.append("trusted-probe")
        assert expected_plan == "pro"
        completed = datetime.now(timezone.utc)
        started = completed - timedelta(seconds=1)
        return {
            "schema_version": "4",
            "package_version": "0.0.12",
            "plan_class": "pro",
            "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "completed_at": completed.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "shape_results": _all_valid_records("SYNTHETIC-SECRET-IN-PROBE"),
        }

    with pytest.raises(ValueError, match="outside the checkout"):
        run_create_gate(
            checkout=checkout,
            dist=inside_dist,
            output=output,
            declared_commit=commit,
            declared_tree=tree,
            expected_plan="pro",
            ci_repository="robotlearning123/gpt2agent",
            ci_run_id="12345",
            ci_run_attempt="2",
            ci_artifact_id="67890",
            ci_artifact_digest="sha256:" + "a" * 64,
            ci_artifact_size="31415",
            ci_artifact_expires_at="2099-07-10T13:17:42Z",
            trusted_probe_runner=probe_runner,
        )
    assert events == []

    digest = run_create_gate(
        checkout=checkout,
        dist=dist,
        output=output,
        declared_commit=commit,
        declared_tree=tree,
        expected_plan="pro",
        ci_repository="robotlearning123/gpt2agent",
        ci_run_id="12345",
        ci_run_attempt="2",
        ci_artifact_id="67890",
        ci_artifact_digest="sha256:" + "a" * 64,
        ci_artifact_size="31415",
        ci_artifact_expires_at="2099-07-10T13:17:42Z",
        trusted_probe_runner=probe_runner,
    )

    assert events == ["trusted-probe"]
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert output.read_bytes() == canonical_json(receipt)
    assert len(digest) == 64
    assert validate_receipt(receipt) is None
    assert receipt["counts"]["adapters_passed"] == 11
    assert receipt["local_candidate_artifacts"]["build_origin"] == "main_ci_package_artifact"
    assert receipt["local_candidate_artifacts"]["workflow"] == {
        "artifact_digest": "sha256:" + "a" * 64,
        "artifact_expires_at": "2099-07-10T13:17:42Z",
        "artifact_id": 67890,
        "artifact_name": f"release-candidate-{commit}-12345-2",
        "artifact_size": 31415,
        "event": "push",
        "job": "package",
        "ref": "refs/heads/main",
        "repository": "robotlearning123/gpt2agent",
        "run_attempt": 2,
        "run_id": 12345,
        "workflow_file": ".github/workflows/ci.yml",
    }
    assert "SYNTHETIC-SECRET-IN-PROBE" not in output.read_text(encoding="utf-8")


def test_create_gate_rejects_receipt_nested_under_not_yet_created_dist(
    tmp_path: Path,
) -> None:
    from scripts.verify_account_receipt import ReceiptError, run_create_gate

    checkout, commit, tree = _create_checkout(tmp_path)
    dist = tmp_path / "not-yet-created-dist"
    output = dist / "account-receipt.json"
    with pytest.raises(
        ReceiptError,
        match="outside the candidate artifact directory",
    ):
        run_create_gate(
            checkout=checkout,
            dist=dist,
            output=output,
            declared_commit=commit,
            declared_tree=tree,
            expected_plan="pro",
            ci_repository="robotlearning123/gpt2agent",
            ci_run_id="12345",
            ci_run_attempt="2",
            ci_artifact_id="67890",
            ci_artifact_digest="sha256:" + "a" * 64,
            ci_artifact_size="31415",
            ci_artifact_expires_at="2099-07-10T13:17:42Z",
        )


def test_account_artifact_binding_rejects_expired_ci_artifact(tmp_path: Path) -> None:
    from scripts.verify_account_receipt import (
        ReceiptError,
        collect_local_candidate_artifacts,
    )

    dist = _create_artifacts(tmp_path)
    with pytest.raises(ReceiptError, match="expired"):
        collect_local_candidate_artifacts(
            dist,
            package_version="0.0.12",
            source_commit="a" * 40,
            source_tree="b" * 40,
            repository="robotlearning123/gpt2agent",
            run_id="12345",
            run_attempt="2",
            artifact_id="67890",
            artifact_digest="sha256:" + "a" * 64,
            artifact_size="31415",
            artifact_expires_at="2020-01-01T00:00:00Z",
        )


def test_account_artifact_binding_requires_pretag_retention_headroom(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta, timezone

    from scripts.verify_account_receipt import (
        ReceiptError,
        collect_local_candidate_artifacts,
    )

    dist = _create_artifacts(tmp_path)
    expires_soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    with pytest.raises(ReceiptError, match="expires too soon"):
        collect_local_candidate_artifacts(
            dist,
            package_version="0.0.12",
            source_commit="a" * 40,
            source_tree="b" * 40,
            repository="robotlearning123/gpt2agent",
            run_id="12345",
            run_attempt="2",
            artifact_id="67890",
            artifact_digest="sha256:" + "a" * 64,
            artifact_size="31415",
            artifact_expires_at=expires_soon,
        )


def test_streaming_requester_stops_at_declared_oversize_and_closes_response() -> None:
    from scripts.verify_account_receipt import (
        MAX_RESPONSE_BYTES,
        CurlCffiRequester,
        ReceiptError,
        _trusted_headers,
    )

    secret = "SYNTHETIC-TRANSPORT-SECRET-9f11"

    class Response:
        status_code = 200
        url = "https://chatgpt.com/backend-api/apps/list"
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(MAX_RESPONSE_BYTES + 1),
            "Set-Cookie": secret,
        }
        closed = False
        iterated = False

        def iter_content(self, chunk_size: int):
            del chunk_size
            self.iterated = True
            yield secret.encode()

        def close(self):
            self.closed = True

    response = Response()

    class Session:
        def get(self, *_args, **_kwargs):
            return response

    requester = CurlCffiRequester(Session())
    headers = _trusted_headers("eyJsynthetic.header.signature")
    headers["X-OpenAI-Target-Path"] = "/backend-api/apps/list"
    with pytest.raises(ReceiptError) as caught:
        requester(
            method="GET",
            url=response.url,
            headers=headers,
            timeout=20,
            max_bytes=MAX_RESPONSE_BYTES,
        )
    assert response.closed is True
    assert response.iterated is False
    assert secret not in str(caught.value)


def test_cli_failure_never_prints_synthetic_receipt_values(tmp_path: Path, capsys) -> None:
    from scripts.verify_account_receipt import canonical_json, main

    secret = "SYNTHETIC-CLI-SECRET-0c31"
    receipt_path = tmp_path / "invalid-receipt.json"
    payload = canonical_json({"unexpected_account_value": secret})
    receipt_path.write_bytes(payload)

    exit_code = main(
        [
            "verify",
            "--receipt",
            str(receipt_path),
            "--checkout",
            str(tmp_path / "checkout"),
            "--dist",
            str(tmp_path / "dist"),
            "--commit",
            "a" * 40,
            "--tree",
            "b" * 40,
            "--sha256",
            __import__("hashlib").sha256(payload).hexdigest(),
            "--repository",
            "robotlearning123/gpt2agent",
            "--ci-run-id",
            "12345",
            "--ci-run-attempt",
            "2",
            "--ci-artifact-id",
            "67890",
            "--ci-artifact-digest",
            "sha256:" + "a" * 64,
            "--ci-artifact-size",
            "31415",
            "--ci-artifact-expires-at",
            "2099-07-10T13:17:42Z",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert secret not in captured.out
    assert secret not in captured.err
