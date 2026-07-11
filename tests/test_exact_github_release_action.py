"""Tests for the commit-pinned exact GitHub Release publisher action."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTION_ROOT = PROJECT_ROOT / ".github" / "actions" / "publish-exact-github-release"
ACTION_YAML = ACTION_ROOT / "action.yml"
PUBLISHER = ACTION_ROOT / "publish.py"


def _load_publisher():
    spec = importlib.util.spec_from_file_location("exact_release_publisher", PUBLISHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publisher = _load_publisher()


def _expected_assets() -> dict[str, Any]:
    return {
        name: publisher.ExpectedAsset(len(payload), hashlib.sha256(payload).hexdigest())
        for name, payload in {
            "gpt2agent-1.2.3-py3-none-any.whl": b"wheel",
            "gpt2agent-1.2.3.tar.gz": b"sdist",
            "release-workflow-artifacts.json": b"evidence",
        }.items()
    }


def _release(*, draft: bool = True, immutable: bool = False) -> dict[str, Any]:
    return {
        "id": 42,
        "tag_name": "v1.2.3",
        "name": "v1.2.3",
        "body": "release notes\n",
        "prerelease": False,
        "draft": draft,
        "immutable": immutable,
    }


def _assets(expected: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": index,
            "name": name,
            "state": "uploaded",
            "size": record.size,
            "digest": f"sha256:{record.sha256}",
        }
        for index, (name, record) in enumerate(expected.items(), start=101)
    ]


class _FakeGitHub:
    def __init__(
        self,
        release: dict[str, Any],
        assets: list[dict[str, Any]],
        *,
        ambiguous_patch: bool = False,
        change_second_snapshot: bool = False,
    ) -> None:
        self.release = deepcopy(release)
        self.assets = deepcopy(assets)
        self.ambiguous_patch = ambiguous_patch
        self.change_second_snapshot = change_second_snapshot
        self.calls: list[tuple[str, str, object | None]] = []
        self.release_gets = 0

    def __call__(self, method: str, path: str, token: str, payload: object | None):
        assert token == "token"
        self.calls.append((method, path, payload))
        base = "/repos/robotlearning123/gpt2agent/releases/42"
        if method == "GET" and path == base:
            self.release_gets += 1
            response = deepcopy(self.release)
            if self.change_second_snapshot and self.release_gets == 2:
                response["body"] = "changed before publish\n"
            return response
        if method == "GET" and path == f"{base}/assets?per_page=100":
            return deepcopy(self.assets)
        if method == "PATCH" and path == base and payload == {"draft": False}:
            self.release["draft"] = False
            self.release["immutable"] = True
            if self.ambiguous_patch:
                raise ValueError("ambiguous transport failure")
            return deepcopy(self.release)
        raise AssertionError(f"unexpected request: {method} {path} {payload!r}")


def _publish(api: _FakeGitHub, expected: dict[str, Any]) -> str:
    return publisher.publish_exact_release(
        "robotlearning123/gpt2agent",
        42,
        "v1.2.3",
        "release notes\n",
        False,
        expected,
        "token",
        request_json=api,
        sleep=lambda delay: None,
    )


def test_action_is_isolated_and_does_not_put_token_on_command_line() -> None:
    action = ACTION_YAML.read_text(encoding="utf-8")
    source = PUBLISHER.read_text(encoding="utf-8")

    assert "using: composite" in action
    assert "/usr/bin/env -i" in action
    assert "/usr/bin/python3 -I -S -B" in action
    assert 'GH_TOKEN="$INPUT_GITHUB_TOKEN"' in action
    assert "--github-token" not in action
    assert "actions/checkout" not in action
    assert 'method not in {"GET", "PATCH"}' in source
    assert 'payload != {"draft": False}' in source
    assert '"POST"' not in source


def test_exact_draft_is_validated_twice_and_patched_by_numeric_id() -> None:
    expected = _expected_assets()
    api = _FakeGitHub(_release(), _assets(expected))

    assert _publish(api, expected) == "published"

    patch_calls = [call for call in api.calls if call[0] == "PATCH"]
    assert patch_calls == [
        ("PATCH", "/repos/robotlearning123/gpt2agent/releases/42", {"draft": False})
    ]
    assert api.calls.index(patch_calls[0]) > 3
    assert {method for method, _, _ in api.calls} <= {"GET", "PATCH"}


def test_exact_published_immutable_rerun_is_a_noop_success() -> None:
    expected = _expected_assets()
    api = _FakeGitHub(_release(draft=False, immutable=True), _assets(expected))

    assert _publish(api, expected) == "already-published"
    assert all(method == "GET" for method, _, _ in api.calls)


def test_ambiguous_patch_is_recovered_only_by_exact_immutable_readback() -> None:
    expected = _expected_assets()
    api = _FakeGitHub(_release(), _assets(expected), ambiguous_patch=True)

    assert _publish(api, expected) == "published-after-ambiguous-response"
    assert sum(method == "PATCH" for method, _, _ in api.calls) == 1


def test_changed_second_snapshot_fails_before_patch() -> None:
    expected = _expected_assets()
    api = _FakeGitHub(
        _release(),
        _assets(expected),
        change_second_snapshot=True,
    )

    with pytest.raises(ValueError, match="body"):
        _publish(api, expected)

    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", 43, "ID"),
        ("tag_name", "v1.2.4", "tag"),
        ("name", "wrong", "name"),
        ("body", "release notes", "body"),
        ("prerelease", True, "prerelease"),
        ("draft", False, "immutable"),
        ("immutable", True, "immutable"),
    ],
)
def test_mismatched_draft_metadata_fails_before_patch(
    field: str,
    value: object,
    message: str,
) -> None:
    expected = _expected_assets()
    release = _release()
    release[field] = value
    api = _FakeGitHub(release, _assets(expected))

    with pytest.raises(ValueError, match=message):
        _publish(api, expected)

    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("extra", "exact asset set"),
        ("missing", "exact asset set"),
        ("duplicate-name", "duplicate asset names"),
        ("duplicate-id", "unique IDs"),
        ("state", "not uploaded"),
        ("size", "size"),
        ("digest", "digest"),
    ],
)
def test_mismatched_draft_assets_fail_before_patch(mutation: str, message: str) -> None:
    expected = _expected_assets()
    assets = _assets(expected)
    if mutation == "extra":
        assets.append(
            {"id": 999, "name": "extra", "state": "uploaded", "size": 0, "digest": "sha256:" + "0" * 64}
        )
    elif mutation == "missing":
        assets.pop()
    elif mutation == "duplicate-name":
        assets[1]["name"] = assets[0]["name"]
    elif mutation == "duplicate-id":
        assets[1]["id"] = assets[0]["id"]
    elif mutation == "state":
        assets[0]["state"] = "new"
    elif mutation == "size":
        assets[0]["size"] += 1
    else:
        assets[0]["digest"] = "sha256:" + "0" * 64
    api = _FakeGitHub(_release(), assets)

    with pytest.raises(ValueError, match=message):
        _publish(api, expected)

    assert all(method == "GET" for method, _, _ in api.calls)


def test_missing_numeric_draft_fails_without_fallback_creation() -> None:
    calls: list[tuple[str, str]] = []

    def missing(method: str, path: str, token: str, payload: object | None):
        calls.append((method, path))
        raise ValueError("GitHub API request failed with HTTP 404")

    with pytest.raises(ValueError, match="HTTP 404"):
        publisher.publish_exact_release(
            "robotlearning123/gpt2agent",
            42,
            "v1.2.3",
            "release notes\n",
            False,
            _expected_assets(),
            "token",
            request_json=missing,
            sleep=lambda delay: None,
        )

    assert calls == [("GET", "/repos/robotlearning123/gpt2agent/releases/42")]


def test_local_inputs_are_exact_regular_bounded_files(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "gpt2agent-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    (dist / "gpt2agent-1.2.3.tar.gz").write_bytes(b"sdist")
    evidence = tmp_path / "release-workflow-artifacts.json"
    evidence.write_bytes(b"evidence")

    expected = publisher.expected_release_assets(dist, evidence, "1.2.3")
    assert set(expected) == {wheel.name, "gpt2agent-1.2.3.tar.gz", evidence.name}

    (dist / "extra").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="exact release files"):
        publisher.expected_release_assets(dist, evidence, "1.2.3")
    (dist / "extra").unlink()

    wheel.unlink()
    wheel.symlink_to(evidence)
    with pytest.raises(ValueError, match="regular file"):
        publisher.expected_release_assets(dist, evidence, "1.2.3")


def test_local_oversized_asset_is_rejected_before_hashing(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "gpt2agent-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    (dist / "gpt2agent-1.2.3.tar.gz").write_bytes(b"sdist")
    evidence = tmp_path / "release-workflow-artifacts.json"
    evidence.write_bytes(b"evidence")
    with wheel.open("wb") as stream:
        stream.truncate(publisher.MAX_ASSET_BYTES + 1)

    with pytest.raises(ValueError, match="size limit"):
        publisher.expected_release_assets(dist, evidence, "1.2.3")


def test_request_layer_rejects_collection_creation_without_network() -> None:
    with pytest.raises(ValueError, match="disallowed"):
        publisher._request_json(
            "POST",
            "/repos/robotlearning123/gpt2agent/releases",
            "token",
            {"draft": False},
        )
