from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from gpt2agent._bounded_process import BoundedProcessError, ProcessResult
from gpt2agent.errors import InputValidationError
from gpt2agent.grok_build import GrokBuildClient, GrokBuildConfig
from gpt2agent.grok_errors import GrokError


def _result(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> ProcessResult:
    return ProcessResult(returncode, stdout.encode(), stderr.encode())


def _models_output(
    *models: str,
    default: str = "grok-4.5",
    logged_in: bool = True,
) -> str:
    login = (
        "You are logged in with grok.com.\n\n"
        if logged_in
        else "You are not authenticated.\n\n"
    )
    entries = "".join(
        f"  {'*' if model == default else '-'} {model}"
        f"{' (default)' if model == default else ''}\n"
        for model in models
    )
    return f"{login}Default model: {default}\n\nAvailable models:\n{entries}"


class RecordingRunner:
    def __init__(self, results: Sequence[ProcessResult | BaseException]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProcessResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "env": dict(env),
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
            }
        )
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path.resolve()


def _config(root: Path | None = None, **overrides: Any) -> GrokBuildConfig:
    values: dict[str, Any] = {"roots": [str(root)] if root is not None else []}
    values.update(overrides)
    return GrokBuildConfig.from_mapping(values)


def _client(
    root: Path | None,
    runner: RecordingRunner,
    *,
    command: str = "grok",
    resolver: Any = None,
    **config_overrides: Any,
) -> GrokBuildClient:
    return GrokBuildClient(
        _config(root, command=command, **config_overrides),
        runner=runner,
        resolver=resolver or (lambda _: sys.executable),
    )


def _assert_grok_code(caught: pytest.ExceptionInfo[GrokError], code: str) -> None:
    assert caught.value.code == code
    assert caught.value.__dict__ == {
        "code": code,
        "method": None,
        "route": None,
        "status_code": None,
        "retryable": caught.value.retryable,
        "retry_after": None,
    }


def _assert_detached_closed_error(error: GrokError, code: str) -> None:
    assert error.code == code
    assert error.__cause__ is None
    assert error.__context__ is None


def _assert_detached_input_error(
    error: InputValidationError, invariant: str, planted: str
) -> None:
    assert error.invariant == invariant
    assert error.__cause__ is None
    assert error.__context__ is None
    assert planted not in str(error)


def test_build_config_defaults_and_resolves_paths(tmp_path: Path) -> None:
    default = GrokBuildConfig.from_mapping(None)
    assert default == GrokBuildConfig(
        command="grok",
        home=None,
        auth_path=None,
        roots=(),
        default_model=None,
        timeout_seconds=120.0,
        max_output_bytes=1_048_576,
        default_max_turns=20,
    )

    configured = GrokBuildConfig.from_mapping(
        {
            "command": str(tmp_path / "bin" / "grok"),
            "home": str(tmp_path / "profile"),
            "auth_path": str(tmp_path / "profile" / "oauth.json"),
            "roots": [str(tmp_path / "repo")],
            "default_model": "grok-4.5:fast",
            "timeout_seconds": "30",
            "max_output_bytes": "4096",
            "default_max_turns": "7",
        }
    )

    assert configured.home == (tmp_path / "profile").resolve()
    assert configured.auth_path == (tmp_path / "profile" / "oauth.json").resolve()
    assert configured.roots == ((tmp_path / "repo").resolve(),)
    assert configured.timeout_seconds == 30.0
    assert configured.max_output_bytes == 4096
    assert configured.default_max_turns == 7


@pytest.mark.parametrize(
    ("mapping", "invariant"),
    [
        ({"command": ""}, "grok_build.command must not be empty"),
        ({"roots": ("/repo",)}, "grok_build.roots must be a string list"),
        ({"roots": [""]}, "grok_build.roots must be a string list"),
        ({"roots": [7]}, "grok_build.roots must be a string list"),
        ({"default_model": "bad model"}, "grok_build.default_model is invalid"),
        ({"timeout_seconds": True}, "grok_build.timeout_seconds must be 1..600"),
        ({"timeout_seconds": float("nan")}, "grok_build.timeout_seconds must be 1..600"),
        ({"timeout_seconds": "inf"}, "grok_build.timeout_seconds must be 1..600"),
        ({"timeout_seconds": 10**10_000}, "grok_build.timeout_seconds must be 1..600"),
        ({"timeout_seconds": 0}, "grok_build.timeout_seconds must be 1..600"),
        ({"max_output_bytes": False}, "grok_build.max_output_bytes is out of range"),
        ({"max_output_bytes": 1024.5}, "grok_build.max_output_bytes is out of range"),
        ({"max_output_bytes": 1023}, "grok_build.max_output_bytes is out of range"),
        ({"default_max_turns": True}, "grok_build.default_max_turns must be 1..100"),
        ({"default_max_turns": 1.5}, "grok_build.default_max_turns must be 1..100"),
        ({"default_max_turns": 101}, "grok_build.default_max_turns must be 1..100"),
    ],
)
def test_build_config_rejects_invalid_values(
    mapping: dict[str, Any], invariant: str
) -> None:
    with pytest.raises(InputValidationError) as caught:
        GrokBuildConfig.from_mapping(mapping)

    assert caught.value.invariant == invariant


@pytest.mark.parametrize(
    ("key", "planted", "invariant"),
    [
        (
            "timeout_seconds",
            "xai-planted-float-secret",
            "grok_build.timeout_seconds must be 1..600",
        ),
        (
            "max_output_bytes",
            "xai-planted-output-secret",
            "grok_build.max_output_bytes is out of range",
        ),
        (
            "default_max_turns",
            "xai-planted-turns-secret",
            "grok_build.default_max_turns must be 1..100",
        ),
    ],
)
def test_build_config_conversion_errors_have_no_raw_exception_chain(
    key: str, planted: str, invariant: str
) -> None:
    with pytest.raises(InputValidationError) as caught:
        GrokBuildConfig.from_mapping({key: planted})

    _assert_detached_input_error(caught.value, invariant, planted)


@pytest.mark.asyncio
async def test_models_resolves_path_command_and_returns_current_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _make_executable(tmp_path / "bin" / "grok")
    profile = tmp_path / "profile"
    auth_path = profile / "oauth.json"
    runner = RecordingRunner(
        [_result(_models_output("grok-4.5", "grok-composer-2.5-fast"))]
    )
    monkeypatch.setenv("XAI_API_KEY", "xai-planted-secret")
    monkeypatch.setenv("GROK_CODE_XAI_API_KEY", "xai-planted-code-secret")
    client = _client(
        Path.cwd(),
        runner,
        resolver=lambda command: str(executable) if command == "grok" else None,
        home=str(profile),
        auth_path=str(auth_path),
        timeout_seconds=45,
        max_output_bytes=8192,
    )

    catalog = await client.models()

    assert catalog == {
        "authenticated": True,
        "default_model": "grok-4.5",
        "models": ["grok-4.5", "grok-composer-2.5-fast"],
        "count": 2,
    }
    call = runner.calls[0]
    assert call["argv"] == [str(executable), "--no-auto-update", "models"]
    assert call["cwd"] == Path.cwd().resolve()
    assert call["env"]["GROK_HOME"] == str(profile.resolve())
    assert call["env"]["GROK_AUTH_PATH"] == str(auth_path.resolve())
    assert "XAI_API_KEY" not in call["env"]
    assert "GROK_CODE_XAI_API_KEY" not in call["env"]
    assert os.environ["XAI_API_KEY"] == "xai-planted-secret"
    assert os.environ["GROK_CODE_XAI_API_KEY"] == "xai-planted-code-secret"
    assert call["timeout_seconds"] == 45.0
    assert call["max_output_bytes"] == 8192


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["models", "status"])
async def test_build_probes_reject_disabled_roots_without_subprocess_launch(
    operation: str,
) -> None:
    runner = RecordingRunner(
        [
            _result("grok 7.4.3 (build) [stable]\n"),
            _result(_models_output("grok-4.5")),
        ]
    )
    client = _client(None, runner)

    with pytest.raises(InputValidationError) as caught:
        await getattr(client, operation)()

    assert caught.value.invariant == (
        "grok_build.cwd must be an existing directory under a configured root"
    )
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["models", "status"])
async def test_build_probes_validate_roots_before_missing_binary_soft_failure(
    operation: str,
) -> None:
    runner = RecordingRunner([])
    client = _client(None, runner, resolver=lambda _: None)

    with pytest.raises(InputValidationError) as caught:
        await getattr(client, operation)()

    assert caught.value.invariant == (
        "grok_build.cwd must be an existing directory under a configured root"
    )
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["models", "status"])
async def test_build_probes_reject_ambient_cwd_outside_roots_without_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    allowed_root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed_root.mkdir()
    outside.mkdir()
    monkeypatch.chdir(outside)
    runner = RecordingRunner(
        [
            _result("grok 7.4.3 (build) [stable]\n"),
            _result(_models_output("grok-4.5")),
        ]
    )
    client = _client(allowed_root, runner)

    with pytest.raises(InputValidationError) as caught:
        await getattr(client, operation)()

    assert caught.value.invariant == (
        "grok_build.cwd must be an existing directory under a configured root"
    )
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["models", "status"])
async def test_build_probes_use_canonical_allowed_ambient_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = tmp_path / "root"
    ambient_cwd = root / "repo"
    ambient_cwd.mkdir(parents=True)
    monkeypatch.chdir(ambient_cwd)
    results = [_result(_models_output("grok-4.5"))]
    if operation == "status":
        results.insert(0, _result("grok 7.4.3 (build) [stable]\n"))
    runner = RecordingRunner(results)
    client = _client(root, runner)

    result = await getattr(client, operation)()

    assert result["authenticated"] is True
    assert result["default_model"] == "grok-4.5"
    assert len(runner.calls) == (2 if operation == "status" else 1)
    assert all(call["cwd"] == ambient_cwd.resolve() for call in runner.calls)


@pytest.mark.asyncio
async def test_models_accepts_explicit_executable_without_path_resolver(
    tmp_path: Path,
) -> None:
    executable = _make_executable(tmp_path / "grok explicit")
    runner = RecordingRunner([_result(_models_output("grok-4.5"))])

    def unexpected_resolver(_: str) -> str | None:
        pytest.fail("explicit executable paths must not use PATH resolution")

    result = await _client(
        Path.cwd(),
        runner,
        command=str(executable),
        resolver=unexpected_resolver,
    ).models()

    assert result["models"] == ["grok-4.5"]
    assert runner.calls[0]["argv"][0] == str(executable)


@pytest.mark.asyncio
async def test_bare_resolver_result_is_pinned_before_repository_cwd_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discovery_cwd = tmp_path / "discovery"
    root = tmp_path / "root"
    repository = root / "repo"
    discovery_cwd.mkdir()
    repository.mkdir(parents=True)
    trusted = _make_executable(discovery_cwd / "grok")
    substituted = _make_executable(repository / "grok")
    runner = RecordingRunner([_result(_models_output("grok-4.5"))])
    resolver_calls: list[str] = []

    def resolver(command: str) -> str:
        resolver_calls.append(command)
        return "grok"

    monkeypatch.chdir(discovery_cwd)
    client = _client(root, runner, resolver=resolver)
    monkeypatch.chdir(repository)

    result = await client.models()

    assert result["models"] == ["grok-4.5"]
    assert resolver_calls == ["grok"]
    assert runner.calls[0]["argv"][0] == str(trusted)
    assert runner.calls[0]["argv"][0] != str(substituted)


@pytest.mark.asyncio
async def test_relative_explicit_command_is_pinned_before_repository_cwd_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discovery_cwd = tmp_path / "discovery"
    root = tmp_path / "root"
    repository = root / "repo"
    discovery_cwd.mkdir()
    repository.mkdir(parents=True)
    trusted = _make_executable(discovery_cwd / "bin" / "grok")
    substituted = _make_executable(repository / "bin" / "grok")
    runner = RecordingRunner([_result(_models_output("grok-4.5"))])

    def unexpected_resolver(_: str) -> str | None:
        pytest.fail("explicit executable paths must not use PATH resolution")

    monkeypatch.chdir(discovery_cwd)
    client = _client(
        root,
        runner,
        command="bin/grok",
        resolver=unexpected_resolver,
    )
    monkeypatch.chdir(repository)

    result = await client.models()

    assert result["models"] == ["grok-4.5"]
    assert runner.calls[0]["argv"][0] == str(trusted)
    assert runner.calls[0]["argv"][0] != str(substituted)


@pytest.mark.asyncio
async def test_discovered_executable_disappearance_does_not_rediscover_in_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discovery_cwd = tmp_path / "discovery"
    root = tmp_path / "root"
    repository = root / "repo"
    discovery_cwd.mkdir()
    repository.mkdir(parents=True)
    trusted = _make_executable(discovery_cwd / "grok")
    _make_executable(repository / "grok")
    runner = RecordingRunner([_result(_models_output("grok-4.5"))])
    resolver_calls: list[Path] = []

    def resolver(_: str) -> str:
        candidate = Path.cwd() / "grok"
        resolver_calls.append(candidate)
        return str(candidate)

    monkeypatch.chdir(discovery_cwd)
    client = _client(root, runner, resolver=resolver)
    trusted.unlink()
    monkeypatch.chdir(repository)

    with pytest.raises(GrokError) as caught:
        await client.models()

    _assert_grok_code(caught, "GROK_BUILD_CLI_NOT_FOUND")
    assert resolver_calls == [discovery_cwd / "grok"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_models_reports_unauthenticated_without_returning_cli_prose() -> None:
    runner = RecordingRunner(
        [
            _result(
                "You are not authenticated.\n\n"
                "Default model: grok-4.5\n\nAvailable models:\n"
            )
        ]
    )

    result = await _client(Path.cwd(), runner).models()

    assert result == {
        "authenticated": False,
        "default_model": "grok-4.5",
        "models": [],
        "count": 0,
    }
    assert "identity" not in result


@pytest.mark.asyncio
async def test_models_accepts_unbulleted_catalog_without_version_pinning() -> None:
    runner = RecordingRunner(
        [
            _result(
                "Default model: grok-build\n\n"
                "Available models:\n"
                "  grok-build  Grok Build\n"
                "  grok-4.5: Grok 4.5\n"
                "  composer/next  Composer Next\n"
            )
        ]
    )

    result = await _client(Path.cwd(), runner).models()

    assert result == {
        "authenticated": True,
        "default_model": "grok-build",
        "models": ["grok-build", "grok-4.5", "composer/next"],
        "count": 3,
    }


@pytest.mark.asyncio
async def test_models_ignores_unallowlisted_footer_and_identity() -> None:
    runner = RecordingRunner(
        [
            _result(
                _models_output("grok-4.5", "grok-composer-2.5-fast")
                + "Account: private.member@example.test\n"
                + "  after-footer-must-not-parse\n"
            )
        ]
    )

    result = await _client(Path.cwd(), runner).models()

    rendered = json.dumps(result)
    assert result["models"] == ["grok-4.5", "grok-composer-2.5-fast"]
    assert "private.member@example.test" not in rendered
    assert "after-footer" not in rendered


@pytest.mark.asyncio
async def test_status_parses_drifted_version_without_identity_or_paths(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(
        [
            _result("grok 9.17.3-next.4 (drifted-build) [stable]\n"),
            _result(_models_output("grok-4.5", "grok-composer-2.5-fast")),
        ]
    )

    status = await _client(
        Path.cwd(),
        runner,
        home=str(tmp_path / "private-profile"),
        auth_path=str(tmp_path / "private-profile" / "oauth.json"),
    ).status()

    assert status == {
        "installed": True,
        "version": "9.17.3-next.4",
        "authenticated": True,
        "default_model": "grok-4.5",
        "models_count": 2,
    }
    assert runner.calls[0]["argv"][-1] == "--version"
    assert runner.calls[1]["argv"][-1] == "models"
    assert "profile" not in status
    assert "identity" not in status


@pytest.mark.asyncio
async def test_status_is_the_only_missing_binary_soft_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.chdir(root)
    runner = RecordingRunner([])
    status_client = _client(root, runner, resolver=lambda _: None)

    assert await status_client.status() == {
        "installed": False,
        "version": None,
        "authenticated": False,
        "default_model": None,
        "models_count": 0,
    }
    action_client = _client(root, runner, resolver=lambda _: None)
    with pytest.raises(GrokError) as caught:
        await action_client.models()

    _assert_grok_code(caught, "GROK_BUILD_CLI_NOT_FOUND")
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (_result(stderr="You are not authenticated.", returncode=1), "GROK_BUILD_AUTH_MISSING"),
        (_result(stderr="Usage quota exceeded.", returncode=1), "GROK_BUILD_QUOTA"),
        (_result(stderr="private.member@example.test xai-secret", returncode=2), "GROK_BUILD_FAILED"),
        (BoundedProcessError("timeout"), "GROK_BUILD_TIMEOUT"),
        (BoundedProcessError("output_too_large"), "GROK_BUILD_OUTPUT_TOO_LARGE"),
    ],
)
async def test_models_maps_process_failures_to_closed_errors(
    failure: ProcessResult | BaseException, code: str
) -> None:
    runner = RecordingRunner([failure])

    with pytest.raises(GrokError) as caught:
        await _client(Path.cwd(), runner).models()

    _assert_grok_code(caught, code)
    rendered = str(caught.value)
    assert "private.member@example.test" not in rendered
    assert "xai-secret" not in rendered


@pytest.mark.asyncio
async def test_models_maps_malformed_utf8_and_contract_to_failed() -> None:
    for result in (
        ProcessResult(0, b"\xff", b""),
        _result("model service ready\n"),
        _result(_models_output("grok-4.5", default="other-model")),
    ):
        with pytest.raises(GrokError) as caught:
            await _client(Path.cwd(), RecordingRunner([result])).models()

        _assert_grok_code(caught, "GROK_BUILD_FAILED")


@pytest.mark.asyncio
async def test_malformed_utf8_grok_error_has_no_raw_exception_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    cwd = root / "repo"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    planted = b"xai-planted-raw-decode-secret"
    runner = RecordingRunner([ProcessResult(0, b"\xff" + planted, b"")])

    with pytest.raises(GrokError) as caught:
        await _client(root, runner).models()

    _assert_detached_closed_error(caught.value, "GROK_BUILD_FAILED")
    assert planted.decode() not in str(caught.value)


@pytest.mark.asyncio
async def test_missing_executable_race_grok_error_has_no_os_exception_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    cwd = root / "repo"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    planted = "xai-planted-file-not-found-secret"
    runner = RecordingRunner([FileNotFoundError(planted)])

    with pytest.raises(GrokError) as caught:
        await _client(root, runner).models()

    _assert_detached_closed_error(caught.value, "GROK_BUILD_CLI_NOT_FOUND")
    assert planted not in str(caught.value)


@pytest.mark.asyncio
async def test_agent_plan_uses_exact_argv_and_projects_only_allowlisted_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    cwd = root / "repo"
    cwd.mkdir(parents=True)
    prompt = "quote ' $(touch /tmp/never-run) ; literal\nsecond line"
    payload = {
        "text": "Completed the planted task.",
        "stopReason": "end_turn",
        "sessionId": "123e4567-e89b-12d3-a456-426614174000",
        "usage": {
            "inputTokens": 12,
            "outputTokens": 5,
            "totalTokens": 17,
            "cacheReadTokens": 3,
            "cacheCreationTokens": 1,
            "costUsd": 999,
            "negative": -1,
            "boolTokens": True,
        },
        "changedFiles": [
            "src/main.py",
            "new/module.py",
            "src/main.py",
            "../outside.txt",
            str(tmp_path / "absolute-secret.txt"),
            42,
        ],
        "requestId": "req-planted-secret",
        "identity": "private.member@example.test",
        "metadata": {"access_token": "xai-planted-secret"},
    }
    runner = RecordingRunner(
        [
            _result(_models_output("grok-4.5", "grok-composer-2.5-fast")),
            _result(json.dumps(payload), stderr="stderr-planted-secret"),
        ]
    )
    monkeypatch.setenv("XAI_API_KEY", "xai-parent-secret")
    monkeypatch.setenv("GROK_CODE_XAI_API_KEY", "xai-parent-code-secret")

    result = await _client(root, runner).agent(prompt, cwd=str(cwd))

    assert result == {
        "surface": "build",
        "status": "completed",
        "session_id": "123e4567-e89b-12d3-a456-426614174000",
        "model": "grok-4.5",
        "text": "Completed the planted task.",
        "stop_reason": "end_turn",
        "usage": {
            "inputTokens": 12,
            "outputTokens": 5,
            "totalTokens": 17,
            "cacheReadTokens": 3,
            "cacheCreationTokens": 1,
        },
        "changed_files": ["src/main.py", "new/module.py"],
    }
    argv = runner.calls[1]["argv"]
    assert argv[:4] == [
        str(Path(sys.executable).resolve()),
        "--no-auto-update",
        "--cwd",
        str(cwd.resolve()),
    ]
    assert argv[argv.index("-p") + 1] == prompt
    assert argv.count(prompt) == 1
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--max-turns") + 1] == "20"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--model") + 1] == "grok-4.5"
    assert "--no-memory" in argv
    assert "--no-subagents" in argv
    assert runner.calls[1]["cwd"] == cwd.resolve()
    assert runner.calls[0]["cwd"] == cwd.resolve()
    assert all(call["cwd"] == cwd.resolve() for call in runner.calls)
    assert "XAI_API_KEY" not in runner.calls[1]["env"]
    assert "GROK_CODE_XAI_API_KEY" not in runner.calls[1]["env"]
    rendered = json.dumps(result)
    for planted in (
        prompt,
        "stderr-planted-secret",
        "req-planted-secret",
        "private.member@example.test",
        "xai-planted-secret",
        str(tmp_path / "absolute-secret.txt"),
    ):
        assert planted not in rendered


@pytest.mark.asyncio
async def test_agent_apply_and_subagent_flags_are_exact(tmp_path: Path) -> None:
    root = tmp_path / "root"
    cwd = root / "repo"
    cwd.mkdir(parents=True)
    complete = _result('{"text":"done"}')
    runner = RecordingRunner(
        [
            _result(_models_output("grok-4.5")),
            complete,
            _result(_models_output("grok-4.5")),
            complete,
        ]
    )
    client = _client(root, runner)

    apply_result = await client.agent("apply", cwd=str(cwd), mode="apply")
    subagent_result = await client.agent("delegate", cwd=str(cwd), subagents=True)

    apply_argv = runner.calls[1]["argv"]
    subagent_argv = runner.calls[3]["argv"]
    assert apply_result["status"] == "completed"
    assert subagent_result["status"] == "completed"
    assert apply_argv[apply_argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert apply_argv[apply_argv.index("--sandbox") + 1] == "strict"
    assert "--no-subagents" in apply_argv
    assert subagent_argv[subagent_argv.index("--permission-mode") + 1] == "plan"
    assert subagent_argv[subagent_argv.index("--sandbox") + 1] == "read-only"
    assert "--no-subagents" not in subagent_argv


@pytest.mark.asyncio
async def test_agent_none_cwd_uses_contained_process_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    cwd = root / "repo"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    runner = RecordingRunner(
        [_result(_models_output("grok-4.5")), _result('{"text":"done"}')]
    )

    result = await _client(root, runner).agent("plan")

    assert result["text"] == "done"
    assert runner.calls[1]["cwd"] == cwd.resolve()
    assert runner.calls[1]["argv"][3] == str(cwd.resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    ["", "   ", "a" * 65_537, "\ud800"],
)
async def test_agent_rejects_invalid_prompt_without_process(
    tmp_path: Path, prompt: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    runner = RecordingRunner([])

    with pytest.raises(InputValidationError) as caught:
        await _client(root, runner).agent(prompt, cwd=str(root))

    assert caught.value.invariant == (
        "grok_build.prompt must be 1..65536 UTF-8 bytes"
    )
    assert runner.calls == []


@pytest.mark.asyncio
async def test_agent_prompt_encoding_error_has_no_raw_exception_chain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    runner = RecordingRunner([])
    planted = "xai-planted-prompt-secret"
    prompt = f"{planted}\ud800"

    with pytest.raises(InputValidationError) as caught:
        await _client(root, runner).agent(prompt, cwd=str(root))

    _assert_detached_input_error(
        caught.value,
        "grok_build.prompt must be 1..65536 UTF-8 bytes",
        planted,
    )
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["\x00", "safe\x00unsafe"])
async def test_agent_rejects_embedded_nul_before_catalog_or_session(
    tmp_path: Path, prompt: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    runner = RecordingRunner(
        [_result(_models_output("grok-4.5")), _result('{"text":"done"}')]
    )

    with pytest.raises(InputValidationError) as caught:
        await _client(root, runner).agent(prompt, cwd=str(root))

    assert caught.value.invariant == (
        "grok_build.prompt must be 1..65536 UTF-8 bytes"
    )
    assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "invariant"),
    [
        ({"mode": "workspace"}, "grok_build.mode must be plan or apply"),
        ({"max_turns": True}, "grok_build.max_turns must be 1..100"),
        ({"max_turns": 0}, "grok_build.max_turns must be 1..100"),
        ({"max_turns": 101}, "grok_build.max_turns must be 1..100"),
        ({"subagents": 1}, "grok_build.subagents must be boolean"),
    ],
)
async def test_agent_rejects_invalid_controls_without_process(
    tmp_path: Path, kwargs: dict[str, Any], invariant: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    runner = RecordingRunner([])

    with pytest.raises(InputValidationError) as caught:
        await _client(root, runner).agent("plan", cwd=str(root), **kwargs)

    assert caught.value.invariant == invariant
    assert runner.calls == []


@pytest.mark.asyncio
async def test_agent_accepts_prompt_at_utf8_limit_and_explicit_catalog_model(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    prompt = "é" * 32_768
    runner = RecordingRunner(
        [
            _result(_models_output("grok-4.5", "grok-composer-2.5-fast")),
            _result('{"text":"done"}'),
        ]
    )

    result = await _client(root, runner).agent(
        prompt,
        cwd=str(root),
        model="grok-composer-2.5-fast",
        max_turns=100,
    )

    assert result["model"] == "grok-composer-2.5-fast"
    argv = runner.calls[1]["argv"]
    assert argv[argv.index("-p") + 1] == prompt
    assert argv[argv.index("--max-turns") + 1] == "100"


@pytest.mark.asyncio
async def test_agent_rejects_unauthenticated_or_unlisted_model_before_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    unauth_runner = RecordingRunner([_result("You are not authenticated.\n")])
    with pytest.raises(GrokError) as auth_error:
        await _client(root, unauth_runner).agent("plan", cwd=str(root))
    _assert_grok_code(auth_error, "GROK_BUILD_AUTH_MISSING")

    model_runner = RecordingRunner([_result(_models_output("grok-4.5"))])
    with pytest.raises(InputValidationError, match="grok_build.model"):
        await _client(root, model_runner).agent(
            "plan", cwd=str(root), model="grok-unlisted"
        )

    assert len(unauth_runner.calls) == 1
    assert len(model_runner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("not-json", "GROK_BUILD_FAILED"),
        ("[]", "GROK_BUILD_FAILED"),
        ('{"text":""}', "GROK_BUILD_FAILED"),
        ('{"type":"error","message":"quota exceeded"}', "GROK_BUILD_QUOTA"),
        ('{"type":"error","message":"not authenticated"}', "GROK_BUILD_AUTH_MISSING"),
        ('{"type":"error","message":"private.member@example.test"}', "GROK_BUILD_FAILED"),
    ],
)
async def test_agent_maps_malformed_and_error_payloads_to_closed_errors(
    tmp_path: Path, payload: str, code: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    runner = RecordingRunner(
        [_result(_models_output("grok-4.5")), _result(payload)]
    )

    with pytest.raises(GrokError) as caught:
        await _client(root, runner).agent("plan", cwd=str(root))

    _assert_grok_code(caught, code)
    assert "private.member@example.test" not in str(caught.value)


@pytest.mark.asyncio
async def test_malformed_json_grok_error_has_no_raw_exception_chain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    cwd = root / "repo"
    cwd.mkdir(parents=True)
    planted = "xai-planted-raw-json-secret"
    runner = RecordingRunner(
        [
            _result(_models_output("grok-4.5")),
            _result(f'{{"text":"unterminated {planted}'),
        ]
    )

    with pytest.raises(GrokError) as caught:
        await _client(root, runner).agent("plan", cwd=str(cwd))

    _assert_detached_closed_error(caught.value, "GROK_BUILD_FAILED")
    assert planted not in str(caught.value)


@pytest.mark.asyncio
async def test_agent_drops_malformed_optional_fields_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    too_large = 2**63
    payload = {
        "text": "done",
        "sessionId": "x" * 129,
        "stopReason": {"private": "value"},
        "usage": {
            "inputTokens": True,
            "outputTokens": -1,
            "totalTokens": too_large,
            "cacheReadTokens": 4,
            "cacheCreationTokens": "5",
            "arbitrary": 9,
        },
        "changedFiles": [
            "safe/file.py",
            "nested/../escape.py",
            "/absolute/file.py",
            "C:drive-relative.py",
            "line\nbreak.py",
            "x" * 1025,
            None,
        ],
    }
    runner = RecordingRunner(
        [
            _result(_models_output("grok-4.5")),
            _result(json.dumps(payload)),
        ]
    )

    result = await _client(root, runner).agent("plan", cwd=str(root))

    assert result["session_id"] is None
    assert result["stop_reason"] is None
    assert result["usage"] == {"cacheReadTokens": 4}
    assert result["changed_files"] == ["safe/file.py"]


@pytest.mark.asyncio
async def test_agent_returns_empty_optional_structures_when_sources_are_not_mappings(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    runner = RecordingRunner(
        [
            _result(_models_output("grok-4.5")),
            _result('{"text":"done","usage":[],"changedFiles":"src/main.py"}'),
        ]
    )

    result = await _client(root, runner).agent("plan", cwd=str(root))

    assert result["usage"] is None
    assert result["changed_files"] == []


@pytest.mark.asyncio
async def test_agent_maps_executable_disappearance_race_to_cli_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.chdir(root)
    executable = _make_executable(tmp_path / "bin" / "grok")

    class DisappearingExecutableRunner(RecordingRunner):
        async def __call__(self, *args: Any, **kwargs: Any) -> ProcessResult:
            executable.unlink(missing_ok=True)
            return await super().__call__(*args, **kwargs)

    runner = DisappearingExecutableRunner([FileNotFoundError("planted")])

    with pytest.raises(GrokError) as caught:
        await _client(
            root,
            runner,
            resolver=lambda _: str(executable),
        ).models()

    _assert_grok_code(caught, "GROK_BUILD_CLI_NOT_FOUND")


@pytest.mark.asyncio
async def test_agent_maps_cwd_disappearance_race_to_input_validation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    cwd = root / "repo"
    cwd.mkdir(parents=True)

    class DisappearingCwdRunner(RecordingRunner):
        async def __call__(
            self, argv: Sequence[str], **kwargs: Any
        ) -> ProcessResult:
            if len(self.calls) == 1:
                shutil.rmtree(cwd)
            return await super().__call__(argv, **kwargs)

    runner = DisappearingCwdRunner(
        [_result(_models_output("grok-4.5")), FileNotFoundError("planted")]
    )

    with pytest.raises(InputValidationError, match="grok_build.cwd"):
        await _client(root, runner).agent("plan", cwd=str(cwd))

    assert len(runner.calls) == 2
