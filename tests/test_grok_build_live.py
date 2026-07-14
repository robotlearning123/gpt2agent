"""Opt-in live gate for the official Grok Build subscription lane."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from gpt2agent.errors import InputValidationError
from gpt2agent.grok_build import GrokBuildClient, GrokBuildConfig
from gpt2agent.grok_errors import GrokError


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RUN_LIVE = os.environ.get("RUN_GROK_BUILD_LIVE") == "1"
_LIVE_MODE = "plan"
_LIVE_PROMPT = "Reply with exactly GROK_BUILD_LIVE_OK. Do not inspect files or use tools."
_LIVE_SENTINEL = "GROK_BUILD_LIVE_OK"
_MAX_LIVE_MODELS = 256


@dataclass(frozen=True)
class _SelectedLiveEnvironment:
    home: Path
    auth_path: Path | None
    build_root: Path
    repository_root: Path
    mode: str


class LiveGateConfigurationError(RuntimeError):
    """Fixed-code configuration failure that never retains a private value."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _absolute_directory(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_dir() else None


def _absolute_file(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _configuration_failure(code: str) -> LiveGateConfigurationError:
    return LiveGateConfigurationError(code)


def _validate_live_environment(
    environment: Mapping[str, str],
    *,
    repository_root: Path,
    mode: str,
) -> _SelectedLiveEnvironment:
    if mode != "plan":
        raise _configuration_failure("GROK_BUILD_LIVE_MODE_FORBIDDEN")
    if "XAI_API_KEY" in environment or "GROK_CODE_XAI_API_KEY" in environment:
        raise _configuration_failure("GROK_BUILD_LIVE_API_KEY_FORBIDDEN")

    home = _absolute_directory(environment.get("GROK_HOME"))
    if home is None:
        raise _configuration_failure("GROK_BUILD_LIVE_HOME_REQUIRED")

    auth_path: Path | None = None
    if "GROK_AUTH_PATH" in environment:
        auth_path = _absolute_file(environment.get("GROK_AUTH_PATH"))
        if auth_path is None:
            raise _configuration_failure("GROK_BUILD_LIVE_AUTH_PATH_INVALID")

    build_root = _absolute_directory(environment.get("GROK_BUILD_ROOT"))
    if build_root is None:
        raise _configuration_failure("GROK_BUILD_LIVE_ROOT_REQUIRED")
    try:
        resolved_repository_root = repository_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        resolved_repository_root = None
    if (
        resolved_repository_root is None
        or not resolved_repository_root.is_dir()
        or (
            resolved_repository_root != build_root
            and build_root not in resolved_repository_root.parents
        )
    ):
        raise _configuration_failure("GROK_BUILD_LIVE_ROOT_MISMATCH")

    return _SelectedLiveEnvironment(
        home=home,
        auth_path=auth_path,
        build_root=build_root,
        repository_root=resolved_repository_root,
        mode=mode,
    )


def _require(condition: bool, code: str) -> None:
    if not condition:
        pytest.fail(code, pytrace=False)


def _valid_environment(
    tmp_path: Path,
) -> tuple[dict[str, str], Path, Path, Path]:
    home = tmp_path / "reviewed-profile"
    build_root = tmp_path / "build-root"
    repository_root = build_root / "repository"
    home.mkdir()
    repository_root.mkdir(parents=True)
    return (
        {
            "GROK_HOME": str(home.resolve()),
            "GROK_BUILD_ROOT": str(build_root.resolve()),
        },
        home.resolve(),
        build_root.resolve(),
        repository_root.resolve(),
    )


def _assert_configuration_error(
    environment: Mapping[str, str],
    repository_root: Path,
    code: str,
    *,
    mode: str = _LIVE_MODE,
) -> None:
    with pytest.raises(LiveGateConfigurationError) as caught:
        _validate_live_environment(
            environment,
            repository_root=repository_root,
            mode=mode,
        )

    assert caught.value.code == code
    assert str(caught.value) == code
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    for raw_value in environment.values():
        if raw_value:
            assert raw_value not in str(caught.value)


def test_live_requirement_uses_only_fixed_bounded_code() -> None:
    with pytest.raises(pytest.fail.Exception) as caught:
        _require(False, "GROK_BUILD_LIVE_FIXED_FAILURE")

    assert str(caught.value) == "GROK_BUILD_LIVE_FIXED_FAILURE"


def test_live_requirement_accepts_true_condition() -> None:
    _require(True, "GROK_BUILD_LIVE_UNUSED_FAILURE")


def test_live_environment_accepts_absolute_reviewed_paths(tmp_path: Path) -> None:
    environment, home, build_root, repository_root = _valid_environment(tmp_path)
    auth_path = home / "oauth.json"
    auth_path.touch(mode=0o600)
    environment["GROK_AUTH_PATH"] = str(auth_path.resolve())

    selected = _validate_live_environment(
        environment,
        repository_root=repository_root,
        mode="plan",
    )

    assert selected.home == home
    assert selected.auth_path == auth_path.resolve()
    assert selected.build_root == build_root
    assert selected.repository_root == repository_root
    assert selected.mode == "plan"


def test_live_environment_accepts_missing_optional_auth_path(tmp_path: Path) -> None:
    environment, home, build_root, repository_root = _valid_environment(tmp_path)

    selected = _validate_live_environment(
        environment,
        repository_root=repository_root,
        mode="plan",
    )

    assert selected.home == home
    assert selected.auth_path is None
    assert selected.build_root == build_root


@pytest.mark.parametrize("variable", ["GROK_HOME", "GROK_BUILD_ROOT"])
def test_live_environment_rejects_missing_required_paths(
    tmp_path: Path,
    variable: str,
) -> None:
    environment, _, _, repository_root = _valid_environment(tmp_path)
    environment.pop(variable)
    expected = {
        "GROK_HOME": "GROK_BUILD_LIVE_HOME_REQUIRED",
        "GROK_BUILD_ROOT": "GROK_BUILD_LIVE_ROOT_REQUIRED",
    }[variable]

    _assert_configuration_error(environment, repository_root, expected)


@pytest.mark.parametrize("variable", ["GROK_HOME", "GROK_BUILD_ROOT"])
def test_live_environment_rejects_relative_required_paths(
    tmp_path: Path,
    variable: str,
) -> None:
    environment, _, _, repository_root = _valid_environment(tmp_path)
    environment[variable] = "relative-planted-private-path"
    expected = {
        "GROK_HOME": "GROK_BUILD_LIVE_HOME_REQUIRED",
        "GROK_BUILD_ROOT": "GROK_BUILD_LIVE_ROOT_REQUIRED",
    }[variable]

    _assert_configuration_error(environment, repository_root, expected)


def test_live_environment_rejects_non_directory_home(tmp_path: Path) -> None:
    environment, _, _, repository_root = _valid_environment(tmp_path)
    not_directory = tmp_path / "not-a-profile-directory"
    not_directory.touch()
    environment["GROK_HOME"] = str(not_directory.resolve())

    _assert_configuration_error(
        environment,
        repository_root,
        "GROK_BUILD_LIVE_HOME_REQUIRED",
    )


def test_live_environment_rejects_non_directory_build_root(tmp_path: Path) -> None:
    environment, _, _, repository_root = _valid_environment(tmp_path)
    not_directory = tmp_path / "not-a-build-directory"
    not_directory.touch()
    environment["GROK_BUILD_ROOT"] = str(not_directory.resolve())

    _assert_configuration_error(
        environment,
        repository_root,
        "GROK_BUILD_LIVE_ROOT_REQUIRED",
    )


@pytest.mark.parametrize("value_kind", ["relative", "missing", "directory"])
def test_live_environment_rejects_invalid_optional_auth_path(
    tmp_path: Path,
    value_kind: str,
) -> None:
    environment, home, _, repository_root = _valid_environment(tmp_path)
    values = {
        "relative": "relative-planted-private-auth",
        "missing": str((home / "missing-auth.json").resolve()),
        "directory": str(home.resolve()),
    }
    environment["GROK_AUTH_PATH"] = values[value_kind]

    _assert_configuration_error(
        environment,
        repository_root,
        "GROK_BUILD_LIVE_AUTH_PATH_INVALID",
    )


@pytest.mark.parametrize("variable", ["XAI_API_KEY", "GROK_CODE_XAI_API_KEY"])
def test_live_environment_rejects_api_key_presence_even_when_empty(
    tmp_path: Path,
    variable: str,
) -> None:
    environment, _, _, repository_root = _valid_environment(tmp_path)
    environment[variable] = ""

    _assert_configuration_error(
        environment,
        repository_root,
        "GROK_BUILD_LIVE_API_KEY_FORBIDDEN",
    )


def test_live_environment_rejects_root_outside_current_repository(
    tmp_path: Path,
) -> None:
    environment, _, _, repository_root = _valid_environment(tmp_path)
    outside = tmp_path / "outside-root"
    outside.mkdir()
    environment["GROK_BUILD_ROOT"] = str(outside.resolve())

    _assert_configuration_error(
        environment,
        repository_root,
        "GROK_BUILD_LIVE_ROOT_MISMATCH",
    )


def test_live_environment_rejects_non_plan_mode(tmp_path: Path) -> None:
    environment, _, _, repository_root = _valid_environment(tmp_path)

    _assert_configuration_error(
        environment,
        repository_root,
        "GROK_BUILD_LIVE_MODE_FORBIDDEN",
        mode="apply",
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _RUN_LIVE,
    reason="set RUN_GROK_BUILD_LIVE=1 with explicit reviewed paths",
)
async def test_grok_build_live_account() -> None:
    selected = _validate_live_environment(
        os.environ,
        repository_root=_PROJECT_ROOT,
        mode=_LIVE_MODE,
    )
    config = GrokBuildConfig.from_mapping(
        {
            "command": "grok",
            "home": str(selected.home),
            "auth_path": (
                str(selected.auth_path) if selected.auth_path is not None else None
            ),
            "roots": [str(selected.build_root)],
            "timeout_seconds": 120,
            "max_output_bytes": 1_048_576,
            "default_max_turns": 2,
        }
    )
    client = GrokBuildClient(config)

    try:
        status = await client.status()
        catalog = await client.models()
        _require(status.get("installed") is True, "GROK_BUILD_LIVE_CLI_NOT_INSTALLED")
        _require(
            status.get("authenticated") is True,
            "GROK_BUILD_LIVE_AUTH_NOT_READY",
        )
        version = status.get("version")
        _require(
            isinstance(version, str) and 1 <= len(version) <= 64,
            "GROK_BUILD_LIVE_VERSION_INVALID",
        )
        models = catalog.get("models")
        _require(
            catalog.get("authenticated") is True
            and isinstance(models, list)
            and 1 <= len(models) <= _MAX_LIVE_MODELS,
            "GROK_BUILD_LIVE_CATALOG_INVALID",
        )
        default_model = catalog.get("default_model")
        _require(
            isinstance(default_model, str) and default_model in models,
            "GROK_BUILD_LIVE_DEFAULT_MODEL_INVALID",
        )
        _require(
            status.get("default_model") == default_model
            and status.get("models_count") == len(models),
            "GROK_BUILD_LIVE_PROBE_MISMATCH",
        )

        result = await client.agent(
            _LIVE_PROMPT,
            cwd=str(selected.repository_root),
            mode=_LIVE_MODE,
            max_turns=2,
            subagents=False,
        )
    except GrokError as exc:
        pytest.fail(f"GROK_BUILD_LIVE_CLIENT_ERROR:{exc.code}", pytrace=False)
    except InputValidationError:
        pytest.fail("GROK_BUILD_LIVE_CLIENT_ERROR:INPUT_VALIDATION", pytrace=False)

    _require(
        result.get("surface") == "build" and result.get("status") == "completed",
        "GROK_BUILD_LIVE_COMPLETION_INVALID",
    )
    text = result.get("text")
    _require(
        isinstance(text, str) and " ".join(text.split()) == _LIVE_SENTINEL,
        "GROK_BUILD_LIVE_SENTINEL_MISMATCH",
    )
    _require(result.get("model") in models, "GROK_BUILD_LIVE_MODEL_INVALID")
    session_id = result.get("session_id")
    _require(
        isinstance(session_id, str) and bool(session_id),
        "GROK_BUILD_LIVE_SESSION_ID_MISSING",
    )
    _require(
        result.get("changed_files") == [],
        "GROK_BUILD_LIVE_CHANGED_FILES_NONEMPTY",
    )
