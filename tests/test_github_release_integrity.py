"""Fail-closed checks for the public GitHub Release read-back."""

import hashlib
import importlib
import io
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from scripts.verify_github_release import (
    ExpectedAsset,
    collect_release_assets,
    expected_release_assets,
    verify_downloaded_assets,
    verify_public_release,
    verify_release_metadata,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
CHECKOUT = "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5"
DOWNLOAD = "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
UPLOAD = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
SOFTPROPS_RELEASE = "softprops/action-gh-release@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65"
CREATE_APP_TOKEN = (
    "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1"
)
EXACT_RELEASE_ACTION = (
    "robotlearning123/gpt2agent/.github/actions/publish-exact-github-release@"
    "15f56b2c16c5923e81df9428c69256237a004c20"
)
TAG_OBJECT = "1" * 40
COMMIT = "2" * 40
TREE = "3" * 40


def _workflow_job(name: str) -> list[str]:
    lines = RELEASE_WORKFLOW.read_text(encoding="utf-8").splitlines()
    marker = f"  {name}:"
    start = lines.index(marker)
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index])
        ),
        len(lines),
    )
    return lines[start:end]


def _job_permissions(job: list[str]) -> dict[str, str]:
    start = job.index("    permissions:")
    permissions: dict[str, str] = {}
    for line in job[start + 1 :]:
        match = re.fullmatch(r"      ([a-z-]+): ([a-z]+)(?:\s+#.*)?", line)
        if match:
            permissions[match.group(1)] = match.group(2)
        elif line and not line.startswith("      "):
            break
    return permissions


def _job_actions(job: list[str]) -> list[str]:
    actions: list[str] = []
    for line in job:
        match = re.match(r"^\s+(?:- )?uses: ([^\s#]+)", line)
        if match:
            actions.append(match.group(1))
    return actions


def _block_scalar(job: list[str], key: str) -> list[str]:
    marker = f"          {key}: |"
    start = job.index(marker)
    values: list[str] = []
    for line in job[start + 1 :]:
        if line.startswith("            "):
            values.append(line.strip())
        elif line:
            break
    return values


def _closed_local_assets(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "gpt2agent-1.2.3-py3-none-any.whl").write_bytes(b"wheel-bytes")
    (dist / "gpt2agent-1.2.3.tar.gz").write_bytes(b"sdist-bytes")
    evidence = tmp_path / "release-workflow-artifacts.json"
    evidence.write_bytes(b'{"evidence":true}\n')
    return dist, evidence, expected_release_assets(dist, evidence)


def _api_assets(expected: Mapping[str, Any]) -> list[dict[str, Any]]:
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


def _release(
    *,
    body: str = "release notes\n",
    draft: bool = False,
    prerelease: bool = False,
    immutable: bool = True,
):
    return {
        "id": 42,
        "tag_name": "v1.2.3",
        "name": "v1.2.3",
        "body": body,
        "draft": draft,
        "prerelease": prerelease,
        "immutable": immutable,
    }


def _exact_tag_fetch(url: str, token: str):
    assert token == "tag-token"
    if url.endswith("/git/ref/tags/v1.2.3"):
        return {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "tag", "sha": TAG_OBJECT},
        }, {}
    if url.endswith(f"/git/tags/{TAG_OBJECT}"):
        return {
            "sha": TAG_OBJECT,
            "tag": "v1.2.3",
            "object": {"type": "commit", "sha": COMMIT},
        }, {}
    if url.endswith(f"/git/commits/{COMMIT}"):
        return {"sha": COMMIT, "tree": {"sha": TREE}}, {}
    raise AssertionError(f"unexpected tag binding URL: {url}")


def test_public_github_release_verifier_exists() -> None:
    verifier = PROJECT_ROOT / "scripts" / "verify_github_release.py"

    assert verifier.is_file()


def test_public_github_release_verifier_exposes_closed_verification_boundaries() -> None:
    verifier = importlib.import_module("scripts.verify_github_release")

    boundaries = (
        "expected_release_assets",
        "verify_release_metadata",
        "collect_release_assets",
        "verify_downloaded_assets",
        "verify_public_release",
        "_fetch_json",
        "_download_asset",
        "_read_notes",
    )
    assert all(callable(getattr(verifier, name, None)) for name in boundaries)


def test_release_notes_prep_job_is_explicitly_read_only() -> None:
    job = _workflow_job("prepare-release-notes")
    text = "\n".join(job)

    assert _job_permissions(job) == {"actions": "read", "contents": "read"}
    assert "    needs: [pypi-canary, verify]" in job
    assert CHECKOUT in _job_actions(job)
    assert UPLOAD in _job_actions(job)
    assert "python scripts/verify_release.py" in text
    assert "--print-changelog-section" in text
    assert "name: release-notes-${{ github.run_id }}" in text
    assert "path: release-notes/release_notes.md" in text


def test_github_release_draft_writer_is_action_only_and_closed_to_approved_pins() -> None:
    job = _workflow_job("github-release-draft")
    text = "\n".join(job)
    actions = _job_actions(job)

    assert _job_permissions(job) == {"actions": "read", "contents": "write"}
    assert "    outputs:" in job
    assert "      release_id: ${{ steps.release.outputs.id }}" in job
    assert not any(re.match(r"^\s+(?:- )?run:", line) for line in job)
    assert not any(action.startswith("actions/checkout@") for action in actions)
    assert actions == [DOWNLOAD, DOWNLOAD, DOWNLOAD, SOFTPROPS_RELEASE]
    assert all(re.search(r"@[0-9a-f]{40}\Z", action) for action in actions)
    assert "name: release-notes-${{ github.run_id }}" in text
    assert (
        "body_path: ${{ runner.temp }}/github-release-${{ github.run_id }}-"
        "${{ github.run_attempt }}/notes/release_notes.md" in text
    )
    files = _block_scalar(job, "files")
    assert files == [
        "${{ runner.temp }}/github-release-${{ github.run_id }}-"
        "${{ github.run_attempt }}/dist/gpt2agent-"
        "${{ needs.verify.outputs.distribution_version }}-py3-none-any.whl",
        "${{ runner.temp }}/github-release-${{ github.run_id }}-"
        "${{ github.run_attempt }}/dist/gpt2agent-"
        "${{ needs.verify.outputs.distribution_version }}.tar.gz",
        "${{ runner.temp }}/github-release-${{ github.run_id }}-"
        "${{ github.run_attempt }}/release-evidence/release-workflow-artifacts.json",
    ]
    assert all("*" not in path for path in files)
    assert "          fail_on_unmatched_files: true" in job
    assert "          overwrite_files: false" in job
    assert "          draft: true" in job
    assert "        id: release" in job


def test_github_release_publisher_is_action_only_and_binds_exact_draft_id() -> None:
    job = _workflow_job("github-release")
    text = "\n".join(job)

    assert _job_permissions(job) == {"actions": "read", "contents": "write"}
    assert "    needs: [github-release-draft, verify]" in job
    assert "    environment: release-settings-read" in job
    assert not any(re.match(r"^\s+(?:- )?run:", line) for line in job)
    assert not any(action.startswith("actions/checkout@") for action in _job_actions(job))
    assert _job_actions(job) == [
        DOWNLOAD,
        DOWNLOAD,
        DOWNLOAD,
        CREATE_APP_TOKEN,
        EXACT_RELEASE_ACTION,
    ]
    assert all(re.search(r"@[0-9a-f]{40}\Z", action) for action in _job_actions(job))
    assert "          name: release-notes-${{ github.run_id }}" in job
    assert "          release-id: ${{ needs.github-release-draft.outputs.release_id }}" in job
    assert "          tag: ${{ github.ref_name }}" in job
    assert "          tag-object: ${{ needs.verify.outputs.tag_object }}" in job
    assert "          commit: ${{ needs.verify.outputs.commit }}" in job
    assert "          tree: ${{ needs.verify.outputs.tree }}" in job
    assert "          version: ${{ needs.verify.outputs.distribution_version }}" in job
    assert "          expected-prerelease: ${{ contains(github.ref_name, '-rc') ||" in text
    assert "          github-token: ${{ github.token }}" in job
    assert (
        "          immutability-token: "
        "${{ steps.release-settings-token.outputs.token }}" in job
    )
    assert "          permission-administration: read" in job
    assert not any(
        re.fullmatch(r"\s+permission-(?!administration:)[a-z0-9-]+:.*", line)
        for line in job
    )
    assert "          files:" not in text
    assert SOFTPROPS_RELEASE not in _job_actions(job)


def test_github_release_readback_job_is_read_only_and_closes_public_bytes() -> None:
    job = _workflow_job("verify-github-release")
    text = "\n".join(job)

    assert _job_permissions(job) == {"actions": "read", "contents": "read"}
    assert "    needs: [github-release, prepare-release-notes, verify]" in job
    assert _job_actions(job).count(DOWNLOAD) == 3
    assert CHECKOUT in _job_actions(job)
    assert "scripts/verify_github_release.py" in text
    assert '--tag "$GITHUB_REF_NAME"' in text
    assert "--attempts 7 --delay 10" in text
    assert "--expected-prerelease \"${{ contains(github.ref_name, '-rc') ||" in text
    assert "${{ github.run_id }}-${{ github.run_attempt }}" in text
    assert "contents: write" not in text
    assert "GH_TOKEN" not in text
    assert "--token-stdin" in text
    assert 'printf \'%s\' "$GITHUB_RELEASE_VERIFY_TOKEN" |' in text
    assert "/usr/bin/env -i /usr/bin/python3 -I -S -B" in text
    assert "GITHUB_RELEASE_VERIFY_TOKEN: ${{ github.token }}" in text
    assert '--tag-object "${{ needs.verify.outputs.tag_object }}"' in text
    assert '--commit "${{ needs.verify.outputs.commit }}"' in text
    assert '--tree "${{ needs.verify.outputs.tree }}"' in text
    assert "--github-token" not in text


def test_candidate_executing_release_jobs_do_not_inherit_actions_read() -> None:
    test_job = _workflow_job("test")
    canary_job = _workflow_job("pypi-canary")

    assert _job_permissions(test_job) == {"contents": "read"}
    assert "    permissions: {}" in canary_job


def test_expected_release_assets_are_exact_regular_files(tmp_path: Path) -> None:
    dist, evidence, expected = _closed_local_assets(tmp_path)

    assert set(expected) == {
        "gpt2agent-1.2.3-py3-none-any.whl",
        "gpt2agent-1.2.3.tar.gz",
        "release-workflow-artifacts.json",
    }
    for name, record in expected.items():
        path = evidence if name == evidence.name else dist / name
        assert record.path == path
        assert record.size == path.stat().st_size
        assert record.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("entry_kind", ["extra", "symlink", "directory"])
def test_expected_release_assets_reject_open_or_nonregular_local_sets(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "gpt2agent-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    sdist = dist / "gpt2agent-1.2.3.tar.gz"
    if entry_kind == "symlink":
        target = tmp_path / "sdist-target"
        target.write_bytes(b"sdist")
        sdist.symlink_to(target)
    elif entry_kind == "directory":
        sdist.mkdir()
    else:
        sdist.write_bytes(b"sdist")
        (dist / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    evidence = tmp_path / "release-workflow-artifacts.json"
    evidence.write_bytes(b"{}\n")

    with pytest.raises(ValueError, match="regular file|no other entries"):
        expected_release_assets(dist, evidence)


def test_expected_release_assets_rejects_symlinked_evidence(tmp_path: Path) -> None:
    dist, evidence, _ = _closed_local_assets(tmp_path)
    target = tmp_path / "evidence-target"
    evidence.rename(target)
    evidence.symlink_to(target)

    with pytest.raises(ValueError, match="release evidence must be a regular file"):
        expected_release_assets(dist, evidence)


def test_expected_release_assets_rejects_oversized_local_asset(tmp_path: Path) -> None:
    verifier = importlib.import_module("scripts.verify_github_release")
    limit = getattr(verifier, "MAX_ASSET_BYTES", None)
    assert isinstance(limit, int) and limit > 0
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "gpt2agent-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "gpt2agent-1.2.3.tar.gz").write_bytes(b"sdist")
    evidence = tmp_path / "release-workflow-artifacts.json"
    with evidence.open("wb") as stream:
        stream.truncate(limit + 1)

    with pytest.raises(ValueError, match="asset exceeds size limit"):
        expected_release_assets(dist, evidence)


def test_release_metadata_accepts_exact_body_state_and_closed_asset_set(tmp_path: Path) -> None:
    _, _, expected = _closed_local_assets(tmp_path)
    assets = _api_assets(expected)

    ids = verify_release_metadata(
        _release(),
        assets,
        tag="v1.2.3",
        expected_body="release notes\n",
        expected_prerelease=False,
        expected_assets=expected,
    )

    assert ids == {asset["name"]: asset["id"] for asset in assets}


@pytest.mark.parametrize("immutable", [False, None])
def test_release_metadata_rejects_mutable_or_unreported_release(
    tmp_path: Path,
    immutable: bool | None,
) -> None:
    _, _, expected = _closed_local_assets(tmp_path)
    release = _release()
    if immutable is None:
        release.pop("immutable")
    else:
        release["immutable"] = immutable

    with pytest.raises(ValueError, match="immutable"):
        verify_release_metadata(
            release,
            _api_assets(expected),
            tag="v1.2.3",
            expected_body="release notes\n",
            expected_prerelease=False,
            expected_assets=expected,
        )


def test_release_metadata_rejects_extra_asset(tmp_path: Path) -> None:
    _, _, expected = _closed_local_assets(tmp_path)
    assets = _api_assets(expected)
    assets.append(
        {
            "id": 999,
            "name": "stowaway.txt",
            "state": "uploaded",
            "size": 0,
            "digest": "sha256:" + "0" * 64,
        }
    )

    with pytest.raises(ValueError, match="exact GitHub Release asset names"):
        verify_release_metadata(
            _release(),
            assets,
            tag="v1.2.3",
            expected_body="release notes\n",
            expected_prerelease=False,
            expected_assets=expected,
        )


def test_release_metadata_rejects_missing_asset_from_incomplete_collection(
    tmp_path: Path,
) -> None:
    _, _, expected = _closed_local_assets(tmp_path)
    assets = _api_assets(expected)[:-1]

    with pytest.raises(ValueError, match="exact GitHub Release asset names"):
        verify_release_metadata(
            _release(),
            assets,
            tag="v1.2.3",
            expected_body="release notes\n",
            expected_prerelease=False,
            expected_assets=expected,
        )


def test_release_metadata_rejects_stale_asset_digest(tmp_path: Path) -> None:
    _, _, expected = _closed_local_assets(tmp_path)
    assets = _api_assets(expected)
    assets[0]["digest"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="digest"):
        verify_release_metadata(
            _release(),
            assets,
            tag="v1.2.3",
            expected_body="release notes\n",
            expected_prerelease=False,
            expected_assets=expected,
        )


def test_release_metadata_rejects_wrong_body_including_newline_drift(tmp_path: Path) -> None:
    _, _, expected = _closed_local_assets(tmp_path)

    with pytest.raises(ValueError, match="body does not exactly match"):
        verify_release_metadata(
            _release(body="release notes"),
            _api_assets(expected),
            tag="v1.2.3",
            expected_body="release notes\n",
            expected_prerelease=False,
            expected_assets=expected,
        )


def test_release_metadata_rejects_wrong_release_name(tmp_path: Path) -> None:
    _, _, expected = _closed_local_assets(tmp_path)
    release = _release()
    release["name"] = "unreviewed title"

    with pytest.raises(ValueError, match="name does not match"):
        verify_release_metadata(
            release,
            _api_assets(expected),
            tag="v1.2.3",
            expected_body="release notes\n",
            expected_prerelease=False,
            expected_assets=expected,
        )


@pytest.mark.parametrize(
    ("release_overrides", "expected_prerelease", "message"),
    [
        ({"draft": True}, False, "must not be a draft"),
        ({"prerelease": True}, False, "prerelease flag"),
        ({"prerelease": False}, True, "prerelease flag"),
    ],
)
def test_release_metadata_rejects_wrong_draft_or_prerelease_state(
    tmp_path: Path,
    release_overrides: dict[str, bool],
    expected_prerelease: bool,
    message: str,
) -> None:
    _, _, expected = _closed_local_assets(tmp_path)
    release = _release()
    release.update(release_overrides)

    with pytest.raises(ValueError, match=message):
        verify_release_metadata(
            release,
            _api_assets(expected),
            tag="v1.2.3",
            expected_body="release notes\n",
            expected_prerelease=expected_prerelease,
            expected_assets=expected,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda assets: assets[0].update(size=assets[0]["size"] + 1), "size"),
        (lambda assets: assets[0].update(state="new"), "uploaded state"),
        (lambda assets: assets[1].update(id=assets[0]["id"]), "unique IDs"),
        (lambda assets: assets[1].update(name=assets[0]["name"]), "duplicate asset name"),
    ],
)
def test_release_metadata_rejects_wrong_size_state_or_duplicate_identity(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    _, _, expected = _closed_local_assets(tmp_path)
    assets = _api_assets(expected)
    mutation(assets)

    with pytest.raises(ValueError, match=message):
        verify_release_metadata(
            _release(),
            assets,
            tag="v1.2.3",
            expected_body="release notes\n",
            expected_prerelease=False,
            expected_assets=expected,
        )


def test_release_asset_collection_follows_every_pagination_link() -> None:
    first_url = (
        "https://api.github.com/repos/robotlearning123/gpt2agent/releases/42/assets?per_page=100"
    )
    second_url = f"{first_url}&page=2"
    calls: list[str] = []

    def fetch_json(url: str, token: str) -> tuple[Any, Mapping[str, str]]:
        calls.append(url)
        assert token == "token"
        if url == first_url:
            return ([{"id": 1}], {"Link": f'<{second_url}>; rel="next"'})
        if url == second_url:
            return ([{"id": 2}], {})
        raise AssertionError(f"unexpected URL: {url}")

    assets = collect_release_assets(
        "robotlearning123/gpt2agent",
        42,
        "token",
        fetch_json=fetch_json,
    )

    assert assets == [{"id": 1}, {"id": 2}]
    assert calls == [first_url, second_url]


def test_release_asset_collection_rejects_pagination_cycles() -> None:
    first_url = (
        "https://api.github.com/repos/robotlearning123/gpt2agent/releases/42/assets?per_page=100"
    )

    repeated_url = f"{first_url}&page=2"

    def fetch_json(url: str, token: str) -> tuple[Any, Mapping[str, str]]:
        assert url in {first_url, repeated_url}
        assert token == "token"
        return ([], {"Link": f'<{repeated_url}>; rel="next"'})

    with pytest.raises(ValueError, match="pagination cycle"):
        collect_release_assets(
            "robotlearning123/gpt2agent",
            42,
            "token",
            fetch_json=fetch_json,
        )


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.invalid/repos/robotlearning123/gpt2agent/releases/42/assets"
        "?per_page=100&page=2",
        "https://api.github.com/repos/robotlearning123/other/releases/42/assets"
        "?per_page=100&page=2",
        "https://api.github.com/repos/robotlearning123/gpt2agent/releases/43/assets"
        "?per_page=100&page=2",
        "https://token@api.github.com/repos/robotlearning123/gpt2agent/releases/42/assets"
        "?per_page=100&page=2",
        "https://api.github.com/repos/robotlearning123/gpt2agent/releases/42/assets"
        "?per_page=100&page=2#fragment",
        "https://api.github.com/repos/robotlearning123/gpt2agent/releases/42/assets"
        "?per_page=100&page=2&page=3",
        "https://api.github.com/repos/robotlearning123/gpt2agent/releases/42/assets"
        "?per_page=100&page=2&token=leak",
    ],
)
def test_release_asset_collection_rejects_untrusted_pagination_before_auth(
    next_url: str,
) -> None:
    first_url = (
        "https://api.github.com/repos/robotlearning123/gpt2agent/releases/42/assets?per_page=100"
    )
    calls: list[str] = []

    def fetch_json(url: str, token: str) -> tuple[Any, Mapping[str, str]]:
        calls.append(url)
        assert token == "secret-token"
        return ([], {"Link": f'<{next_url}>; rel="next"'})

    with pytest.raises(ValueError, match="untrusted GitHub pagination URL"):
        collect_release_assets(
            "robotlearning123/gpt2agent",
            42,
            "secret-token",
            fetch_json=fetch_json,
        )
    assert calls == [first_url]


def test_release_asset_collection_caps_items() -> None:
    def fetch_json(url: str, token: str) -> tuple[Any, Mapping[str, str]]:
        assert token == "token"
        return ([{"id": index} for index in range(1001)], {})

    with pytest.raises(ValueError, match="asset collection exceeds"):
        collect_release_assets(
            "robotlearning123/gpt2agent",
            42,
            "token",
            fetch_json=fetch_json,
        )


def test_github_http_auth_errors_do_not_expose_token_or_response_body(tmp_path: Path) -> None:
    verifier = importlib.import_module("scripts.verify_github_release")
    fetch_json = getattr(verifier, "_fetch_json", None)
    download_asset = getattr(verifier, "_download_asset", None)
    assert callable(fetch_json)
    assert callable(download_asset)
    token = "do-not-print-this-token"
    body = b"do-not-print-this-response-body"

    def deny(request: Any, timeout: int) -> Any:
        assert request.get_header("Authorization") == f"Bearer {token}"
        assert "Authorization" not in request.headers
        assert timeout == 30
        raise HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(body))

    with pytest.raises(ValueError) as fetch_error:
        fetch_json("https://api.github.com/repos/o/r/releases/tags/v1.2.3", token, opener=deny)
    with pytest.raises(ValueError) as download_error:
        download_asset(
            "o/r",
            123,
            tmp_path / "asset.whl",
            token,
            ExpectedAsset(tmp_path / "expected.whl", 5, "0" * 64),
            opener=deny,
        )

    combined = f"{fetch_error.value}\n{download_error.value}"
    assert token not in combined
    assert body.decode() not in combined
    assert "HTTP 401" in combined


def test_public_github_requests_can_be_decisively_unauthenticated() -> None:
    verifier = importlib.import_module("scripts.verify_github_release")

    request = verifier._request(
        "https://api.github.com/repos/o/r/releases/tags/v1.2.3",
        "",
        accept="application/vnd.github+json",
    )

    assert request.get_header("Authorization") is None


class _BytesResponse:
    def __init__(self, payload: bytes, headers: Mapping[str, str] | None = None) -> None:
        self._stream = io.BytesIO(payload)
        self.headers = headers if headers is not None else {}
        self.read_sizes: list[int] = []

    def __enter__(self) -> "_BytesResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._stream.read(size)


class _DuplicateHeaders(dict[str, str]):
    def items(self):
        return [
            ("Content-Length", "4"),
            ("Content-Length", "3"),
        ]


def test_github_json_response_is_bounded_before_parsing() -> None:
    verifier = importlib.import_module("scripts.verify_github_release")
    limit = getattr(verifier, "MAX_JSON_BYTES", None)
    assert isinstance(limit, int) and limit > 0
    response = _BytesResponse(b"x" * (limit + 1))

    with pytest.raises(ValueError, match="JSON response exceeds size limit"):
        verifier._fetch_json(
            "https://api.github.com/repos/o/r/releases/tags/v1.2.3",
            "token",
            opener=lambda request, timeout: response,
        )

    assert response.read_sizes == [limit + 1]


def test_github_json_rejects_duplicate_content_length_headers() -> None:
    response = _BytesResponse(b"{}", _DuplicateHeaders())

    with pytest.raises(ValueError, match="malformed Content-Length"):
        importlib.import_module("scripts.verify_github_release")._fetch_json(
            "https://api.github.com/repos/o/r/releases/tags/v1.2.3",
            "",
            opener=lambda request, timeout: response,
        )


@pytest.mark.parametrize(
    ("headers", "payload", "message"),
    [
        ({"Content-Length": "4"}, b"abcd", "Content-Length"),
        ({}, b"abcd", "exceeds expected size"),
    ],
)
def test_asset_download_is_bounded_by_expected_size(
    tmp_path: Path,
    headers: Mapping[str, str],
    payload: bytes,
    message: str,
) -> None:
    expected_path = tmp_path / "expected.whl"
    expected_path.write_bytes(b"abc")
    expected = ExpectedAsset(expected_path, 3, hashlib.sha256(b"abc").hexdigest())
    destination = tmp_path / "download.whl"
    response = _BytesResponse(payload, headers)

    with pytest.raises(ValueError, match=message):
        importlib.import_module("scripts.verify_github_release")._download_asset(
            "o/r",
            123,
            destination,
            "token",
            expected,
            opener=lambda request, timeout: response,
        )

    assert not destination.exists()


def test_asset_download_rejects_duplicate_content_length_headers(tmp_path: Path) -> None:
    expected_path = tmp_path / "expected.whl"
    expected_path.write_bytes(b"abc")
    expected = ExpectedAsset(expected_path, 3, hashlib.sha256(b"abc").hexdigest())
    destination = tmp_path / "download.whl"
    response = _BytesResponse(b"abc", _DuplicateHeaders())

    with pytest.raises(ValueError, match="malformed Content-Length"):
        importlib.import_module("scripts.verify_github_release")._download_asset(
            "o/r",
            123,
            destination,
            "",
            expected,
            opener=lambda request, timeout: response,
        )

    assert not destination.exists()


def test_asset_download_removes_partial_file_after_midstream_http_error(
    tmp_path: Path,
) -> None:
    expected_path = tmp_path / "expected.whl"
    expected_path.write_bytes(b"abc")
    expected = ExpectedAsset(expected_path, 3, hashlib.sha256(b"abc").hexdigest())
    destination = tmp_path / "download.whl"
    response = _BytesResponse(b"ab")
    original_read = response.read
    reads = 0

    def fail_midstream(size: int = -1) -> bytes:
        nonlocal reads
        reads += 1
        if reads == 1:
            return original_read(size)
        raise HTTPError("https://objects.invalid/asset", 503, "Unavailable", {}, None)

    response.read = fail_midstream  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="HTTP 503"):
        importlib.import_module("scripts.verify_github_release")._download_asset(
            "o/r",
            123,
            destination,
            "",
            expected,
            opener=lambda request, timeout: response,
        )

    assert not destination.exists()


def test_release_notes_read_is_bounded(tmp_path: Path) -> None:
    verifier = importlib.import_module("scripts.verify_github_release")
    limit = getattr(verifier, "MAX_NOTES_BYTES", None)
    assert isinstance(limit, int) and limit > 0
    notes = tmp_path / "release_notes.md"
    notes.write_bytes(b"x" * (limit + 1))

    with pytest.raises(ValueError, match="release notes exceed size limit"):
        verifier._read_notes(notes)


def test_public_release_poll_retries_then_downloads_exact_numeric_asset_ids(
    tmp_path: Path,
) -> None:
    dist, evidence, expected = _closed_local_assets(tmp_path)
    notes = tmp_path / "release_notes.md"
    notes.write_text("release notes\n", encoding="utf-8")
    api_assets = _api_assets(expected)
    release_calls = 0
    sleeps: list[float] = []
    downloaded_ids: list[int] = []
    sources = {asset["id"]: expected[asset["name"]].path for asset in api_assets}

    def fetch_json(url: str, token: str) -> tuple[Any, Mapping[str, str]]:
        nonlocal release_calls
        assert token == ""
        if "/releases/tags/v1.2.3" in url:
            release_calls += 1
            body = "not propagated\n" if release_calls == 1 else "release notes\n"
            return (_release(body=body), {})
        assert url.endswith("/releases/42/assets?per_page=100")
        return (api_assets, {})

    def download_asset(
        repository: str,
        asset_id: int,
        destination: Path,
        token: str,
        expected_asset: ExpectedAsset,
    ) -> None:
        assert repository == "robotlearning123/gpt2agent"
        assert token == ""
        assert expected_asset.path == sources[asset_id]
        downloaded_ids.append(asset_id)
        destination.write_bytes(sources[asset_id].read_bytes())

    verified = verify_public_release(
        "robotlearning123/gpt2agent",
        "v1.2.3",
        notes,
        dist,
        evidence,
        tmp_path / "public-downloads",
        expected_prerelease=False,
        expected_tag_object=TAG_OBJECT,
        expected_commit=COMMIT,
        expected_tree=TREE,
        tag_token="tag-token",
        attempts=7,
        delay=10,
        fetch_json=fetch_json,
        download_asset=download_asset,
        tag_fetch_json=_exact_tag_fetch,
        sleep=sleeps.append,
    )

    assert release_calls == 2
    assert sleeps == [10]
    assert set(downloaded_ids) == {asset["id"] for asset in api_assets}
    assert verified.name.startswith("attempt-2-")
    verify_downloaded_assets(verified, expected)


def test_public_release_poll_removes_failed_attempt_before_retry(tmp_path: Path) -> None:
    dist, evidence, expected = _closed_local_assets(tmp_path)
    notes = tmp_path / "release_notes.md"
    notes.write_text("release notes\n", encoding="utf-8")
    api_assets = _api_assets(expected)
    sources = {asset["id"]: expected[asset["name"]].path for asset in api_assets}
    download_root = tmp_path / "public-downloads"
    calls = 0

    def fetch_json(url: str, token: str) -> tuple[Any, Mapping[str, str]]:
        if "/releases/tags/v1.2.3" in url:
            return (_release(), {})
        return (api_assets, {})

    def download_asset(
        repository: str,
        asset_id: int,
        destination: Path,
        token: str,
        expected_asset: ExpectedAsset,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("transient download failure")
        destination.write_bytes(sources[asset_id].read_bytes())

    def sleep(delay: float) -> None:
        assert delay == 0
        assert list(download_root.iterdir()) == []

    verified = verify_public_release(
        "robotlearning123/gpt2agent",
        "v1.2.3",
        notes,
        dist,
        evidence,
        download_root,
        expected_prerelease=False,
        expected_tag_object=TAG_OBJECT,
        expected_commit=COMMIT,
        expected_tree=TREE,
        tag_token="tag-token",
        attempts=2,
        delay=0,
        fetch_json=fetch_json,
        download_asset=download_asset,
        tag_fetch_json=_exact_tag_fetch,
        sleep=sleep,
    )

    assert [entry for entry in download_root.iterdir()] == [verified]


@pytest.mark.parametrize("unexpected_attempt", [1, 2])
def test_public_release_poll_removes_download_root_after_unexpected_failure(
    tmp_path: Path,
    unexpected_attempt: int,
) -> None:
    dist, evidence, expected = _closed_local_assets(tmp_path)
    notes = tmp_path / "release_notes.md"
    notes.write_text("release notes\n", encoding="utf-8")
    api_assets = _api_assets(expected)
    download_root = tmp_path / "public-downloads"
    attempt = 0

    def fetch_json(url: str, token: str) -> tuple[Any, Mapping[str, str]]:
        nonlocal attempt
        if "/releases/tags/v1.2.3" in url:
            attempt += 1
            return (_release(), {})
        return (api_assets, {})

    def download_asset(
        repository: str,
        asset_id: int,
        destination: Path,
        token: str,
        expected_asset: ExpectedAsset,
    ) -> None:
        if attempt < unexpected_attempt:
            raise ValueError("retryable download failure")
        raise RuntimeError("unexpected stream failure")

    with pytest.raises(RuntimeError, match="unexpected stream failure"):
        verify_public_release(
            "robotlearning123/gpt2agent",
            "v1.2.3",
            notes,
            dist,
            evidence,
            download_root,
            expected_prerelease=False,
            expected_tag_object=TAG_OBJECT,
            expected_commit=COMMIT,
            expected_tree=TREE,
            tag_token="tag-token",
            attempts=2,
            delay=0,
            fetch_json=fetch_json,
            download_asset=download_asset,
            tag_fetch_json=_exact_tag_fetch,
            sleep=lambda delay: None,
        )

    assert not download_root.exists()


def test_downloaded_assets_require_exact_regular_files_sizes_and_hashes(tmp_path: Path) -> None:
    _, _, expected = _closed_local_assets(tmp_path)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    for name, record in expected.items():
        (downloads / name).write_bytes(record.path.read_bytes())

    verify_downloaded_assets(downloads, expected)

    first = next(iter(expected))
    original = (downloads / first).read_bytes()
    (downloads / first).write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(ValueError, match="downloaded SHA-256"):
        verify_downloaded_assets(downloads, expected)


@pytest.mark.parametrize("entry_kind", ["extra", "missing", "symlink", "directory"])
def test_downloaded_assets_reject_open_or_nonregular_sets(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    _, _, expected = _closed_local_assets(tmp_path)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    for name, record in expected.items():
        (downloads / name).write_bytes(record.path.read_bytes())
    first = next(iter(expected))
    if entry_kind == "extra":
        (downloads / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    elif entry_kind == "missing":
        (downloads / first).unlink()
    elif entry_kind == "symlink":
        target = tmp_path / "download-target"
        (downloads / first).rename(target)
        (downloads / first).symlink_to(target)
    else:
        (downloads / first).unlink()
        (downloads / first).mkdir()

    with pytest.raises(ValueError, match="exact downloaded asset names|regular file"):
        verify_downloaded_assets(downloads, expected)
