"""Fail-closed tests for binding a release to the live annotated tag graph."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTION_ROOT = PROJECT_ROOT / ".github" / "actions" / "publish-exact-github-release"
ACTION_YAML = ACTION_ROOT / "action.yml"
PUBLISHER_PATH = ACTION_ROOT / "publish.py"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
TAG_OBJECT = "1" * 40
COMMIT = "2" * 40
TREE = "3" * 40
WRONG = "f" * 40


def _load_publisher():
    spec = importlib.util.spec_from_file_location(
        "final_tag_release_publisher", PUBLISHER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publisher = _load_publisher()
verifier = importlib.import_module("scripts.verify_github_release")


def _graph(mode: str = "exact") -> dict[str, Any]:
    reference: dict[str, Any] = {
        "ref": "refs/tags/v1.2.3",
        "object": {"type": "tag", "sha": TAG_OBJECT},
    }
    tag_object: dict[str, Any] = {
        "sha": TAG_OBJECT,
        "tag": "v1.2.3",
        "object": {"type": "commit", "sha": COMMIT},
    }
    commit: dict[str, Any] = {"sha": COMMIT, "tree": {"sha": TREE}}
    if mode == "deleted":
        reference = {"deleted": True}
    elif mode == "moved":
        reference["object"]["sha"] = WRONG
    elif mode == "lightweight":
        reference["object"] = {"type": "commit", "sha": COMMIT}
    elif mode == "wrong-tag":
        tag_object["tag"] = "v1.2.4"
    elif mode == "wrong-tag-object":
        tag_object["sha"] = WRONG
    elif mode == "wrong-commit":
        tag_object["object"]["sha"] = WRONG
    elif mode == "wrong-commit-object":
        commit["sha"] = WRONG
    elif mode == "wrong-tree":
        commit["tree"]["sha"] = WRONG
    elif mode not in {"exact", "drift-final-ref"}:
        raise AssertionError(f"unknown graph mode: {mode}")
    return {"ref": reference, "tag": tag_object, "commit": commit}


def _publisher_assets() -> dict[str, Any]:
    return {
        name: publisher.ExpectedAsset(len(payload), hashlib.sha256(payload).hexdigest())
        for name, payload in {
            "gpt2agent-1.2.3-py3-none-any.whl": b"wheel",
            "gpt2agent-1.2.3.tar.gz": b"sdist",
            "release-workflow-artifacts.json": b"evidence",
        }.items()
    }


class _FakePublicationAPI:
    def __init__(
        self,
        *,
        graphs: list[str] | None = None,
        published: bool = False,
        ambiguous_patch: bool = False,
        ambiguous_patch_applies: bool = False,
    ) -> None:
        expected = _publisher_assets()
        self.release = {
            "id": 42,
            "tag_name": "v1.2.3",
            "name": "v1.2.3",
            "body": "release notes\n",
            "prerelease": False,
            "draft": not published,
            "immutable": published,
        }
        self.assets = [
            {
                "id": index,
                "name": name,
                "state": "uploaded",
                "size": record.size,
                "digest": f"sha256:{record.sha256}",
            }
            for index, (name, record) in enumerate(expected.items(), start=101)
        ]
        self.graphs = graphs or ["exact"]
        self.graph_gets = 0
        self.ambiguous_patch = ambiguous_patch
        self.ambiguous_patch_applies = ambiguous_patch_applies
        self.calls: list[tuple[str, str, str, object | None]] = []

    def __call__(self, method: str, path: str, token: str, payload: object | None):
        settings = "/repos/robotlearning123/gpt2agent/immutable-releases"
        release = "/repos/robotlearning123/gpt2agent/releases/42"
        ref = "/repos/robotlearning123/gpt2agent/git/ref/tags/v1.2.3"
        tag_object = f"/repos/robotlearning123/gpt2agent/git/tags/{TAG_OBJECT}"
        commit = f"/repos/robotlearning123/gpt2agent/git/commits/{COMMIT}"
        assert token == ("settings-token" if path == settings else "release-token")
        self.calls.append((method, path, token, payload))
        if method == "GET" and path == settings:
            return {"enabled": True, "enforced_by_owner": False}
        if method == "GET" and path == release:
            return deepcopy(self.release)
        if method == "GET" and path == f"{release}/assets?per_page=100":
            return deepcopy(self.assets)
        if method == "GET" and path in {ref, tag_object, commit}:
            binding = min(self.graph_gets // 4, len(self.graphs) - 1)
            graph = _graph(self.graphs[binding])
            self.graph_gets += 1
            if path == ref:
                if self.graphs[binding] == "deleted":
                    raise ValueError("GitHub API request failed with HTTP 404")
                if self.graphs[binding] == "drift-final-ref" and self.graph_gets % 4 == 0:
                    graph["ref"]["object"]["sha"] = WRONG
                return deepcopy(graph["ref"])
            if path == tag_object:
                return deepcopy(graph["tag"])
            return deepcopy(graph["commit"])
        if method == "PATCH" and path == release and payload == {"draft": False}:
            if not self.ambiguous_patch or self.ambiguous_patch_applies:
                self.release["draft"] = False
                self.release["immutable"] = True
            if self.ambiguous_patch:
                self.ambiguous_patch = False
                raise ValueError("ambiguous transport failure")
            return deepcopy(self.release)
        raise AssertionError(f"unexpected request: {method} {path} {payload!r}")


def _publish(api: _FakePublicationAPI) -> str:
    return publisher.publish_exact_release(
        "robotlearning123/gpt2agent",
        42,
        "v1.2.3",
        "release notes\n",
        False,
        _publisher_assets(),
        "release-token",
        "settings-token",
        expected_tag_object=TAG_OBJECT,
        expected_commit=COMMIT,
        expected_tree=TREE,
        release_request_json=api,
        settings_request_json=api,
        sleep=lambda delay: None,
    )


def _preflight(api: _FakePublicationAPI) -> None:
    publisher.preflight_exact_release_draft(
        "robotlearning123/gpt2agent",
        42,
        "v1.2.3",
        "release notes\n",
        False,
        _publisher_assets(),
        "release-token",
        expected_tag_object=TAG_OBJECT,
        expected_commit=COMMIT,
        expected_tree=TREE,
        release_request_json=api,
    )


def _binding_paths() -> list[str]:
    base = "/repos/robotlearning123/gpt2agent"
    return [
        f"{base}/git/ref/tags/v1.2.3",
        f"{base}/git/tags/{TAG_OBJECT}",
        f"{base}/git/commits/{COMMIT}",
        f"{base}/git/ref/tags/v1.2.3",
    ]


def test_publisher_rebinds_immediately_before_patch_and_after_readback() -> None:
    api = _FakePublicationAPI()

    assert _publish(api) == "published"

    patch_index = next(
        index for index, call in enumerate(api.calls) if call[0] == "PATCH"
    )
    binding_size = len(_binding_paths())
    assert [call[1] for call in api.calls[patch_index - binding_size : patch_index]] == _binding_paths()
    assert [call[1] for call in api.calls[-binding_size:]] == _binding_paths()
    assert all(call[2] == "release-token" for call in api.calls[-binding_size:])


def test_already_published_rerun_rebinds_before_noop_success() -> None:
    api = _FakePublicationAPI(published=True)

    assert _publish(api) == "already-published"

    assert [call[1] for call in api.calls[-len(_binding_paths()) :]] == _binding_paths()
    assert all(call[0] == "GET" for call in api.calls)


@pytest.mark.parametrize(
    "mode",
    [
        "deleted",
        "moved",
        "lightweight",
        "wrong-tag",
        "wrong-tag-object",
        "wrong-commit",
        "wrong-commit-object",
        "wrong-tree",
        "drift-final-ref",
    ],
)
def test_prepatch_tag_graph_drift_fails_without_mutation(mode: str) -> None:
    api = _FakePublicationAPI(graphs=[mode])

    with pytest.raises(ValueError, match="tag|commit|tree|HTTP 404"):
        _publish(api)

    assert all(call[0] != "PATCH" for call in api.calls)


def test_draft_preflight_rejects_wrong_live_tree_without_mutation() -> None:
    api = _FakePublicationAPI(graphs=["wrong-tree"])

    with pytest.raises(ValueError, match="source tree"):
        _preflight(api)

    assert api.calls
    assert all(call[0] == "GET" for call in api.calls)


def test_ambiguous_retry_rebinds_and_refuses_drift_without_second_patch() -> None:
    api = _FakePublicationAPI(
        graphs=["exact", "moved"],
        ambiguous_patch=True,
        ambiguous_patch_applies=False,
    )

    with pytest.raises(ValueError, match="tag"):
        _publish(api)

    assert sum(call[0] == "PATCH" for call in api.calls) == 1
    first_patch = next(index for index, call in enumerate(api.calls) if call[0] == "PATCH")
    assert [
        call[1]
        for call in api.calls[first_patch - len(_binding_paths()) : first_patch]
    ] == _binding_paths()


def test_ambiguous_applied_patch_rebinds_after_exact_readback() -> None:
    api = _FakePublicationAPI(
        graphs=["exact", "exact"],
        ambiguous_patch=True,
        ambiguous_patch_applies=True,
    )

    assert _publish(api) == "published-after-ambiguous-response"
    assert sum(call[0] == "PATCH" for call in api.calls) == 1
    assert [call[1] for call in api.calls[-len(_binding_paths()) :]] == _binding_paths()


def test_postpublish_readback_rejects_late_tag_drift() -> None:
    api = _FakePublicationAPI(graphs=["exact", "moved"])

    with pytest.raises(ValueError, match="published release did not become exact"):
        _publish(api)

    assert sum(call[0] == "PATCH" for call in api.calls) == 1


def test_release_request_allowlist_is_get_only_for_exact_tag_graph_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str, object | None]] = []
    monkeypatch.setattr(
        publisher,
        "_perform_json_request",
        lambda method, path, token, payload: calls.append((method, path, token, payload)),
    )
    for path in _binding_paths():
        publisher._request_release_json("GET", path, "release-token", None)
    assert [call[1] for call in calls] == _binding_paths()

    for method, path, payload in [
        ("PATCH", _binding_paths()[0], {"draft": False}),
        ("GET", "/repos/robotlearning123/gpt2agent/git/refs/tags/v1.2.3", None),
        ("GET", f"/repos/robotlearning123/gpt2agent/git/trees/{TREE}", None),
        ("POST", _binding_paths()[0], None),
    ]:
        with pytest.raises(ValueError, match="disallowed"):
            publisher._request_release_json(method, path, "release-token", payload)


def test_action_and_workflow_flow_exact_verified_git_identity() -> None:
    action = ACTION_YAML.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    for input_name, argument in [
        ("tag-object", "--tag-object"),
        ("commit", "--commit"),
        ("tree", "--tree"),
    ]:
        lines = action.splitlines()
        start = lines.index(f"  {input_name}:")
        assert "    required: true" in lines[start + 1 : start + 4]
        assert argument in action
        assert (
            f"          {input_name}: ${{{{ needs.verify.outputs."
            f"{'tag_object' if input_name == 'tag-object' else input_name} }}}}"
        ) in workflow


def _verifier_fetcher(mode: str, calls: list[tuple[str, str]]):
    graph = _graph(mode)
    reference_gets = 0

    def fetch(url: str, token: str):
        nonlocal reference_gets
        calls.append((url, token))
        if url.endswith("/git/ref/tags/v1.2.3"):
            if mode == "deleted":
                raise ValueError("GitHub API request failed with HTTP 404")
            reference_gets += 1
            if mode == "drift-final-ref" and reference_gets == 2:
                moved = deepcopy(graph["ref"])
                moved["object"]["sha"] = WRONG
                return moved, {}
            return deepcopy(graph["ref"]), {}
        if url.endswith(f"/git/tags/{TAG_OBJECT}"):
            return deepcopy(graph["tag"]), {}
        if url.endswith(f"/git/commits/{COMMIT}"):
            return deepcopy(graph["commit"]), {}
        raise AssertionError(f"unexpected URL: {url}")

    return fetch


@pytest.mark.parametrize(
    "mode",
    [
        "deleted",
        "moved",
        "lightweight",
        "wrong-tag",
        "wrong-tag-object",
        "wrong-commit",
        "wrong-commit-object",
        "wrong-tree",
        "drift-final-ref",
    ],
)
def test_postrelease_verifier_independently_rejects_tag_graph_drift(mode: str) -> None:
    calls: list[tuple[str, str]] = []

    with pytest.raises(ValueError, match="tag|commit|tree|HTTP 404"):
        verifier.verify_remote_tag_binding(
            "robotlearning123/gpt2agent",
            "v1.2.3",
            TAG_OBJECT,
            COMMIT,
            TREE,
            "release-token",
            fetch_json=_verifier_fetcher(mode, calls),
        )

    assert calls
    assert all(token == "release-token" for _, token in calls)


def test_postrelease_tag_fetch_allowlist_rejects_nonexact_urls_before_auth() -> None:
    for url in [
        f"https://evil.invalid/repos/o/r/git/tags/{TAG_OBJECT}",
        f"https://api.github.com/repos/o/r/git/trees/{TREE}",
        "https://token@api.github.com/repos/o/r/git/ref/tags/v1.2.3",
        "https://api.github.com/repos/o/r/git/ref/tags/v1.2.3?redirect=1",
    ]:
        with pytest.raises(ValueError, match="untrusted GitHub tag binding URL"):
            verifier._validate_tag_binding_url(url)


def test_postrelease_workflow_passes_expected_identity_and_token_via_stdin() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("  verify-github-release:", 1)[1]

    assert "--token-stdin" in job
    assert 'printf \'%s\' "$GITHUB_RELEASE_VERIFY_TOKEN" |' in job
    assert "/usr/bin/env -i /usr/bin/python3 -I -S -B" in job
    assert "GITHUB_RELEASE_VERIFY_TOKEN: ${{ github.token }}" in job
    assert '--tag-object "${{ needs.verify.outputs.tag_object }}"' in job
    assert '--commit "${{ needs.verify.outputs.commit }}"' in job
    assert '--tree "${{ needs.verify.outputs.tree }}"' in job
    assert "token" not in inspect.signature(verifier.verify_public_release).parameters
