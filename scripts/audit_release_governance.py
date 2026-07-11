#!/usr/bin/env python3
"""Audit the read-only GitHub controls required before a gpt2agent release."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .verify_release_tools import ToolValidationError, validate_policy, validate_tool
else:
    from verify_release_tools import ToolValidationError, validate_policy, validate_tool


_MAX_JSON_BYTES = 4 * 1024 * 1024
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_APP_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?\Z")
_APP_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9])?\Z")
_REQUIRED_TAG_IMMUTABILITY_RULES = frozenset({"deletion", "non_fast_forward", "update"})
_REQUIRED_CHECK_CONTEXT = "Required checks"
_RELEASE_SETTINGS_ENVIRONMENT = "release-settings-read"
_RELEASE_SETTINGS_CLIENT_ID_VARIABLE = "GPT2AGENT_RELEASE_SETTINGS_APP_CLIENT_ID"
_RELEASE_SETTINGS_PRIVATE_KEY_SECRET = "GPT2AGENT_RELEASE_SETTINGS_APP_PRIVATE_KEY"


class GovernanceError(ValueError):
    """A live or fixture snapshot could not be obtained safely."""


class _DuplicateJsonKey(ValueError):
    """A JSON object repeated a key."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _loads_json(payload: str | bytes, label: str) -> Any:
    try:
        return json.loads(payload, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, RecursionError):
        raise GovernanceError(f"{label} is invalid") from None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _array(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _exact_keys(value: dict[str, Any], expected: set[str]) -> bool:
    return set(value) == expected


def _validated_policy(value: Any) -> dict[str, Any] | None:
    policy = _object(value)
    if not _exact_keys(
        policy,
        {
            "schema_version",
            "repository",
            "release_tag_app",
            "release_settings_app",
            "required_check_app",
            "pypi_gate",
        },
    ):
        return None
    if policy.get("schema_version") != 2 or isinstance(policy.get("schema_version"), bool):
        return None

    repository = policy.get("repository")
    release_tag_app = _object(policy.get("release_tag_app"))
    release_settings_app = _object(policy.get("release_settings_app"))
    required_check_app = _object(policy.get("required_check_app"))
    gate = _object(policy.get("pypi_gate"))
    settings_id = release_settings_app.get("id")
    settings_slug = release_settings_app.get("slug")
    settings_client_id = release_settings_app.get("client_id")
    if (
        not isinstance(repository, str)
        or not _REPOSITORY_RE.fullmatch(repository)
        or not _exact_keys(release_tag_app, {"id"})
        or not _positive_int(release_tag_app.get("id"))
        or not _exact_keys(release_settings_app, {"id", "slug", "client_id"})
        or not _positive_int(settings_id)
        or not isinstance(settings_slug, str)
        or not _APP_SLUG_RE.fullmatch(settings_slug)
        or not isinstance(settings_client_id, str)
        or not _APP_CLIENT_ID_RE.fullmatch(settings_client_id)
        or not _exact_keys(required_check_app, {"id"})
        or not _positive_int(required_check_app.get("id"))
        or len(
            {
                release_tag_app.get("id"),
                settings_id,
                required_check_app.get("id"),
            }
        )
        != 3
    ):
        return None

    kind = gate.get("kind")
    if kind == "reviewer":
        login = gate.get("login")
        if (
            not _exact_keys(gate, {"kind", "login"})
            or not isinstance(login, str)
            or not _LOGIN_RE.fullmatch(login)
            or login.lower() == repository.split("/", 1)[0].lower()
        ):
            return None
        normalized_gate = {"kind": "reviewer", "login": login.lower()}
    elif kind == "protection_app":
        app_id = gate.get("id")
        slug = gate.get("slug")
        if (
            not _exact_keys(gate, {"kind", "id", "slug"})
            or not _positive_int(app_id)
            or app_id in {release_tag_app["id"], settings_id, required_check_app["id"]}
            or not isinstance(slug, str)
            or not _APP_SLUG_RE.fullmatch(slug)
        ):
            return None
        normalized_gate = {"kind": "protection_app", "id": app_id, "slug": slug}
    else:
        return None

    return {
        "repository": repository.lower(),
        "release_tag_app_id": release_tag_app["id"],
        "release_settings_app": {
            "id": settings_id,
            "slug": settings_slug,
            "client_id": settings_client_id,
        },
        "required_check_app_id": required_check_app["id"],
        "pypi_gate": normalized_gate,
    }


def _rules(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in _array(snapshot.get("rulesets")) if isinstance(item, dict)]


def _has_rule(ruleset: dict[str, Any], rule_type: str) -> bool:
    return any(
        isinstance(rule, dict) and rule.get("type") == rule_type
        for rule in _array(ruleset.get("rules"))
    )


def _exact_ref_scope(ruleset: dict[str, Any], pattern: str) -> bool:
    ref_name = _object(_object(ruleset.get("conditions")).get("ref_name"))
    return ref_name.get("include") == [pattern] and ref_name.get("exclude") == []


def _release_tag_rulesets(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        ruleset
        for ruleset in _rules(snapshot)
        if ruleset.get("target") == "tag"
        and ruleset.get("enforcement") == "active"
        and _exact_ref_scope(ruleset, "refs/tags/v*")
    ]


def _tag_immutability_enforced(rulesets: list[dict[str, Any]]) -> bool:
    no_bypass_rule_types = {
        rule.get("type")
        for ruleset in rulesets
        if ruleset.get("bypass_actors") == []
        for rule in _array(ruleset.get("rules"))
        if isinstance(rule, dict)
    }
    return _REQUIRED_TAG_IMMUTABILITY_RULES <= no_bypass_rule_types


def _narrow_release_bypass(ruleset: dict[str, Any], expected_app_id: int) -> bool:
    actors = _array(ruleset.get("bypass_actors"))
    if len(actors) != 1 or not isinstance(actors[0], dict):
        return False
    actor = actors[0]
    return (
        actor.get("actor_type") == "Integration"
        and actor.get("bypass_mode") == "always"
        and actor.get("actor_id") == expected_app_id
    )


def _repository_owner(snapshot: dict[str, Any]) -> str | None:
    full_name = _object(snapshot.get("repository")).get("full_name")
    if not isinstance(full_name, str) or not _REPOSITORY_RE.fullmatch(full_name):
        return None
    return full_name.split("/", 1)[0].lower()


def _policy_matches_repository(snapshot: dict[str, Any], policy: dict[str, Any] | None) -> bool:
    full_name = _object(snapshot.get("repository")).get("full_name")
    return (
        policy is not None
        and isinstance(full_name, str)
        and bool(_REPOSITORY_RE.fullmatch(full_name))
        and full_name.lower() == policy["repository"]
    )


def _reviewer_rules(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    environment = _object(snapshot.get("environment"))
    if environment.get("name") != "pypi":
        return []
    return [
        rule
        for rule in _array(environment.get("protection_rules"))
        if isinstance(rule, dict) and rule.get("type") == "required_reviewers"
    ]


def _expected_reviewer_rule(snapshot: dict[str, Any], expected_login: str) -> dict[str, Any] | None:
    owner = _repository_owner(snapshot)
    rules = _reviewer_rules(snapshot)
    if owner is None or len(rules) != 1 or expected_login == owner:
        return None
    rule = rules[0]
    reviewers = _array(rule.get("reviewers"))
    if len(reviewers) != 1 or not isinstance(reviewers[0], dict):
        return None
    entry = reviewers[0]
    reviewer = _object(entry.get("reviewer"))
    login = reviewer.get("login")
    if (
        entry.get("type") != "User"
        or not isinstance(login, str)
        or not _LOGIN_RE.fullmatch(login)
        or login.lower() != expected_login
    ):
        return None
    return rule


def _expected_protection_app(
    snapshot: dict[str, Any], expected_id: int, expected_slug: str
) -> bool:
    payload = _object(snapshot.get("custom_deployment_protection_rules"))
    rules = _array(payload.get("custom_deployment_protection_rules"))
    count = payload.get("total_count")
    if not isinstance(count, int) or isinstance(count, bool) or count != len(rules):
        return False
    enabled = [rule for rule in rules if isinstance(rule, dict) and rule.get("enabled") is True]
    if len(enabled) != 1:
        return False
    app = _object(enabled[0].get("app"))
    return app.get("id") == expected_id and app.get("slug") == expected_slug


def _pypi_v_tags_only(snapshot: dict[str, Any]) -> bool:
    environment = _object(snapshot.get("environment"))
    deployment_policy = _object(environment.get("deployment_branch_policy"))
    policies_payload = _object(snapshot.get("deployment_branch_policies"))
    policies = _array(policies_payload.get("branch_policies"))
    count = policies_payload.get("total_count")
    return (
        environment.get("name") == "pypi"
        and deployment_policy.get("protected_branches") is False
        and deployment_policy.get("custom_branch_policies") is True
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count == len(policies) == 1
        and isinstance(policies[0], dict)
        and policies[0].get("type") == "tag"
        and policies[0].get("name") == "v*"
    )


def _release_settings_app_identity(
    snapshot: dict[str, Any],
    expected: dict[str, Any] | None,
) -> bool:
    app = _object(snapshot.get("release_settings_app"))
    return expected is not None and all(
        app.get(field) == expected[field] for field in ("id", "slug", "client_id")
    )


def _release_settings_app_is_least_privilege(snapshot: dict[str, Any]) -> bool:
    app = _object(snapshot.get("release_settings_app"))
    return app.get("permissions") == {
        "administration": "read",
        "metadata": "read",
    } and app.get("events") == []


def _release_settings_environment_is_nonblocking(snapshot: dict[str, Any]) -> bool:
    environment = _object(snapshot.get("release_settings_environment"))
    rules = _array(environment.get("protection_rules"))
    custom_payload = _object(
        snapshot.get("release_settings_custom_deployment_protection_rules")
    )
    custom_rules = _array(custom_payload.get("custom_deployment_protection_rules"))
    custom_count = custom_payload.get("total_count")
    return (
        environment.get("name") == _RELEASE_SETTINGS_ENVIRONMENT
        and len(rules) == 1
        and isinstance(rules[0], dict)
        and rules[0].get("type") == "branch_policy"
        and isinstance(custom_count, int)
        and not isinstance(custom_count, bool)
        and custom_count == len(custom_rules) == 0
    )


def _release_settings_v_tags_only(snapshot: dict[str, Any]) -> bool:
    environment = _object(snapshot.get("release_settings_environment"))
    deployment_policy = _object(environment.get("deployment_branch_policy"))
    policies_payload = _object(snapshot.get("release_settings_deployment_branch_policies"))
    policies = _array(policies_payload.get("branch_policies"))
    count = policies_payload.get("total_count")
    return (
        environment.get("name") == _RELEASE_SETTINGS_ENVIRONMENT
        and deployment_policy.get("protected_branches") is False
        and deployment_policy.get("custom_branch_policies") is True
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count == len(policies) == 1
        and isinstance(policies[0], dict)
        and policies[0].get("type") == "tag"
        and policies[0].get("name") == "v*"
    )


def _release_settings_client_id_is_bound(
    snapshot: dict[str, Any],
    expected_client_id: str | None,
) -> bool:
    payload = _object(snapshot.get("release_settings_environment_variables"))
    variables = _array(payload.get("variables"))
    count = payload.get("total_count")
    return (
        expected_client_id is not None
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count == len(variables) == 1
        and isinstance(variables[0], dict)
        and variables[0].get("name") == _RELEASE_SETTINGS_CLIENT_ID_VARIABLE
        and variables[0].get("value") == expected_client_id
    )


def _release_settings_private_key_is_scoped(snapshot: dict[str, Any]) -> bool:
    payload = _object(snapshot.get("release_settings_environment_secrets"))
    secrets = _array(payload.get("secrets"))
    count = payload.get("total_count")
    return (
        isinstance(count, int)
        and not isinstance(count, bool)
        and count == len(secrets) == 1
        and isinstance(secrets[0], dict)
        and secrets[0].get("name") == _RELEASE_SETTINGS_PRIVATE_KEY_SECRET
    )


def _main_rulesets(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    repository = _object(snapshot.get("repository"))
    if repository.get("default_branch") != "main":
        return []
    return [
        ruleset
        for ruleset in _rules(snapshot)
        if ruleset.get("target") == "branch"
        and ruleset.get("enforcement") == "active"
        and _exact_ref_scope(ruleset, "refs/heads/main")
    ]


def _rule_parameters(rulesets: list[dict[str, Any]], rule_type: str) -> list[dict[str, Any]]:
    return [
        _object(rule.get("parameters"))
        for ruleset in rulesets
        for rule in _array(ruleset.get("rules"))
        if isinstance(rule, dict) and rule.get("type") == rule_type
    ]


def _approval_parameters(rulesets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        parameters
        for parameters in _rule_parameters(rulesets, "pull_request")
        if _positive_int(parameters.get("required_approving_review_count"))
    ]


def _required_check_parameters(
    rulesets: list[dict[str, Any]],
    expected_integration_id: int | None,
) -> list[dict[str, Any]]:
    if expected_integration_id is None:
        return []
    matching: list[dict[str, Any]] = []
    for parameters in _rule_parameters(rulesets, "required_status_checks"):
        checks = _array(parameters.get("required_status_checks"))
        if any(
            isinstance(check, dict)
            and check.get("context") == _REQUIRED_CHECK_CONTEXT
            and check.get("integration_id") == expected_integration_id
            for check in checks
        ):
            matching.append(parameters)
    return matching


_CHECK_DETAILS = {
    "policy_identity_bound": (
        "the explicit release-identity policy is valid and bound to this repository",
        "the release-identity policy is absent, invalid, or bound to another repository",
    ),
    "tag_creation_restricted": (
        "v* tag creation is restricted by an active ruleset",
        "v* tag creation is not restricted by an active exact-scope ruleset",
    ),
    "tag_immutable": (
        "v* tag deletion, update, and non-fast-forward restrictions have no bypass actors",
        "v* tag deletion, update, or non-fast-forward lacks a no-bypass restriction",
    ),
    "tag_bypass_actor_narrow": (
        "v* tags have only the policy-bound release App bypass actor",
        "v* tags lack the exact policy-bound release App as their sole bypass actor",
    ),
    "release_immutability_enabled": (
        "repository release immutability is enabled",
        "repository release immutability is not enabled",
    ),
    "release_settings_app_identity": (
        "the immutable-release reader matches the policy-bound GitHub App",
        "the immutable-release reader does not match the policy-bound GitHub App",
    ),
    "release_settings_app_least_privilege": (
        "the immutable-release reader has only Administration read and Metadata read",
        "the immutable-release reader has unexpected permissions or subscribed events",
    ),
    "release_settings_environment_nonblocking": (
        "the release-settings-read environment has no late approval or wait gate",
        "the release-settings-read environment is missing or has an unexpected protection gate",
    ),
    "release_settings_admin_bypass_disabled": (
        "release-settings-read administrator bypass is disabled",
        "release-settings-read administrator bypass is not explicitly disabled",
    ),
    "release_settings_v_tags_only": (
        "release-settings-read accepts only v* tags",
        "release-settings-read is not limited to exactly the v* tag policy",
    ),
    "release_settings_client_id_bound": (
        "release-settings-read exposes only the policy-bound App client ID variable",
        "release-settings-read does not expose exactly the policy-bound App client ID variable",
    ),
    "release_settings_private_key_scoped": (
        "release-settings-read contains only the expected App private-key secret",
        "release-settings-read does not contain exactly the expected App private-key secret",
    ),
    "pypi_independent_gate": (
        "PyPI deployment matches the policy-bound independent gate identity",
        "PyPI deployment does not match the policy-bound independent gate identity",
    ),
    "pypi_prevent_self_review": (
        "PyPI self-approval is prevented",
        "PyPI self-approval is not prevented",
    ),
    "pypi_admin_bypass_disabled": (
        "PyPI administrator bypass is disabled",
        "PyPI administrator bypass is not explicitly disabled",
    ),
    "pypi_v_tags_only": (
        "PyPI deployment accepts only v* tags",
        "PyPI deployment is not limited to exactly the v* tag policy",
    ),
    "main_one_approval": (
        "main requires at least one pull-request approval",
        "main lacks an active exact-scope one-approval pull-request gate",
    ),
    "main_stale_reviews_dismissed": (
        "main dismisses stale approvals after new pushes",
        "main does not dismiss stale approvals after new pushes",
    ),
    "main_threads_resolved": (
        "main requires review-thread resolution",
        "main does not require review-thread resolution",
    ),
    "main_required_checks": (
        "main requires Required checks from the policy-bound App",
        "main does not require Required checks from the policy-bound App",
    ),
    "main_strict_required_checks": (
        "main enforces Required checks with strict branch freshness",
        "main does not enforce Required checks with strict branch freshness",
    ),
    "main_bypass_disabled": (
        "main protection has no bypass actors",
        "main protection is absent or has a bypass actor",
    ),
}


def audit_snapshot(snapshot: Any, policy: Any = None) -> dict[str, Any]:
    """Return a deterministic, value-safe report for one GitHub snapshot."""
    root = _object(snapshot)
    reviewed_policy = _validated_policy(policy)
    policy_bound = _policy_matches_repository(root, reviewed_policy)
    tag_rulesets = _release_tag_rulesets(root)
    creation_tag_rulesets = [ruleset for ruleset in tag_rulesets if _has_rule(ruleset, "creation")]

    reviewer_rule = None
    protection_app = False
    if policy_bound and reviewed_policy is not None:
        gate = reviewed_policy["pypi_gate"]
        if gate["kind"] == "reviewer":
            reviewer_rule = _expected_reviewer_rule(root, gate["login"])
        else:
            protection_app = _expected_protection_app(root, gate["id"], gate["slug"])

    environment = _object(root.get("environment"))
    main_rulesets = _main_rulesets(root)
    approval_parameters = _approval_parameters(main_rulesets)
    expected_check_app_id = (
        reviewed_policy["required_check_app_id"]
        if policy_bound and reviewed_policy is not None
        else None
    )
    required_check_parameters = _required_check_parameters(main_rulesets, expected_check_app_id)
    main_bypass_disabled = bool(main_rulesets) and all(
        ruleset.get("bypass_actors") == [] for ruleset in main_rulesets
    )
    expected_settings_app = (
        reviewed_policy["release_settings_app"]
        if policy_bound and reviewed_policy is not None
        else None
    )
    settings_app_identity = _release_settings_app_identity(root, expected_settings_app)
    settings_environment = _object(root.get("release_settings_environment"))
    expected_settings_client_id = (
        expected_settings_app["client_id"] if expected_settings_app is not None else None
    )
    results = (
        ("policy_identity_bound", policy_bound),
        (
            "tag_creation_restricted",
            bool(creation_tag_rulesets),
        ),
        ("tag_immutable", _tag_immutability_enforced(tag_rulesets)),
        (
            "tag_bypass_actor_narrow",
            bool(reviewed_policy)
            and policy_bound
            and bool(creation_tag_rulesets)
            and all(
                _narrow_release_bypass(ruleset, reviewed_policy["release_tag_app_id"])
                for ruleset in creation_tag_rulesets
            ),
        ),
        (
            "release_immutability_enabled",
            _object(root.get("immutable_releases")).get("enabled") is True,
        ),
        ("release_settings_app_identity", settings_app_identity),
        (
            "release_settings_app_least_privilege",
            settings_app_identity and _release_settings_app_is_least_privilege(root),
        ),
        (
            "release_settings_environment_nonblocking",
            policy_bound and _release_settings_environment_is_nonblocking(root),
        ),
        (
            "release_settings_admin_bypass_disabled",
            policy_bound and settings_environment.get("can_admins_bypass") is False,
        ),
        (
            "release_settings_v_tags_only",
            policy_bound and _release_settings_v_tags_only(root),
        ),
        (
            "release_settings_client_id_bound",
            policy_bound
            and _release_settings_client_id_is_bound(root, expected_settings_client_id),
        ),
        (
            "release_settings_private_key_scoped",
            policy_bound and _release_settings_private_key_is_scoped(root),
        ),
        ("pypi_independent_gate", reviewer_rule is not None or protection_app),
        (
            "pypi_prevent_self_review",
            protection_app
            or (reviewer_rule is not None and reviewer_rule.get("prevent_self_review") is True),
        ),
        ("pypi_admin_bypass_disabled", environment.get("can_admins_bypass") is False),
        ("pypi_v_tags_only", _pypi_v_tags_only(root)),
        ("main_one_approval", bool(approval_parameters)),
        (
            "main_stale_reviews_dismissed",
            any(
                parameters.get("dismiss_stale_reviews_on_push") is True
                for parameters in approval_parameters
            ),
        ),
        (
            "main_threads_resolved",
            any(
                parameters.get("required_review_thread_resolution") is True
                for parameters in approval_parameters
            ),
        ),
        ("main_required_checks", bool(required_check_parameters)),
        (
            "main_strict_required_checks",
            any(
                parameters.get("strict_required_status_checks_policy") is True
                for parameters in required_check_parameters
            ),
        ),
        ("main_bypass_disabled", main_bypass_disabled),
    )
    checks = [
        {
            "id": check_id,
            "status": "pass" if passed else "fail",
            "detail": _CHECK_DETAILS[check_id][0 if passed else 1],
        }
        for check_id, passed in results
    ]
    return {
        "schema_version": 2,
        "status": "pass" if all(passed for _, passed in results) else "fail",
        "checks": checks,
    }


def _gh_json(endpoint: str, *, gh_path: Path) -> Any:
    token = os.environ.get("GH_TOKEN")
    if (
        not gh_path.is_absolute()
        or not isinstance(token, str)
        or not token
        or len(token) > 4096
        or any(character.isspace() for character in token)
    ):
        raise GovernanceError("GitHub governance snapshot is unavailable")
    command = [
        str(gh_path),
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2026-03-10",
        endpoint,
    ]
    try:
        result = subprocess.run(
            command,
            env={
                "GH_TOKEN": token,
                "GH_CONFIG_DIR": "/nonexistent",
                "GH_PROMPT_DISABLED": "1",
                "HOME": "/nonexistent",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GovernanceError("GitHub governance snapshot is unavailable") from None
    if result.returncode != 0 or len(result.stdout) > _MAX_JSON_BYTES:
        raise GovernanceError("GitHub governance snapshot is unavailable")
    return _loads_json(result.stdout, "GitHub governance snapshot")


def fetch_live_snapshot(
    repository: str,
    *,
    release_settings_app_slug: str,
    requester: Callable[[str], Any] | None = None,
    gh_path: Path | None = None,
) -> dict[str, Any]:
    """Fetch only the reviewed GitHub GET endpoints needed by the audit."""
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        raise GovernanceError("repository must be an owner/name pair")
    if (
        not isinstance(release_settings_app_slug, str)
        or not _APP_SLUG_RE.fullmatch(release_settings_app_slug)
    ):
        raise GovernanceError("release settings App slug is invalid")
    if requester is None:
        if gh_path is None:
            raise GovernanceError("an exact gh path is required")

        def requester(endpoint: str) -> Any:
            return _gh_json(endpoint, gh_path=gh_path)

    prefix = f"repos/{repository}"
    repository_payload = requester(prefix)
    immutable_releases = requester(f"{prefix}/immutable-releases")
    summaries = requester(f"{prefix}/rulesets?includes_parents=true&per_page=100")
    if not isinstance(summaries, list) or len(summaries) >= 100:
        raise GovernanceError("GitHub ruleset snapshot is incomplete")
    ruleset_ids = [item.get("id") for item in summaries if isinstance(item, dict)]
    if len(ruleset_ids) != len(summaries) or not all(
        _positive_int(ruleset_id) for ruleset_id in ruleset_ids
    ):
        raise GovernanceError("GitHub ruleset snapshot is invalid")
    if len(set(ruleset_ids)) != len(ruleset_ids):
        raise GovernanceError("GitHub ruleset snapshot is invalid")
    rulesets = [requester(f"{prefix}/rulesets/{ruleset_id}") for ruleset_id in ruleset_ids]
    environment = requester(f"{prefix}/environments/pypi")
    policies = requester(f"{prefix}/environments/pypi/deployment-branch-policies?per_page=100")
    protection_apps = requester(
        f"{prefix}/environments/pypi/deployment_protection_rules?per_page=100"
    )
    release_settings_app = requester(f"apps/{release_settings_app_slug}")
    release_settings_environment = requester(
        f"{prefix}/environments/{_RELEASE_SETTINGS_ENVIRONMENT}"
    )
    release_settings_policies = requester(
        f"{prefix}/environments/{_RELEASE_SETTINGS_ENVIRONMENT}"
        "/deployment-branch-policies?per_page=100"
    )
    release_settings_protection_rules = requester(
        f"{prefix}/environments/{_RELEASE_SETTINGS_ENVIRONMENT}"
        "/deployment_protection_rules?per_page=100"
    )
    release_settings_secrets = requester(
        f"{prefix}/environments/{_RELEASE_SETTINGS_ENVIRONMENT}/secrets?per_page=100"
    )
    release_settings_variables = requester(
        f"{prefix}/environments/{_RELEASE_SETTINGS_ENVIRONMENT}/variables?per_page=100"
    )
    return {
        "schema_version": 2,
        "repository": repository_payload,
        "immutable_releases": immutable_releases,
        "rulesets": rulesets,
        "environment": environment,
        "deployment_branch_policies": policies,
        "custom_deployment_protection_rules": protection_apps,
        "release_settings_app": release_settings_app,
        "release_settings_environment": release_settings_environment,
        "release_settings_deployment_branch_policies": release_settings_policies,
        "release_settings_custom_deployment_protection_rules": (
            release_settings_protection_rules
        ),
        "release_settings_environment_secrets": release_settings_secrets,
        "release_settings_environment_variables": release_settings_variables,
    }


def _load_json_file(path: Path, label: str) -> Any:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise GovernanceError(f"{label} exceeds 4 MiB")
        payload = path.read_bytes()
    except OSError:
        raise GovernanceError(f"{label} is unavailable") from None
    return _loads_json(payload, label)


def _load_snapshot(path: Path) -> Any:
    return _load_json_file(path, "governance fixture")


def _load_policy(path: Path) -> Any:
    return _load_json_file(path, "governance policy")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", type=Path, help="read a local JSON fixture")
    source.add_argument("--live", metavar="OWNER/REPO", help="fetch reviewed endpoints with gh")
    parser.add_argument(
        "--policy",
        type=Path,
        help="read reviewed repository and release-principal identities from JSON",
    )
    parser.add_argument(
        "--gh",
        type=Path,
        help="exact trusted gh executable (required with --live)",
    )
    args = parser.parse_args(argv)

    try:
        if args.live is not None:
            if args.policy is None:
                raise GovernanceError("live audit requires an explicit reviewed --policy file")
            if args.gh is None or not args.gh.is_absolute():
                raise GovernanceError("live audit requires an exact absolute --gh path")
            policy = _load_policy(args.policy)
            reviewed_policy = _validated_policy(policy)
            if reviewed_policy is None:
                raise GovernanceError("governance policy is invalid")
            if args.live.lower() != reviewed_policy["repository"]:
                raise GovernanceError("governance policy repository does not match --live target")
            try:
                validate_tool(args.gh, "gh")
                validate_policy(args.policy)
            except ToolValidationError:
                raise GovernanceError("live audit inputs are not protected") from None
            snapshot = fetch_live_snapshot(
                args.live,
                release_settings_app_slug=reviewed_policy["release_settings_app"]["slug"],
                gh_path=args.gh,
            )
        else:
            snapshot = _load_snapshot(args.snapshot)
            policy = (
                _load_policy(args.policy)
                if args.policy is not None
                else _object(snapshot).get("policy")
            )
    except GovernanceError as exc:
        print(f"release governance audit failed: {exc}", file=sys.stderr)
        return 2

    report = audit_snapshot(snapshot, policy)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
