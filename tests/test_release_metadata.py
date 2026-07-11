"""Tests for the release metadata and changelog verifier."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path

import pytest

import scripts.verify_main_ci as verify_main_ci
from scripts.verify_pypi_artifacts import artifact_hashes, compare_artifacts
from scripts.verify_main_ci import select_exact_main_run
from scripts.verify_release import changelog_section, distribution_version


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFIER = PROJECT_ROOT / "scripts" / "verify_release.py"


def _project(root: Path, version: str = "1.2.3") -> Path:
    (root / "gpt2agent").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / "pyproject.toml").write_text(f'[project]\nname = "gpt2agent"\nversion = "{version}"\n')
    (root / "gpt2agent" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    (root / "server.json").write_text(
        json.dumps({"version": version, "packages": [{"version": version}]})
    )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        f"## [{version}] - 2026-07-09\n\n"
        "### Fixed\n\n- Verified release metadata.\n"
    )
    return root


def _verify(root: Path, tag: str | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(VERIFIER), "--root", str(root)]
    if tag is not None:
        command.extend(["--tag", tag])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _git_env(**overrides: str) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        **overrides,
    }


def _release_source_guard() -> str:
    lines = (
        (PROJECT_ROOT / ".github" / "workflows" / "release.yml")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    step = lines.index("      - name: Require the tagged commit to be on origin/main")
    run = lines.index("        run: |", step + 1)
    assert run <= step + 2

    script: list[str] = []
    for line in lines[run + 1 :]:
        if line.startswith("          "):
            script.append(line[10:])
        elif not line:
            script.append("")
        else:
            break
    return "\n".join(script)


def _run_release_source_guard(
    tmp_path: Path,
    tag_kind: str,
    *,
    mismatched_event: bool = False,
    receipt_lines: tuple[str, ...] = ("account-receipt-sha256: " + "a" * 64,),
) -> subprocess.CompletedProcess[str]:
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"
    source.mkdir()
    _git(tmp_path, "init", "--bare", str(remote))
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Release Test")
    _git(source, "config", "user.email", "release-test@example.invalid")
    (source / "release.txt").write_text("main\n", encoding="utf-8")
    _git(source, "add", "release.txt")
    _git(source, "commit", "-m", "main release")
    main_sha = _git(source, "rev-parse", "HEAD")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "origin", "main")

    tag_sha = main_sha
    if tag_kind == "off-main":
        _git(source, "switch", "-c", "side")
        (source / "side.txt").write_text("side\n", encoding="utf-8")
        _git(source, "add", "side.txt")
        _git(source, "commit", "-m", "side release")
        tag_sha = _git(source, "rev-parse", "HEAD")

    if tag_kind in {"lightweight", "lightweight-decoy"}:
        _git(source, "tag", "v1.2.3", tag_sha)
    else:
        tag_command = ["tag", "-a", "v1.2.3", tag_sha, "-m", "v1.2.3"]
        for receipt_line in receipt_lines:
            tag_command.extend(["-m", receipt_line])
        _git(source, *tag_command)
    _git(source, "push", "origin", "refs/tags/v1.2.3")
    if tag_kind == "lightweight-decoy":
        decoy = "0/refs/tags/v1.2.3"
        _git(source, "tag", "-a", decoy, tag_sha, "-m", decoy)
        _git(source, "push", "origin", f"refs/tags/{decoy}")

    _git(tmp_path, "clone", "--branch", "main", str(remote), str(checkout))
    _git(checkout, "update-ref", "refs/tags/v1.2.3", main_sha)
    assert _git(checkout, "cat-file", "-t", "refs/tags/v1.2.3") == "commit"

    event_sha = "0" * 40 if mismatched_event else tag_sha
    return subprocess.run(
        ["bash", "-c", _release_source_guard()],
        cwd=checkout,
        env=_git_env(
            GITHUB_REF="refs/tags/v1.2.3",
            GITHUB_SHA=event_sha,
            GITHUB_OUTPUT=str(tmp_path / "github-output"),
        ),
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_verifier_accepts_consistent_tree(tmp_path) -> None:
    result = _verify(_project(tmp_path), "v1.2.3")

    assert result.returncode == 0, result.stderr
    assert "release metadata verified: 1.2.3" in result.stdout


@pytest.mark.parametrize(
    ("relative_path", "payload"),
    [
        (
            ".claude-plugin/plugin.json",
            '{"version":"1.2.4","version":"1.2.3"}',
        ),
        (
            "server.json",
            '{"version":"1.2.4","version":"1.2.3",'
            '"packages":[{"version":"1.2.3"}]}',
        ),
        (
            "server.json",
            '{"version":"1.2.3","packages":['
            '{"version":"1.2.4","version":"1.2.3"}]}',
        ),
    ],
)
def test_release_verifier_rejects_duplicate_json_keys(
    tmp_path: Path, relative_path: str, payload: str
) -> None:
    root = _project(tmp_path)
    (root / relative_path).write_text(payload, encoding="utf-8")

    result = _verify(root, "v1.2.3")

    assert result.returncode == 1
    assert f"{relative_path}: duplicate JSON key 'version'" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "release = '1.2.3'\n",
        '__version__ = "1.2.3"\n__version__ = "1.2.3"\n',
        '__version__ = __version__ = "1.2.3"\n',
    ],
)
def test_release_verifier_requires_exactly_one_top_level_package_version_assignment(
    tmp_path: Path, source: str
) -> None:
    root = _project(tmp_path)
    (root / "gpt2agent" / "__init__.py").write_text(source, encoding="utf-8")

    result = _verify(root, "v1.2.3")

    assert result.returncode == 1
    assert "exactly one top-level __version__ assignment" in result.stderr


def test_release_verifier_requires_literal_package_version_assignment(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "gpt2agent" / "__init__.py").write_text(
        'release = "1.2.3"\n__version__ = release\n',
        encoding="utf-8",
    )

    result = _verify(root, "v1.2.3")

    assert result.returncode == 1
    assert "__version__ assignment must be a literal string" in result.stderr


@pytest.mark.parametrize(
    "nested_override",
    [
        'if True:\n    __version__ = "1.2.4"\n',
        'try:\n    pass\nfinally:\n    __version__ = "1.2.4"\n',
        'for __version__ in ["1.2.4"]:\n    pass\n',
        'with manager as __version__:\n    pass\n',
        'match "1.2.4":\n    case __version__:\n        pass\n',
        'factory = lambda value=(__version__ := "1.2.4"): value\n',
    ],
)
def test_release_verifier_rejects_nested_module_scope_version_bindings(
    tmp_path: Path, nested_override: str
) -> None:
    root = _project(tmp_path)
    (root / "gpt2agent" / "__init__.py").write_text(
        '__version__ = "1.2.3"\n' + nested_override,
        encoding="utf-8",
    )

    result = _verify(root, "v1.2.3")

    assert result.returncode == 1
    assert "exactly one top-level __version__ assignment" in result.stderr


def test_release_verifier_ignores_function_and_class_local_version_bindings(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    (root / "gpt2agent" / "__init__.py").write_text(
        '__version__: str = "1.2.3"\n'
        "def helper() -> str:\n"
        '    __version__ = "function local"\n'
        "    return __version__\n"
        "class Metadata:\n"
        '    __version__ = "class local"\n',
        encoding="utf-8",
    )

    result = _verify(root, "v1.2.3")

    assert result.returncode == 0, result.stderr


def test_changelog_section_uses_exact_version_not_regex_near_match(tmp_path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n"
        "## [1x2y3] - 2026-07-08\n\n- Wrong near match.\n\n"
        "## [1.2.3] - 2026-07-09\n\n- Exact release.\n\n"
        "## [1.2.2] - 2026-07-01\n\n- Older release.\n",
        encoding="utf-8",
    )

    section = changelog_section(changelog, "1.2.3")

    assert section.startswith("## [1.2.3] - 2026-07-09\n")
    assert "Exact release" in section
    assert "Wrong near match" not in section
    assert "Older release" not in section


def test_changelog_section_rejects_impossible_calendar_date(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [1.2.3] - 2026-02-30\n\n- Release.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="valid calendar date"):
        changelog_section(changelog, "1.2.3")


def test_changelog_section_rejects_html_comment_only_notes(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [1.2.3] - 2026-07-09\n\n<!-- hidden note -->\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no release notes"):
        changelog_section(changelog, "1.2.3")


def test_release_workflow_uses_shared_exact_changelog_extractor() -> None:
    release = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "verify_release.py --print-changelog-section" in release
    assert 'if ($0 ~ "## \\\\[" ver "\\\\]")' not in release


def test_active_plugin_surfaces_advertise_current_tool_count() -> None:
    marketplace = json.loads(
        (PROJECT_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    installer = (PROJECT_ROOT / "gpt2agent" / "install.py").read_text(encoding="utf-8")

    assert "32 MCP tools" in marketplace["plugins"][0]["description"]
    assert "pre-approves all 32 MCP tools" in installer


@pytest.mark.parametrize(
    ("project_version", "expected_distribution_version"),
    [
        ("1.2.3", "1.2.3"),
        ("1.2.3-alpha1", "1.2.3a1"),
        ("1.2.3-beta2", "1.2.3b2"),
        ("1.2.3-rc10", "1.2.3rc10"),
    ],
)
def test_distribution_version_normalizes_supported_semver_prereleases(
    project_version: str, expected_distribution_version: str
) -> None:
    assert distribution_version(project_version) == expected_distribution_version


@pytest.mark.parametrize("version", ["1.2.3-preview1", "1.2.3-rc.1", "1.2.3+build.1"])
def test_release_verifier_rejects_unsupported_version_suffixes(
    tmp_path: Path, version: str
) -> None:
    result = _verify(_project(tmp_path, version), f"v{version}")

    assert result.returncode == 1
    assert "not supported" in result.stderr


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("package", "gpt2agent/__init__.py"),
        ("plugin", ".claude-plugin/plugin.json"),
        ("server", "server.json version"),
        ("server_package", "server.json packages[0].version"),
        ("changelog_missing", "CHANGELOG.md"),
        ("changelog_empty", "CHANGELOG.md"),
    ],
)
def test_release_verifier_names_each_inconsistent_surface(tmp_path, mutation, expected) -> None:
    root = _project(tmp_path)
    if mutation == "package":
        (root / "gpt2agent" / "__init__.py").write_text('__version__ = "1.2.4"\n')
    elif mutation == "plugin":
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "1.2.4"}))
    elif mutation == "server":
        (root / "server.json").write_text(
            json.dumps({"version": "1.2.4", "packages": [{"version": "1.2.3"}]})
        )
    elif mutation == "server_package":
        (root / "server.json").write_text(
            json.dumps({"version": "1.2.3", "packages": [{"version": "1.2.4"}]})
        )
    elif mutation == "changelog_missing":
        (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    elif mutation == "changelog_empty":
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [1.2.3] - 2026-07-09\n\n## [1.2.2] - 2026-07-01\n"
        )

    result = _verify(root, "v1.2.3")

    assert result.returncode == 1
    assert expected in result.stderr


def test_release_verifier_rejects_mismatched_tag(tmp_path) -> None:
    result = _verify(_project(tmp_path), "v1.2.4")

    assert result.returncode == 1
    assert "tag v1.2.4" in result.stderr


def test_release_verifier_accepts_live_tree_without_tag() -> None:
    result = _verify(PROJECT_ROOT)

    assert result.returncode == 0, result.stderr


def test_pypi_artifact_verifier_rejects_unexpected_dist_entries(tmp_path) -> None:
    wheel = tmp_path / "gpt2agent-1.2.3-py3-none-any.whl"
    sdist = tmp_path / "gpt2agent-1.2.3.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")

    hashes = artifact_hashes(tmp_path)

    assert set(hashes) == {wheel.name, sdist.name}
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes.values())

    (tmp_path / "unexpected.txt").write_text("unexpected")

    with pytest.raises(ValueError, match="no other entries"):
        artifact_hashes(tmp_path)


@pytest.mark.parametrize(
    ("remote", "require_complete", "expected"),
    [
        ({"wheel.whl": "aaa"}, False, []),
        ({"wheel.whl": "aaa", "source.tar.gz": "bbb"}, True, []),
        ({"wheel.whl": "wrong"}, False, ["SHA-256 mismatch for wheel.whl"]),
        ({"other.whl": "aaa"}, False, ["PyPI has unexpected artifact other.whl"]),
        ({"wheel.whl": "aaa"}, True, ["PyPI is missing artifact source.tar.gz"]),
        (None, False, []),
        (None, True, ["PyPI release does not exist"]),
    ],
)
def test_pypi_artifact_verifier_fails_closed_on_drift(remote, require_complete, expected) -> None:
    local = {"wheel.whl": "aaa", "source.tar.gz": "bbb"}

    assert compare_artifacts(local, remote, require_complete=require_complete) == expected


def test_pypi_pre_publish_verifier_rejects_partial_existing_release() -> None:
    local = {"wheel.whl": "aaa", "source.tar.gz": "new-sdist"}
    remote = {"wheel.whl": "aaa"}

    assert compare_artifacts(
        local,
        remote,
        require_complete=False,
        require_absent=True,
    ) == ["PyPI release already exists; refusing rebuilt artifacts"]


def test_workflow_actions_are_pinned_to_full_commit_shas() -> None:
    workflows = list((PROJECT_ROOT / ".github" / "workflows").glob("*.yml"))
    uses_pattern = re.compile(r"^\s*(?:-\s*)?uses:\s+([^@\s]+)@([^\s#]+)", re.MULTILINE)

    references = [
        (path.name, action, revision)
        for path in workflows
        for action, revision in uses_pattern.findall(path.read_text(encoding="utf-8"))
    ]

    assert references
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for _, _, revision in references)


def test_ci_checkouts_do_not_persist_credentials() -> None:
    lines = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    ).splitlines()
    checkout_indexes = [
        index for index, line in enumerate(lines) if "- uses: actions/checkout@" in line
    ]

    assert checkout_indexes
    for index in checkout_indexes:
        step_indent = len(lines[index]) - len(lines[index].lstrip())
        block: list[str] = []
        for line in lines[index + 1 :]:
            if line.strip() and len(line) - len(line.lstrip()) <= step_indent:
                break
            block.append(line.strip())
        assert "persist-credentials: false" in block


def test_release_source_guard_accepts_remote_annotated_tag_after_local_clobber(
    tmp_path: Path,
) -> None:
    result = _run_release_source_guard(tmp_path, "annotated")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("receipt_lines", "expected_error"),
    [
        ((), "exactly one account receipt SHA-256"),
        (("account-receipt-sha256: not-a-digest",), "must be 64 lowercase hex"),
        (
            (
                "account-receipt-sha256: " + "a" * 64,
                "account-receipt-sha256: " + "b" * 64,
            ),
            "exactly one account receipt SHA-256",
        ),
    ],
)
def test_release_source_guard_rejects_missing_or_malformed_receipt_digest(
    tmp_path: Path,
    receipt_lines: tuple[str, ...],
    expected_error: str,
) -> None:
    result = _run_release_source_guard(
        tmp_path,
        "annotated",
        receipt_lines=receipt_lines,
    )

    assert result.returncode == 1
    assert expected_error in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("tag_kind", "mismatched_event", "expected_error"),
    [
        ("lightweight", False, "must be an annotated tag"),
        ("lightweight-decoy", False, "must be an annotated tag"),
        ("annotated", True, "not event SHA"),
        ("off-main", False, "is not reachable from origin/main"),
    ],
)
def test_release_source_guard_rejects_invalid_remote_release_source(
    tmp_path: Path,
    tag_kind: str,
    mismatched_event: bool,
    expected_error: str,
) -> None:
    result = _run_release_source_guard(tmp_path, tag_kind, mismatched_event=mismatched_event)

    assert result.returncode == 1
    assert expected_error in result.stdout + result.stderr


def test_release_workflows_keep_required_source_and_artifact_gates() -> None:
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    readme_flat = re.sub(r"\s+", " ", readme)

    assert "name: Required checks" in ci
    assert "name: Windows package smoke" in ci
    assert "runs-on: windows-latest" in ci
    assert "working-directory: ${{ runner.temp }}" in ci
    assert "gpt2agent --version" in ci
    assert "name: MCP compatibility (${{ matrix.lane }})" in ci
    assert 'mcp_spec: "mcp==1.26.0"' in ci
    assert 'mcp_spec: "mcp>=1.26,<2"' in ci
    assert "name: Package dry-run" in ci
    assert "python -m build" in ci
    assert "python -m twine check dist/*" in ci
    assert 'scripts/package_smoke.sh dist "$PROJECT_VERSION" "$DIST_VERSION"' in ci
    assert (
        "needs: [quality, dependency-audit, test, windows-smoke, lint-installer, mcp-compat, package]"
        in ci
    )
    assert "PUBLIC_SURFACE" not in ci
    assert "workflow_dispatch:" not in release
    assert "  actions: read" in release
    assert "    timeout-minutes: 35" in release
    assert 'VERIFY_REF="refs/release-verification/tag"' in release
    assert 'git fetch --force --no-tags origin "$GITHUB_REF:$VERIFY_REF"' in release
    assert 'git cat-file -t "$VERIFY_REF"' in release
    assert "must be an annotated tag" in release
    assert 'TAG_COMMIT="$(git rev-parse "$VERIFY_REF^{}")"' in release
    assert 'if [ "$TAG_COMMIT" != "$GITHUB_SHA" ]; then' in release
    assert 'git merge-base --is-ancestor "$TAG_COMMIT" origin/main' in release
    assert 'git for-each-ref --format="%(contents)" "$VERIFY_REF"' in release
    assert "account-receipt-sha256:" in release
    assert "receipt_sha256:" in release
    assert 'git cat-file -t "$GITHUB_REF"' not in release
    assert "python scripts/verify_release.py --tag" in release
    assert "python scripts/verify_main_ci.py" in release
    assert '--commit "${{ steps.provenance.outputs.commit }}"' in release
    assert "distribution_version" in release
    assert "DIST_VERSION" in release
    assert "Test built artifacts in clean environments" in release
    assert 'scripts/package_smoke.sh dist "$PROJECT_VERSION" "$DIST_VERSION"' in release
    assert "Require the PyPI version to be absent before publication" in release
    assert "--require-absent --attempts 3 --delay 5" in release
    assert "skip-existing: true" in release
    assert "name: Verify published PyPI artifacts" in release
    assert "--require-complete --attempts 7 --delay 10" in release
    assert "name: Verify PyPI install in a clean environment" in release
    assert '"gpt2agent==$DIST_VERSION"' in release
    assert "needs: [pypi-canary, verify]" in release
    assert "scripts/release_evidence.py create" in release
    assert "scripts/release_evidence.py verify" in release
    assert "release-workflow-artifacts.json" in release
    assert 'gh pr view "$PR_NUMBER" --json mergeCommit,state' in readme
    assert (
        "```bash\nset -euo pipefail\ngit fetch --no-tags origin main:refs/remotes/origin/main"
    ) in readme
    assert "\ngit switch main\n" not in readme
    assert "git pull --ff-only origin main" not in readme
    assert 'git merge-base --is-ancestor "$RELEASE_SHA" origin/main' in readme
    assert "python scripts/verify_main_ci.py" in readme
    assert '--commit "$RELEASE_SHA"' in readme
    assert readme.index("python scripts/verify_main_ci.py") < readme.index(
        '"repos/$REPOSITORY/git/tags"'
    )
    assert (
        'test -z "$(git status --porcelain=v1 --untracked-files=all --ignored=matching)"' in readme
    )
    assert 'git switch --detach "$RELEASE_SHA"' in readme
    assert "trap 'git switch - >/dev/null || true' EXIT" in readme
    assert "awk -v ref=\"refs/tags/$TAG\" '$2 == ref { print $1 }'" in readme
    assert "GPT2AGENT_RELEASE_APP_TOKEN:?" in readme
    assert 'GH_TOKEN="$GPT2AGENT_RELEASE_APP_TOKEN" gh api --method POST' in readme
    assert '"repos/$REPOSITORY/git/tags"' in readme
    assert '--raw-field object="$RELEASE_SHA"' in readme
    assert '--raw-field type=commit' in readme
    assert '"repos/$REPOSITORY/git/refs"' in readme
    assert '--raw-field ref="refs/tags/$TAG"' in readme
    assert '--raw-field sha="$TAG_OBJECT_SHA"' in readme
    assert 'git tag -a "$TAG" "$RELEASE_SHA"' not in readme
    assert 'git push origin "refs/tags/$TAG"' not in readme
    assert 'test "$(git cat-file -t "$TAG")" = tag' in readme
    assert 'test "$(git rev-parse "$TAG")" = "$TAG_OBJECT_SHA"' in readme
    assert 'test "$(git rev-parse "$TAG^{}")" = "$RELEASE_SHA"' in readme
    assert "trap - EXIT" in readme
    assert "\ngit switch -\n```" in readme
    assert "Re-run failed jobs" in readme_flat
    assert "Do not re-run the whole workflow" in readme_flat
    assert not re.search(r"python scripts/verify_release\.py --tag v\d+\.\d+\.\d+", readme)


def test_exact_main_ci_selector_ignores_other_commits_branches_and_events() -> None:
    commit = "a" * 40
    payload = {
        "workflow_runs": [
            {
                "id": 1,
                "run_attempt": 9,
                "head_sha": "b" * 40,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 2,
                "run_attempt": 9,
                "head_sha": commit,
                "head_branch": "feature",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 3,
                "run_attempt": 9,
                "head_sha": commit,
                "head_branch": "main",
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 4,
                "run_attempt": 1,
                "head_sha": commit,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            },
        ]
    }

    state, run_id = select_exact_main_run(payload, commit)

    assert state == "success"
    assert run_id == 4


def test_exact_main_ci_selector_uses_latest_attempt_and_fails_closed() -> None:
    commit = "c" * 40
    payload = {
        "workflow_runs": [
            {
                "id": 10,
                "run_attempt": 1,
                "head_sha": commit,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 11,
                "run_attempt": 2,
                "head_sha": commit,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "failure",
            },
        ]
    }

    with pytest.raises(ValueError, match="exact main CI concluded failure"):
        select_exact_main_run(payload, commit)


def test_exact_main_ci_selector_reports_pending_or_missing_without_guessing() -> None:
    commit = "d" * 40
    pending = {
        "workflow_runs": [
            {
                "id": 12,
                "run_attempt": 1,
                "head_sha": commit,
                "head_branch": "main",
                "event": "push",
                "status": "in_progress",
                "conclusion": None,
            }
        ]
    }

    assert select_exact_main_run(pending, commit) == ("pending", 12)
    assert select_exact_main_run({"workflow_runs": []}, commit) == ("missing", None)
    with pytest.raises(ValueError, match="workflow_runs list"):
        select_exact_main_run({"workflow_runs": "wrong"}, commit)


class _NoNetworkHTTPTransport(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, *, redirect_to: str | None = None) -> None:
        self.redirect_to = redirect_to
        self.requests: list[urllib.request.Request] = []

    def http_open(self, request):  # noqa: ANN001
        return self._open(request)

    def https_open(self, request):  # noqa: ANN001
        return self._open(request)

    def _open(self, request):  # noqa: ANN001
        self.requests.append(request)
        headers = Message()
        if len(self.requests) == 1 and self.redirect_to is not None:
            headers["Location"] = self.redirect_to
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            return response
        headers["Content-Type"] = "application/json"
        response = urllib.response.addinfourl(
            io.BytesIO(b'{"workflow_runs": []}'), headers, request.full_url, 200
        )
        response.msg = "OK"
        return response


@pytest.mark.parametrize(
    "redirect_to",
    [
        "https://attacker.example/capture",
        "http://api.github.com/capture",
    ],
)
def test_exact_main_ci_opener_rejects_redirects_without_forwarding_token(
    redirect_to: str,
) -> None:
    source_url = "https://api.github.com/repos/owner/repo/actions/workflows/ci.yml/runs"
    transport = _NoNetworkHTTPTransport(redirect_to=redirect_to)
    opener = verify_main_ci._github_opener(transport)
    request = urllib.request.Request(
        source_url,
        headers={"Authorization": "Bearer SYNTHETIC_GITHUB_TOKEN"},
    )

    with pytest.raises(ValueError, match="redirects are forbidden"):
        opener.open(request, timeout=1)

    assert [item.full_url for item in transport.requests] == [source_url]
    assert transport.requests[0].get_header("Authorization") == "Bearer SYNTHETIC_GITHUB_TOKEN"


def test_exact_main_ci_fetch_preserves_non_redirect_behavior(monkeypatch) -> None:
    transport = _NoNetworkHTTPTransport()
    opener = verify_main_ci._github_opener(transport)
    monkeypatch.setattr(verify_main_ci, "_github_opener", lambda: opener)

    assert verify_main_ci._fetch_runs("owner/repo", "SYNTHETIC_GITHUB_TOKEN") == {
        "workflow_runs": []
    }
    assert len(transport.requests) == 1
    assert transport.requests[0].get_header("Authorization") == "Bearer SYNTHETIC_GITHUB_TOKEN"


def test_account_reported_quota_is_not_documented_as_a_fixed_limit() -> None:
    sse = (PROJECT_ROOT / "gpt2agent" / "sse.py").read_text(encoding="utf-8")

    assert "248 uses / reset cycle" not in sse


def test_mcp_dependency_is_bounded_to_stable_v1() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert '"mcp>=1.26,<2"' in pyproject
    assert requirements.splitlines().count("mcp>=1.26,<2") == 1
    assert "mcp>=1.26.0" not in pyproject + requirements


def test_curl_cffi_floor_excludes_cve_2026_33752() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert '"curl_cffi>=0.15.0"' in pyproject
    assert requirements.splitlines().count("curl_cffi>=0.15.0") == 1
    assert "curl_cffi>=0.11.0" not in pyproject + requirements
    assert "pip-audit==2.10.0" in ci
    assert "curl_cffi==0.15.0" in ci
    assert "DEPENDENCY_AUDIT_RESULT" in ci


def test_package_smoke_is_reused_in_ci_and_release() -> None:
    script = PROJECT_ROOT / "scripts" / "package_smoke.sh"
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert script.is_file()
    assert ci.count("scripts/package_smoke.sh") == 1
    assert release.count("scripts/package_smoke.sh") == 1
    assert "TMP_ROOT=$(mktemp -d)" not in release


def test_release_shell_paths_do_not_require_mapfile() -> None:
    package_smoke = (PROJECT_ROOT / "scripts" / "package_smoke.sh").read_text(
        encoding="utf-8"
    )
    release = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "mapfile" not in package_smoke
    assert "mapfile" not in release


def test_package_smoke_rejects_unexpected_dist_entries_before_install(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "gpt2agent-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "gpt2agent-1.2.3.tar.gz").write_bytes(b"sdist")
    (dist / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\necho 'unexpected python invocation' >&2\nexit 97\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "package_smoke.sh"), str(dist), "1.2.3", "1.2.3"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "no other entries" in result.stderr
    assert "unexpected python invocation" not in result.stderr


def test_package_smoke_checks_sdist_install_outside_extracted_source() -> None:
    script = (PROJECT_ROOT / "scripts" / "package_smoke.sh").read_text(encoding="utf-8")

    assert (
        'check_installed_package "$TMP_ROOT/sdist-venv" '
        '"$TMP_ROOT/sdist-check" "$SDIST_ROOT"' in script
    )
    assert '"$VENV_ROOT/bin/gpt2agent" --version' in script
    assert '"$VENV_ROOT/bin/python" -m gpt2agent --version' in script
    assert "from importlib.resources import files" in script
    assert "assert not module_path.is_relative_to(forbidden_source)" in script


def test_release_artifact_uploads_are_safe_to_rerun_in_the_same_run() -> None:
    release = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert release.count("actions/upload-artifact@") == 2
    assert release.count("          overwrite: true") == 2
    assert release.count("          if-no-files-found: error") == 2
    assert "name: dist\n          path: dist/\n          overwrite: true" in release
    assert (
        "name: release-evidence\n"
        "          path: release-evidence/release-workflow-artifacts.json\n"
        "          overwrite: true" in release
    )


def test_release_evidence_binds_source_receipt_and_artifact_hashes(tmp_path: Path) -> None:
    from scripts.release_evidence import build_manifest, verify_manifest

    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "gpt2agent-1.2.3-py3-none-any.whl"
    sdist = dist / "gpt2agent-1.2.3.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    commit = "1" * 40
    tree = "2" * 40
    tag_object = "3" * 40
    receipt = "4" * 64

    manifest = build_manifest(
        dist,
        tag="v1.2.3",
        tag_object=tag_object,
        commit=commit,
        tree=tree,
        receipt_sha256=receipt,
        repository="robotlearning123/gpt2agent",
        run_id="12345",
        run_attempt="1",
        job="build",
    )

    assert manifest["receipt_sha256"] == receipt
    assert manifest["source"] == {"commit": commit, "tree": tree}
    assert manifest["workflow"]["run_attempt"] == "1"
    assert [artifact["filename"] for artifact in manifest["artifacts"]] == [
        sdist.name,
        wheel.name,
    ]
    verify_manifest(
        manifest,
        dist,
        tag="v1.2.3",
        tag_object=tag_object,
        commit=commit,
        tree=tree,
        receipt_sha256=receipt,
        repository="robotlearning123/gpt2agent",
        run_id="12345",
        run_attempt="2",
    )

    manifest["workflow"]["job"] = "test"
    with pytest.raises(ValueError, match="workflow job must be build"):
        verify_manifest(
            manifest,
            dist,
            tag="v1.2.3",
            tag_object=tag_object,
            commit=commit,
            tree=tree,
            receipt_sha256=receipt,
            repository="robotlearning123/gpt2agent",
            run_id="12345",
            run_attempt="2",
        )
    manifest["workflow"]["job"] = "build"

    wheel.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="artifact metadata does not match"):
        verify_manifest(
            manifest,
            dist,
            tag="v1.2.3",
            tag_object=tag_object,
            commit=commit,
            tree=tree,
            receipt_sha256=receipt,
            repository="robotlearning123/gpt2agent",
            run_id="12345",
            run_attempt="2",
        )


def test_release_evidence_requires_build_job_and_exact_dist_contents(
    tmp_path: Path,
) -> None:
    from scripts.release_evidence import build_manifest

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "gpt2agent-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "gpt2agent-1.2.3.tar.gz").write_bytes(b"sdist")
    common = {
        "tag": "v1.2.3",
        "tag_object": "3" * 40,
        "commit": "1" * 40,
        "tree": "2" * 40,
        "receipt_sha256": "4" * 64,
        "repository": "robotlearning123/gpt2agent",
        "run_id": "12345",
        "run_attempt": "1",
    }

    with pytest.raises(ValueError, match="workflow job must be build"):
        build_manifest(dist, job="test", **common)

    (dist / "unexpected-directory").mkdir()
    with pytest.raises(ValueError, match="no other entries"):
        build_manifest(dist, job="build", **common)


def test_public_markdown_does_not_embed_operator_home_paths() -> None:
    markdown_files = [
        *PROJECT_ROOT.glob("*.md"),
        *(PROJECT_ROOT / "docs").rglob("*.md"),
        *(PROJECT_ROOT / "gpt2agent" / "skills").rglob("*.md"),
    ]

    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in markdown_files
        if "/home/robot/" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
