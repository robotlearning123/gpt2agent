#!/usr/bin/env python3
"""Verify installed account adapters against one closed synthetic corpus."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable


MAX_CORPUS_BYTES = 1024 * 1024
_MAX_CASES = 64
_MAX_JSON_DEPTH = 16
_MAX_JSON_NODES = 20_000
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_CATEGORIES = (
    "chat_models",
    "work_models",
    "apps",
    "plugins",
    "installed_plugins",
    "background_jobs",
    "scheduled_automations",
    "sites_access",
    "site_catalog",
    "custom_gpts",
    "codex",
)


class CorpusError(ValueError):
    """The installed adapter corpus contract failed closed."""


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _fail() -> CorpusError:
    return CorpusError("adapter corpus verification failed")


def _exact_dict(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _fail()
    return value


def _bounded_json(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > _MAX_JSON_DEPTH or counter[0] > _MAX_JSON_NODES:
        raise _fail()
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not (-1.0e308 <= value <= 1.0e308):
            raise _fail()
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 16 * 1024:
            raise _fail()
        return
    if isinstance(value, list):
        if len(value) > 1_000:
            raise _fail()
        for item in value:
            _bounded_json(item, depth=depth + 1, counter=counter)
        return
    if isinstance(value, dict):
        if len(value) > 1_000:
            raise _fail()
        for key, item in value.items():
            if not isinstance(key, str) or len(key.encode("utf-8")) > 256:
                raise _fail()
            _bounded_json(item, depth=depth + 1, counter=counter)
        return
    raise _fail()


def _read_fixture(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise _fail()
    try:
        if path.stat().st_size < 1 or path.stat().st_size > MAX_CORPUS_BYTES:
            raise _fail()
        raw = path.read_bytes()
        payload = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(_fail()),
        )
    except CorpusError:
        raise
    except Exception:
        raise _fail() from None
    _bounded_json(payload)
    return _exact_dict(payload, {"schema_version", "cases"})


def _adapter_dispatch() -> dict[str, Callable[[Any], Any]]:
    from gpt2agent.model_catalog import normalize_general_models, normalize_work_models
    from gpt2agent.tools.apps import normalize_apps
    from gpt2agent.tools.automations import normalize_scheduled_page
    from gpt2agent.tools.codex import normalize_codex_environments
    from gpt2agent.tools.conversations import normalize_background_tasks
    from gpt2agent.tools.gpts import normalize_custom_gpts
    from gpt2agent.tools.plugins import normalize_installed_plugins, normalize_plugin_catalog
    from gpt2agent.tools.sites import normalize_sites_access, normalize_sites_page

    return {
        "chat_models": lambda data: normalize_general_models(data["models"]),
        "work_models": lambda data: normalize_work_models(data["models"]),
        "apps": normalize_apps,
        "plugins": lambda data: normalize_plugin_catalog(data, limit=1, cursor=None),
        "installed_plugins": normalize_installed_plugins,
        "background_jobs": lambda data: normalize_background_tasks(data, limit=1),
        "scheduled_automations": normalize_scheduled_page,
        "sites_access": normalize_sites_access,
        "site_catalog": normalize_sites_page,
        "custom_gpts": normalize_custom_gpts,
        "codex": normalize_codex_environments,
    }


def _validate_cases(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload["schema_version"] != "1":
        raise _fail()
    cases = payload["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= _MAX_CASES:
        raise _fail()
    seen_ids: set[str] = set()
    categories_with_acceptance: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw_case in cases:
        case = _exact_dict(raw_case, {"id", "category", "input", "expected"})
        case_id = case["id"]
        category = case["category"]
        if (
            not isinstance(case_id, str)
            or _CASE_ID.fullmatch(case_id) is None
            or case_id in seen_ids
            or category not in _CATEGORIES
        ):
            raise _fail()
        expected = _exact_dict(case["expected"], {"status"}) if (
            isinstance(case["expected"], dict)
            and case["expected"].get("status") == "rejected"
        ) else _exact_dict(case["expected"], {"status", "value"})
        if expected["status"] not in {"accepted", "rejected"}:
            raise _fail()
        if expected["status"] == "accepted":
            categories_with_acceptance.add(category)
            _bounded_json(expected["value"])
        seen_ids.add(case_id)
        validated.append(case)
    if categories_with_acceptance != set(_CATEGORIES):
        raise _fail()
    return validated


def _run_case(
    case: dict[str, Any],
    dispatch: dict[str, Callable[[Any], Any]],
) -> dict[str, Any]:
    expected = case["expected"]
    try:
        value = dispatch[case["category"]](case["input"])
        _bounded_json(value)
    except Exception:
        if expected["status"] != "rejected":
            raise _fail() from None
        return {
            "category": case["category"],
            "id": case["id"],
            "status": "passed",
        }
    if expected["status"] != "accepted" or value != expected["value"]:
        raise _fail()
    return {
        "category": case["category"],
        "id": case["id"],
        "status": "passed",
        "value": value,
    }


def _write_output(path: Path, value: dict[str, Any]) -> None:
    path = Path(path)
    if not path.parent.is_dir() or path.exists() or path.is_symlink():
        raise _fail()
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise _fail() from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def verify_corpus(fixture: Path, output: Path) -> dict[str, Any]:
    payload = _read_fixture(fixture)
    cases = _validate_cases(payload)
    try:
        dispatch = _adapter_dispatch()
    except Exception:
        raise _fail() from None
    if set(dispatch) != set(_CATEGORIES) or any(
        not callable(adapter) for adapter in dispatch.values()
    ):
        raise _fail()
    results = [_run_case(case, dispatch) for case in cases]
    result = {
        "schema_version": "1",
        "counts": {
            "adapters_declared": len(_CATEGORIES),
            "adapters_exercised": len(_CATEGORIES),
            "adapters_passed": len(_CATEGORIES),
            "adapters_not_requested": 0,
            "cases_passed": len(results),
        },
        "cases": results,
    }
    _write_output(output, result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify_corpus(args.fixture, args.output)
    except Exception:
        print("adapter corpus verification failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
