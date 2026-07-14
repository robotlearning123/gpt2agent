from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from gpt2agent._bounded_process import ProcessResult
from gpt2agent._secure_file import write_private_json
from gpt2agent.grok_errors import GrokError
from gpt2agent.grok_setup import (
    import_browser_web_auth,
    run_grok_setup,
    setup_web_auth,
)
from gpt2agent.grok_web_auth import GrokWebAuthStore


def _process(payload: Any = None, *, returncode: int = 0) -> ProcessResult:
    stdout = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return ProcessResult(returncode, stdout, b"")


def _response(data: dict[str, Any]) -> dict[str, Any]:
    return {"id": "planted-request", "success": True, "data": data}


def _browser_cookie(
    name: str,
    value: str,
    *,
    expires: int,
) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": ".grok.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
        "expires": expires,
    }


class RecordingRunner:
    def __init__(self, results: Sequence[ProcessResult | BaseException]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []
        self.options: list[dict[str, Any]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProcessResult:
        self.calls.append(list(argv))
        self.options.append(
            {
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


def _successful_results(
    *,
    now: int,
    user_agent: str = "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36",
) -> list[ProcessResult]:
    cookies = [
        _browser_cookie("sso", "planted-sso-secret", expires=now + 3_600),
        _browser_cookie("sso-rw", "planted-rw-secret", expires=now + 3_600),
        _browser_cookie(
            "cf_clearance", "planted-clearance-secret", expires=now + 3_600
        ),
        _browser_cookie("unrelated", "planted-unrelated", expires=now + 3_600),
    ]
    return [
        _process(_response({"url": "https://grok.com/"})),
        _process(_response({"cookies": cookies})),
        _process(_response({"result": user_agent})),
        _process(_response({"shutdown": True})),
    ]


@pytest.mark.asyncio
async def test_browser_import_uses_one_owned_session_and_exact_argv_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    runner = RecordingRunner(_successful_results(now=now))
    auth_path = tmp_path / "private" / "grok-web-auth.json"
    owned_session = "gpt2agent-grok-planted-session"
    monkeypatch.setenv("XAI_API_KEY", "xai-planted-cross-lane-secret")
    monkeypatch.setenv("GROK_CODE_XAI_API_KEY", "xai-planted-build-secret")

    status = await import_browser_web_auth(
        auth_path,
        chrome_profile="Default",
        runner=runner,
        resolver=lambda name: name if name == "browser-use" else None,
        session_factory=lambda: owned_session,
    )

    assert runner.calls[0] == [
        "browser-use",
        "--headed",
        "--profile",
        "Default",
        "--session",
        owned_session,
        "--json",
        "open",
        "https://grok.com/",
    ]
    assert runner.calls[1] == [
        "browser-use",
        "--profile",
        "Default",
        "--session",
        owned_session,
        "--json",
        "cookies",
        "get",
        "--url",
        "https://grok.com/",
    ]
    assert runner.calls[2] == [
        "browser-use",
        "--profile",
        "Default",
        "--session",
        owned_session,
        "--json",
        "eval",
        "navigator.userAgent",
    ]
    assert runner.calls[-1] == [
        "browser-use",
        "--profile",
        "Default",
        "--session",
        owned_session,
        "close",
    ]
    assert all(call.count(owned_session) == 1 for call in runner.calls)
    assert not any(call[-2:] == ["close", "--all"] for call in runner.calls)
    assert not any("export" in call for call in runner.calls)
    assert all(options["max_output_bytes"] == 131_072 for options in runner.options)
    assert all("XAI_API_KEY" not in options["env"] for options in runner.options)
    assert all(
        "GROK_CODE_XAI_API_KEY" not in options["env"] for options in runner.options
    )
    assert status["cookie_names"] == ["cf_clearance", "sso", "sso-rw"]

    stored = json.loads(auth_path.read_text(encoding="utf-8"))
    assert stored["browser_impersonation"] == "chrome131"
    assert "userAgent" not in repr(stored)
    assert "Mozilla" not in repr(stored)
    assert {cookie["name"] for cookie in stored["cookies"]} == {
        "sso",
        "sso-rw",
        "cf_clearance",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_agent",
    [
        "Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 Chromium/131.0.0.0 Safari/537.36",
        "planted-unparseable-agent",
    ],
)
async def test_browser_import_discards_clearance_without_exact_supported_major(
    tmp_path: Path,
    user_agent: str,
) -> None:
    now = int(time.time())
    runner = RecordingRunner(_successful_results(now=now, user_agent=user_agent))
    auth_path = tmp_path / "private" / f"{len(user_agent)}-auth.json"

    status = await import_browser_web_auth(
        auth_path,
        runner=runner,
        resolver=lambda _: "browser-use",
        session_factory=lambda: "gpt2agent-grok-unsupported-ua",
    )

    assert "cf_clearance" not in status["cookie_names"]
    stored = json.loads(auth_path.read_text(encoding="utf-8"))
    assert "cf_clearance" not in {cookie["name"] for cookie in stored["cookies"]}
    assert user_agent not in repr(stored)


@pytest.mark.asyncio
async def test_browser_import_drops_invalid_optional_cookie_metadata(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    cookies = [
        _browser_cookie("sso", "planted-sso-secret", expires=now + 3_600),
        _browser_cookie("sso-rw", "planted-rw-secret", expires=now + 3_600),
        _browser_cookie("grok_device_id", "planted-device", expires=now + 3_600)
        | {"secure": False},
    ]
    runner = RecordingRunner(
        [
            _process(_response({"url": "https://grok.com/"})),
            _process(_response({"cookies": cookies})),
            _process(
                _response(
                    {
                        "result": (
                            "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36"
                        )
                    }
                )
            ),
            _process(_response({"shutdown": True})),
        ]
    )

    status = await import_browser_web_auth(
        tmp_path / "private" / "auth.json",
        runner=runner,
        resolver=lambda _: "browser-use",
        session_factory=lambda: "gpt2agent-grok-drop-optional",
    )

    assert status["cookie_names"] == ["sso", "sso-rw"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["open", "cookies", "write"])
async def test_browser_import_closes_only_owned_session_on_every_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    now = int(time.time())
    owned_session = f"gpt2agent-grok-{failure_point}"
    if failure_point == "open":
        results: list[ProcessResult | BaseException] = [
            RuntimeError("planted-open-secret"),
            _process(_response({"shutdown": True})),
        ]
    elif failure_point == "cookies":
        results = [
            _process(_response({"url": "https://grok.com/"})),
            _process({"success": True, "data": {"cookies": "not-a-list"}}),
            _process(_response({"shutdown": True})),
        ]
    else:
        results = _successful_results(now=now)
        from gpt2agent import grok_setup

        def fail_write(path: Path, value: Any) -> None:
            raise RuntimeError("planted-write-secret")

        monkeypatch.setattr(grok_setup, "write_private_json", fail_write)
    runner = RecordingRunner(results)

    with pytest.raises(GrokError) as caught:
        await import_browser_web_auth(
            tmp_path / "private" / "auth.json",
            runner=runner,
            resolver=lambda _: "browser-use",
            session_factory=lambda: owned_session,
        )

    assert caught.value.code == "GROK_WEB_FAILED"
    assert "planted" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert runner.calls[-1] == [
        "browser-use",
        "--profile",
        "Default",
        "--session",
        owned_session,
        "close",
    ]
    assert not any("--all" in call or "export" in call for call in runner.calls)


@pytest.mark.asyncio
async def test_browser_import_reports_owned_session_close_failure(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    results: list[ProcessResult | BaseException] = _successful_results(now=now)[:-1]
    results.append(RuntimeError("planted-close-secret"))
    runner = RecordingRunner(results)

    with pytest.raises(GrokError) as caught:
        await import_browser_web_auth(
            tmp_path / "private" / "auth.json",
            runner=runner,
            resolver=lambda _: "browser-use",
            session_factory=lambda: "gpt2agent-grok-close-failure",
        )

    assert caught.value.code == "GROK_WEB_FAILED"
    assert "planted-close-secret" not in str(caught.value)
    assert runner.calls[-1][-1] == "close"


@pytest.mark.asyncio
async def test_manual_fallback_uses_hidden_injected_input_and_prints_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts: list[str] = []
    answers = iter(["planted-manual-sso", "planted-manual-rw"])

    def hidden_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    status = await setup_web_auth(
        tmp_path / "private" / "auth.json",
        manual=True,
        resolver=lambda _: pytest.fail("manual setup must not discover a browser"),
        getpass_fn=hidden_input,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert len(prompts) == 2
    assert all("planted" not in prompt for prompt in prompts)
    assert status["cookie_names"] == ["sso", "sso-rw"]
    assert "planted" not in repr(status)


@pytest.mark.asyncio
async def test_missing_browser_automatically_uses_hidden_manual_fallback(
    tmp_path: Path,
) -> None:
    answers = iter(["planted-auto-sso", "planted-auto-rw"])
    status = await setup_web_auth(
        tmp_path / "private" / "auth.json",
        resolver=lambda _: None,
        getpass_fn=lambda _: next(answers),
    )

    assert status["authenticated"] is True
    assert status["cookie_names"] == ["sso", "sso-rw"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_name", ["sso", "sso-rw"])
@pytest.mark.parametrize(
    ("case", "invalid_value"),
    [
        ("blank", ""),
        ("blank-whitespace", "   "),
        ("control", "planted-control\nsecret"),
        ("oversized", "planted-" + "x" * (16_385 - len("planted-"))),
    ],
    ids=("blank", "blank-whitespace", "control", "oversized"),
)
async def test_invalid_manual_cookie_preserves_existing_private_auth(
    tmp_path: Path,
    invalid_name: str,
    case: str,
    invalid_value: str,
) -> None:
    now = int(time.time())
    path = tmp_path / "private" / "grok-web-auth.json"
    existing = {
        "schema_version": 1,
        "source": "manual",
        "browser_impersonation": "chrome131",
        "cookies": [
            {
                "name": name,
                "domain": ".grok.com",
                "path": "/",
                "expires": now + 3_600,
                "secure": True,
                "http_only": True,
                "value": f"planted-existing-{name}-secret",
            }
            for name in ("sso", "sso-rw")
        ],
    }
    write_private_json(path, existing)
    original_bytes = path.read_bytes()
    original_stat = path.stat()
    original_fingerprint = (
        original_stat.st_dev,
        original_stat.st_ino,
        original_stat.st_size,
        original_stat.st_mtime_ns,
    )
    answers = {
        "sso": invalid_value
        if invalid_name == "sso"
        else "planted-replacement-sso-secret",
        "sso-rw": invalid_value
        if invalid_name == "sso-rw"
        else "planted-replacement-rw-secret",
    }
    supplied = iter((answers["sso"], answers["sso-rw"]))

    with pytest.raises(GrokError) as caught:
        await setup_web_auth(
            path,
            manual=True,
            getpass_fn=lambda _: next(supplied),
        )

    assert caught.value.code == "GROK_WEB_AUTH_MISSING"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    if invalid_value:
        assert invalid_value not in str(caught.value)
    assert path.read_bytes() == original_bytes
    current_stat = path.stat()
    assert (
        current_stat.st_dev,
        current_stat.st_ino,
        current_stat.st_size,
        current_stat.st_mtime_ns,
    ) == original_fingerprint, case


class _TypedBuildFailure:
    async def __call__(self) -> dict[str, Any]:
        raise GrokError("GROK_BUILD_AUTH_MISSING", retryable=False)


class _TypedWebFailure:
    async def __call__(self) -> dict[str, Any]:
        raise GrokError("GROK_WEB_AUTH_MISSING", retryable=False)


@pytest.mark.asyncio
async def test_build_failure_does_not_prevent_web_setup() -> None:
    calls: list[str] = []

    async def web_setup() -> dict[str, Any]:
        calls.append("web")
        return {
            "configured": True,
            "authenticated": True,
            "expires_at": 1_800_000_000,
            "cookie_names": ["sso", "sso-rw"],
        }

    receipt = await run_grok_setup(
        build_probe=_TypedBuildFailure(),
        web_setup=web_setup,
    )

    assert calls == ["web"]
    assert receipt == {
        "status": "partial",
        "build": {"status": "failed", "code": "GROK_BUILD_AUTH_MISSING"},
        "web": {"status": "ok", "code": "OK"},
    }


@pytest.mark.asyncio
async def test_web_failure_does_not_erase_successful_build_probe() -> None:
    calls: list[str] = []

    async def build_probe() -> dict[str, Any]:
        calls.append("build")
        return {
            "installed": True,
            "version": "planted-version",
            "authenticated": True,
            "default_model": "planted-model",
            "models_count": 2,
        }

    receipt = await run_grok_setup(
        build_probe=build_probe,
        web_setup=_TypedWebFailure(),
    )

    assert calls == ["build"]
    assert receipt == {
        "status": "partial",
        "build": {"status": "ok", "code": "OK"},
        "web": {"status": "failed", "code": "GROK_WEB_AUTH_MISSING"},
    }
    assert "planted-version" not in repr(receipt)
    assert "planted-model" not in repr(receipt)


@pytest.mark.asyncio
async def test_both_lanes_are_attempted_and_receipt_never_contains_failures() -> None:
    calls: list[str] = []

    async def build_failure() -> dict[str, Any]:
        calls.append("build")
        raise GrokError("GROK_BUILD_FAILED", retryable=False)

    async def web_failure() -> dict[str, Any]:
        calls.append("web")
        raise GrokError("GROK_WEB_FAILED", retryable=False)

    receipt = await run_grok_setup(
        build_probe=build_failure,
        web_setup=web_failure,
    )

    assert calls == ["build", "web"]
    assert receipt == {
        "status": "failed",
        "build": {"status": "failed", "code": "GROK_BUILD_FAILED"},
        "web": {"status": "failed", "code": "GROK_WEB_FAILED"},
    }
    rendered = repr(receipt)
    assert "exception" not in rendered.lower()
    assert "cookie" not in rendered.lower()
    assert "identity" not in rendered.lower()


def test_grok_setup_help_has_no_account_or_browser_side_effects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    completed = subprocess.run(
        [sys.executable, "-m", "gpt2agent", "grok-setup", "--help"],
        cwd=Path(__file__).parents[1],
        env={"HOME": str(home), "PATH": ""},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert "--refresh" in completed.stdout
    assert "--chrome-profile" in completed.stdout
    assert "--manual" in completed.stdout
    assert not (home / ".gpt2agent").exists()


def test_imported_auth_round_trips_through_store(tmp_path: Path) -> None:
    now = int(time.time())
    runner = RecordingRunner(_successful_results(now=now))
    path = tmp_path / "private" / "auth.json"

    async def exercise() -> None:
        await import_browser_web_auth(
            path,
            runner=runner,
            resolver=lambda _: "browser-use",
            session_factory=lambda: "gpt2agent-grok-round-trip",
        )

    import asyncio

    asyncio.run(exercise())
    assert GrokWebAuthStore(path).status()["authenticated"] is True
