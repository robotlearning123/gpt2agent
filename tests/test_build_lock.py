"""Regression checks for the locked release-builder toolchain."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_INPUT = PROJECT_ROOT / "requirements-build.in"
BUILD_LOCK = PROJECT_ROOT / "requirements-build.txt"
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
DEPENDABOT_CONFIG = PROJECT_ROOT / ".github" / "dependabot.yml"

EXPECTED_BUILD_PINS = {
    "pip": "26.1.2",
    "build": "1.5.1",
    "twine": "6.2.0",
    "setuptools": "83.0.0",
    "wheel": "0.47.0",
}

EXPECTED_LOCKED_PROJECTS = {
    "build": "1.5.1",
    "certifi": "2026.6.17",
    "cffi": "2.1.0",
    "charset-normalizer": "3.4.9",
    "cryptography": "49.0.0",
    "docutils": "0.23",
    "id": "1.6.1",
    "idna": "3.18",
    "jaraco-classes": "3.4.0",
    "jaraco-context": "6.1.2",
    "jaraco-functools": "4.5.0",
    "jeepney": "0.9.0",
    "keyring": "25.7.0",
    "markdown-it-py": "4.2.0",
    "mdurl": "0.1.2",
    "more-itertools": "11.1.0",
    "nh3": "0.3.6",
    "packaging": "26.2",
    "pip": "26.1.2",
    "pycparser": "3.0",
    "pygments": "2.20.0",
    "pyproject-hooks": "1.2.0",
    "readme-renderer": "45.0",
    "requests": "2.34.2",
    "requests-toolbelt": "1.0.0",
    "rfc3986": "2.0.0",
    "rich": "15.0.0",
    "secretstorage": "3.5.0",
    "setuptools": "83.0.0",
    "twine": "6.2.0",
    "urllib3": "2.7.0",
    "wheel": "0.47.0",
}


def _normalise_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _active_lines(path: Path) -> list[str]:
    return [
        line.rstrip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _locked_requirements() -> dict[str, tuple[str, set[str]]]:
    requirements: dict[str, tuple[str, set[str]]] = {}
    current_name: str | None = None

    for line in _active_lines(BUILD_LOCK):
        stripped = line.strip()
        if not line[0].isspace():
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s\\]+)\s*\\?", stripped)
            assert match is not None, f"non-exact or unsupported requirement line: {line!r}"
            current_name = _normalise_name(match.group(1))
            assert current_name not in requirements, f"duplicate locked project: {current_name}"
            requirements[current_name] = (match.group(2), set())
            continue

        assert current_name is not None, f"hash without a requirement: {line!r}"
        match = re.fullmatch(r"--hash=sha256:([0-9a-f]{64})\s*\\?", stripped)
        assert match is not None, f"unsupported locked requirement option: {line!r}"
        requirements[current_name][1].add(match.group(1))

    return requirements


def _workflow_job(path: Path, job_name: str) -> str:
    workflow = path.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n.*?(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert match is not None, f"missing workflow job {job_name!r}"
    return match.group(0)


def _assert_fresh_locked_environment(job: str) -> None:
    assert "PIP_CONFIG_FILE: /dev/null" in job
    assert "PIP_INDEX_URL: https://pypi.org/simple" in job
    assert 'PIP_EXTRA_INDEX_URL: ""' in job
    assert 'PIP_TRUSTED_HOST: ""' in job
    assert 'PIP_FIND_LINKS: ""' in job
    assert 'PIP_NO_INDEX: "0"' in job
    assert 'PIP_NO_INPUT: "1"' in job
    assert 'BUILD_VENV="$RUNNER_TEMP/' in job
    assert 'rm -rf "$BUILD_VENV"' in job
    assert '"$GPT2AGENT_REVIEWED_PYTHON_BASE/bin/python3.12" -m venv "$BUILD_VENV"' in job
    assert (
        '"$BUILD_VENV/bin/python" -m pip install --require-hashes '
        '--only-binary=:all: -r requirements-build.txt'
    ) in job
    assert '"$BUILD_VENV/bin/python" -m pip check' in job
    assert '"$BUILD_VENV/bin/python" -m pip --version' in job
    assert "pip install --upgrade" not in job


def _assert_reviewed_cpython(job: str) -> None:
    assert "scripts/install_account_gate_runtime.sh" in job
    assert "astral-sh/python-build-standalone/releases/download/20260623/" in job
    assert (
        "cpython-3.12.13%2B20260623-x86_64-unknown-linux-gnu-"
        "install_only_stripped.tar.gz" in job
    )
    assert '"$RUNTIME_BASE/bin/python3.12" -I -S -B -c' in job
    assert '("cpython", (3, 12, 13), "linux", "x86_64")' in job
    assert "GPT2AGENT_REVIEWED_PYTHON_BASE" in job
    assert "Clean reviewed CPython runtime" in job
    assert job.count("set +o posix") >= 2
    assert job.count("unset POSIXLY_CORRECT") >= 2


def test_build_system_and_direct_builder_inputs_use_exact_approved_pins() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["build-system"]["requires"] == [
        "setuptools==83.0.0",
        "wheel==0.47.0",
    ]
    assert _active_lines(BUILD_INPUT) == [
        f"{name}=={version}" for name, version in EXPECTED_BUILD_PINS.items()
    ]


def test_build_lock_has_only_exact_hashed_requirements_from_the_approved_inputs() -> None:
    locked = _locked_requirements()

    assert {name: version for name, (version, _) in locked.items()} == EXPECTED_LOCKED_PROJECTS
    for name, (version, hashes) in locked.items():
        assert version, f"{name} has no exact version"
        assert hashes, f"{name} has no SHA256 hashes"
    for name, version in EXPECTED_BUILD_PINS.items():
        assert locked[name][0] == version


def test_build_lock_rejects_indexes_direct_urls_vcs_and_editables() -> None:
    active = "\n".join(_active_lines(BUILD_LOCK))

    assert not re.search(
        r"(?mi)^\s*(?:--index-url|--extra-index-url|--trusted-host|"
        r"--find-links|-f\b|-e\b|--editable\b)",
        active,
    )
    assert "://" not in active
    assert " @ " not in active
    assert not re.search(r"(?i)\b(?:git|hg|svn|bzr)\+", active)


def test_build_lock_records_its_reproducible_generation_provenance() -> None:
    header = "\n".join(BUILD_LOCK.read_text(encoding="utf-8").splitlines()[:12])

    assert "CPython 3.12.13 on Linux x86_64" in header
    assert "official PyPI" in header
    assert "pip==26.1.2 with pip-tools==7.5.3" in header


def test_package_ci_uses_the_locked_builder_and_reproducible_build_settings() -> None:
    package = _workflow_job(CI_WORKFLOW, "package")

    assert "runs-on: ubuntu-24.04" in package
    assert 'python-version: "3.12.13"' not in package
    _assert_reviewed_cpython(package)
    _assert_fresh_locked_environment(package)
    assert 'SOURCE_DATE_EPOCH="$(git show -s --format=%ct "$GITHUB_SHA")"' in package
    assert "PYTHONHASHSEED=0" in package
    assert "TZ=UTC" in package
    assert '"$BUILD_VENV/bin/python" -m build --no-isolation' in package
    assert '"$BUILD_VENV/bin/python" -m twine check --strict dist/*' in package
    assert package.count("scripts/package_smoke.sh") == 1
    assert "overwrite: false" in package


def test_dependency_audit_checks_the_locked_builder_closure_without_resolving() -> None:
    audit = _workflow_job(CI_WORKFLOW, "dependency-audit")

    assert (
        "pip-audit -r requirements-build.txt --no-deps --disable-pip "
        "--progress-spinner off"
    ) in audit


def test_release_relay_validates_with_the_lock_without_rebuilding() -> None:
    build = _workflow_job(RELEASE_WORKFLOW, "build")
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "runs-on: ubuntu-24.04" in build
    assert 'python-version: "3.12.13"' not in build
    _assert_reviewed_cpython(build)
    _assert_fresh_locked_environment(build)
    assert (
        'run: |\n          "$BUILD_VENV/bin/python" -m twine check --strict dist/*'
        in build
    )
    assert " -m build" not in build
    assert "Test built artifacts in clean environments" not in build
    assert workflow.count("scripts/package_smoke.sh") == 0


def test_dependabot_groups_only_the_approved_build_lock_inputs() -> None:
    config = DEPENDABOT_CONFIG.read_text(encoding="utf-8")
    pip_update = re.search(
        r"(?ms)^  - package-ecosystem: pip\n.*?(?=^  - package-ecosystem:|\Z)",
        config,
    )

    assert pip_update is not None
    assert 'directory: "/"' in pip_update.group(0)
    assert "build-lock:" in pip_update.group(0)
    for package in EXPECTED_BUILD_PINS:
        assert f'- "{package}"' in pip_update.group(0)
