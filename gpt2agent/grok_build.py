"""Bounded official-CLI client for Grok Build account operations."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from ._bounded_process import (
    BoundedProcessError,
    ProcessResult,
    run_bounded_process,
)
from .errors import InputValidationError
from .grok_errors import GrokError
from .grok_paths import RootPolicy


_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_VERSION_RE = re.compile(
    r"^grok[ \t]+([A-Za-z0-9][A-Za-z0-9._+-]{0,63})(?:[ \t]|$)",
    re.MULTILINE,
)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STOP_REASON_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_PROMPT_MAX_BYTES = 65_536
_CHANGED_PATH_MAX_BYTES = 1_024
_CHANGED_PATH_MAX_COUNT = 256
_USAGE_MAX = 2**63 - 1
_USAGE_KEYS = (
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "cacheReadTokens",
    "cacheCreationTokens",
)
_AUTH_MARKERS = (
    "not authenticated",
    "authentication required",
    "please log in",
    "login --oauth",
    "unauthorized",
)
_QUOTA_MARKERS = (
    "quota",
    "rate limit",
    "rate-limit",
    "too many requests",
    "usage limit",
    "resource exhausted",
)
_CWD_INVARIANT = (
    "grok_build.cwd must be an existing directory under a configured root"
)


Runner = Callable[..., Awaitable[ProcessResult]]
Resolver = Callable[[str], str | None]


def _invalid(invariant: str) -> InputValidationError:
    return InputValidationError(invariant)


def _finite_float(value: Any, invariant: str) -> float:
    if isinstance(value, bool):
        raise _invalid(invariant)
    if isinstance(value, float) and not math.isfinite(value):
        raise _invalid(invariant)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise _invalid(invariant) from None
    if not math.isfinite(result):
        raise _invalid(invariant)
    return result


def _bounded_int(value: Any, invariant: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise _invalid(invariant)
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        raise _invalid(invariant)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise _invalid(invariant) from None
    if not minimum <= result <= maximum:
        raise _invalid(invariant)
    return result


@dataclass(frozen=True)
class GrokBuildConfig:
    command: str
    home: Path | None
    auth_path: Path | None
    roots: tuple[Path, ...]
    default_model: str | None
    timeout_seconds: float
    max_output_bytes: int
    default_max_turns: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> GrokBuildConfig:
        data = dict(values or {})
        command = str(data.get("command", "grok")).strip()
        if not command:
            raise _invalid("grok_build.command must not be empty")
        raw_roots = data.get("roots", [])
        if not isinstance(raw_roots, list) or not all(
            isinstance(value, str) and value.strip() for value in raw_roots
        ):
            raise _invalid("grok_build.roots must be a string list")
        roots = tuple(Path(value).expanduser().resolve() for value in raw_roots)
        home_value = data.get("home")
        auth_value = data.get("auth_path")
        model_value = data.get("default_model")
        default_model = str(model_value).strip() if model_value else None
        if default_model is not None and not _MODEL_ID_RE.fullmatch(default_model):
            raise _invalid("grok_build.default_model is invalid")
        timeout_seconds = _finite_float(
            data.get("timeout_seconds", 120.0),
            "grok_build.timeout_seconds must be 1..600",
        )
        if not 1.0 <= timeout_seconds <= 600.0:
            raise _invalid("grok_build.timeout_seconds must be 1..600")
        max_output_bytes = _bounded_int(
            data.get("max_output_bytes", 1_048_576),
            "grok_build.max_output_bytes is out of range",
            1_024,
            16_777_216,
        )
        default_max_turns = _bounded_int(
            data.get("default_max_turns", 20),
            "grok_build.default_max_turns must be 1..100",
            1,
            100,
        )
        return cls(
            command=command,
            home=Path(str(home_value)).expanduser().resolve()
            if home_value
            else None,
            auth_path=Path(str(auth_value)).expanduser().resolve()
            if auth_value
            else None,
            roots=roots,
            default_model=default_model,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            default_max_turns=default_max_turns,
        )


@dataclass(frozen=True)
class GrokBuildModelCatalog:
    authenticated: bool
    default_model: str | None
    models: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "default_model": self.default_model,
            "models": list(self.models),
            "count": len(self.models),
        }


@dataclass(frozen=True)
class GrokBuildResult:
    surface: str
    status: str
    session_id: str | None
    model: str
    text: str
    stop_reason: str | None
    usage: dict[str, int] | None
    changed_files: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "status": self.status,
            "session_id": self.session_id,
            "model": self.model,
            "text": self.text,
            "stop_reason": self.stop_reason,
            "usage": dict(self.usage) if self.usage is not None else None,
            "changed_files": list(self.changed_files),
        }


def _grok_error(code: str) -> GrokError:
    return GrokError(
        code,
        retryable=code in {"GROK_BUILD_QUOTA", "GROK_BUILD_TIMEOUT"},
    )


def _classified_error(text: str) -> GrokError:
    lowered = text.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return _grok_error("GROK_BUILD_AUTH_MISSING")
    if any(marker in lowered for marker in _QUOTA_MARKERS):
        return _grok_error("GROK_BUILD_QUOTA")
    return _grok_error("GROK_BUILD_FAILED")


def _parse_version(text: str) -> str:
    match = _VERSION_RE.search(text)
    if match is None:
        raise _grok_error("GROK_BUILD_FAILED")
    return match.group(1)


def _parse_models(text: str) -> GrokBuildModelCatalog:
    lowered = text.lower()
    authenticated = not any(marker in lowered for marker in _AUTH_MARKERS)

    default_model: str | None = None
    models: list[str] = []
    saw_catalog = False
    in_models = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered_line = line.lower()
        if lowered_line.startswith("default model:"):
            saw_catalog = True
            candidate = line.split(":", 1)[1].strip()
            if not _MODEL_ID_RE.fullmatch(candidate):
                raise _grok_error("GROK_BUILD_FAILED")
            default_model = candidate
            continue
        if lowered_line == "available models:":
            saw_catalog = True
            in_models = True
            continue
        if not in_models or not line:
            continue

        bullet = next(
            (prefix for prefix in ("- ", "* ", "• ") if line.startswith(prefix)),
            None,
        )
        if bullet is not None:
            candidate = line[len(bullet) :].split(None, 1)[0].rstrip(":")
        else:
            if not raw_line[:1].isspace():
                break
            raw_candidate = line.split(None, 1)[0]
            candidate = raw_candidate.rstrip(":")
            if not any(marker in candidate for marker in "-./:"):
                break
        if not _MODEL_ID_RE.fullmatch(candidate):
            break
        if candidate not in models:
            models.append(candidate)

    if not authenticated:
        return GrokBuildModelCatalog(False, default_model, ())
    if (
        not saw_catalog
        or not models
        or (default_model is not None and default_model not in models)
    ):
        raise _grok_error("GROK_BUILD_FAILED")
    return GrokBuildModelCatalog(True, default_model, tuple(models))


def _parse_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    usage = {
        key: raw_value
        for key in _USAGE_KEYS
        if isinstance((raw_value := value.get(key)), int)
        and not isinstance(raw_value, bool)
        and 0 <= raw_value <= _USAGE_MAX
    }
    return usage or None


def _safe_changed_path(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or not value.isprintable()
        or "\x00" in value
    ):
        return None
    try:
        if len(value.encode("utf-8")) > _CHANGED_PATH_MAX_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
    ):
        return None
    parts = value.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return value


def _parse_changed_files(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    changed: list[str] = []
    for raw_path in value[:_CHANGED_PATH_MAX_COUNT]:
        path = _safe_changed_path(raw_path)
        if path is not None and path not in changed:
            changed.append(path)
    return tuple(changed)


class GrokBuildClient:
    def __init__(
        self,
        config: GrokBuildConfig,
        *,
        runner: Runner = run_bounded_process,
        resolver: Resolver = shutil.which,
        cwd_policy: RootPolicy | None = None,
    ) -> None:
        self.config = config
        self._runner = runner
        self._resolver = resolver
        self._cwd_policy = cwd_policy or RootPolicy(config.roots)

    def _resolved_command(self) -> str:
        expanded = str(Path(self.config.command).expanduser())
        is_explicit = (
            Path(expanded).is_absolute()
            or os.sep in expanded
            or bool(os.altsep and os.altsep in expanded)
        )
        if is_explicit:
            return self._validated_executable(expanded)
        resolved = self._resolver(expanded)
        if not resolved:
            raise _grok_error("GROK_BUILD_CLI_NOT_FOUND")
        if (
            Path(resolved).is_absolute()
            or os.sep in resolved
            or bool(os.altsep and os.altsep in resolved)
        ):
            return self._validated_executable(resolved)
        return resolved

    @staticmethod
    def _validated_executable(value: str) -> str:
        try:
            path = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise _grok_error("GROK_BUILD_CLI_NOT_FOUND") from None
        if not path.is_file() or not os.access(path, os.X_OK):
            raise _grok_error("GROK_BUILD_CLI_NOT_FOUND")
        return str(path)

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("XAI_API_KEY", None)
        env.pop("GROK_CODE_XAI_API_KEY", None)
        if self.config.home is not None:
            env["GROK_HOME"] = str(self.config.home)
        if self.config.auth_path is not None:
            env["GROK_AUTH_PATH"] = str(self.config.auth_path)
        return env

    async def _run(
        self, args: Sequence[str], *, cwd: Path | None = None
    ) -> ProcessResult:
        process_cwd = Path.cwd().resolve() if cwd is None else cwd
        argv = [self._resolved_command(), "--no-auto-update", *args]
        try:
            return await self._runner(
                argv,
                cwd=process_cwd,
                env=self._environment(),
                timeout_seconds=self.config.timeout_seconds,
                max_output_bytes=self.config.max_output_bytes,
            )
        except BoundedProcessError as exc:
            if exc.code == "timeout":
                raise _grok_error("GROK_BUILD_TIMEOUT") from exc
            raise _grok_error("GROK_BUILD_OUTPUT_TOO_LARGE") from exc
        except FileNotFoundError as exc:
            if not process_cwd.is_dir():
                raise InputValidationError(_CWD_INVARIANT) from exc
            raise _grok_error("GROK_BUILD_CLI_NOT_FOUND") from exc

    @staticmethod
    def _decode(result: ProcessResult) -> tuple[str, str]:
        try:
            stdout = result.stdout.decode("utf-8")
            stderr = result.stderr.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _grok_error("GROK_BUILD_FAILED") from exc
        if result.returncode != 0:
            raise _classified_error(f"{stderr}\n{stdout}")
        return stdout, stderr

    async def _model_catalog(self) -> GrokBuildModelCatalog:
        stdout, _ = self._decode(await self._run(["models"]))
        return _parse_models(stdout)

    async def models(self) -> dict[str, Any]:
        """Return authenticated/default_model/models/count from ``grok models``."""
        return (await self._model_catalog()).as_dict()

    async def status(self) -> dict[str, Any]:
        """Return installed/version/authenticated/catalog counts without identity."""
        try:
            version_stdout, _ = self._decode(await self._run(["--version"]))
            version = _parse_version(version_stdout)
            catalog = await self._model_catalog()
        except GrokError as exc:
            if exc.code != "GROK_BUILD_CLI_NOT_FOUND":
                raise
            return {
                "installed": False,
                "version": None,
                "authenticated": False,
                "default_model": None,
                "models_count": 0,
            }
        return {
            "installed": True,
            "version": version,
            "authenticated": catalog.authenticated,
            "default_model": catalog.default_model,
            "models_count": len(catalog.models),
        }

    async def agent(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        mode: Literal["plan", "apply"] = "plan",
        model: str | None = None,
        max_turns: int | None = None,
        subagents: bool = False,
    ) -> dict[str, Any]:
        """Run one bounded official Build headless agent session."""
        self._validate_prompt(prompt)
        if mode not in ("plan", "apply"):
            raise _invalid("grok_build.mode must be plan or apply")
        turns = self.config.default_max_turns if max_turns is None else max_turns
        if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= 100:
            raise _invalid("grok_build.max_turns must be 1..100")
        if not isinstance(subagents, bool):
            raise _invalid("grok_build.subagents must be boolean")
        validated_cwd = self._cwd_policy.directory(cwd)

        catalog = await self._model_catalog()
        if not catalog.authenticated:
            raise _grok_error("GROK_BUILD_AUTH_MISSING")
        selected_model = (
            model
            if model is not None
            else self.config.default_model or catalog.default_model
        )
        if (
            not isinstance(selected_model, str)
            or _MODEL_ID_RE.fullmatch(selected_model) is None
            or selected_model not in catalog.models
        ):
            raise _invalid("grok_build.model must be in the account catalog")

        validated_cwd = self._cwd_policy.directory(validated_cwd)
        permission_mode, sandbox = (
            ("plan", "read-only")
            if mode == "plan"
            else ("bypassPermissions", "strict")
        )
        args = [
            "--cwd",
            str(validated_cwd),
            "-p",
            prompt,
            "--output-format",
            "json",
            "--max-turns",
            str(turns),
            "--no-memory",
            "--permission-mode",
            permission_mode,
            "--sandbox",
            sandbox,
        ]
        if not subagents:
            args.append("--no-subagents")
        args.extend(("--model", selected_model))
        stdout, stderr = self._decode(await self._run(args, cwd=validated_cwd))
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise _classified_error(f"{stderr}\n{stdout}") from exc
        if not isinstance(payload, dict):
            raise _grok_error("GROK_BUILD_FAILED")
        if payload.get("type") == "error":
            message = payload.get("message")
            raise _classified_error(message if isinstance(message, str) else "")
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _grok_error("GROK_BUILD_FAILED")

        raw_session_id = payload.get("sessionId")
        session_id = (
            raw_session_id
            if isinstance(raw_session_id, str)
            and _SESSION_ID_RE.fullmatch(raw_session_id) is not None
            else None
        )
        raw_stop_reason = payload.get("stopReason")
        stop_reason = (
            raw_stop_reason
            if isinstance(raw_stop_reason, str)
            and _STOP_REASON_RE.fullmatch(raw_stop_reason) is not None
            else None
        )
        return GrokBuildResult(
            surface="build",
            status="completed",
            session_id=session_id,
            model=selected_model,
            text=text,
            stop_reason=stop_reason,
            usage=_parse_usage(payload.get("usage")),
            changed_files=_parse_changed_files(payload.get("changedFiles")),
        ).as_dict()

    @staticmethod
    def _validate_prompt(prompt: str) -> None:
        if not isinstance(prompt, str):
            raise _invalid("grok_build.prompt must be 1..65536 UTF-8 bytes")
        try:
            size = len(prompt.encode("utf-8"))
        except UnicodeEncodeError:
            raise _invalid("grok_build.prompt must be 1..65536 UTF-8 bytes") from None
        if not prompt.strip() or size > _PROMPT_MAX_BYTES:
            raise _invalid("grok_build.prompt must be 1..65536 UTF-8 bytes")
