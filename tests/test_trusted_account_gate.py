"""Adversarial tests for the trusted account verifier boundary."""

from __future__ import annotations

import ast
import base64
import builtins
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


TOKEN_CANARY = "eyJtrusted.canary.signature"


def _record_digest(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return "sha256=" + digest.rstrip(b"=").decode("ascii")


def _exact_distribution_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    from scripts.verify_account_receipt import _TRUSTED_DISTRIBUTIONS

    venv = tmp_path / "venv"
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    for directory in (venv, venv / "lib", venv / "lib" / "python3.12", site):
        directory.chmod(0o700)
    first_payload: Path | None = None
    for index, (name, version) in enumerate(_TRUSTED_DISTRIBUTIONS.items()):
        module = site / f"reviewed_dependency_{index}.py"
        module.write_bytes(f'VALUE = "{name}=={version}"\n'.encode())
        module.chmod(0o600)
        if first_payload is None:
            first_payload = module
        dist_info = site / f"{name.replace('-', '_')}-{version}.dist-info"
        dist_info.mkdir()
        dist_info.chmod(0o700)
        metadata = dist_info / "METADATA"
        metadata.write_text(
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
            encoding="utf-8",
        )
        metadata.chmod(0o600)
        record = dist_info / "RECORD"
        rows: list[tuple[str, str, str]] = []
        for installed in (module, metadata):
            payload = installed.read_bytes()
            rows.append(
                (
                    installed.relative_to(site).as_posix(),
                    _record_digest(payload),
                    str(len(payload)),
                )
            )
        rows.append((record.relative_to(site).as_posix(), "", ""))
        stream = io.StringIO(newline="")
        csv.writer(stream, lineterminator="\n").writerows(rows)
        record.write_text(stream.getvalue(), encoding="utf-8")
        record.chmod(0o600)
    assert first_payload is not None
    return venv, site, first_payload


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _checkout(tmp_path: Path) -> tuple[Path, str, str]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.email", "gate-tests@example.invalid")
    _git(checkout, "config", "user.name", "Gate Tests")
    (checkout / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "gpt2agent"\nversion = "0.0.12"\n',
        encoding="utf-8",
    )
    _git(checkout, "add", ".")
    _git(checkout, "commit", "--quiet", "-m", "fixture")
    return checkout, _git(checkout, "rev-parse", "HEAD"), _git(
        checkout, "rev-parse", "HEAD^{tree}"
    )


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "candidate-dist"
    dist.mkdir()
    (dist / "gpt2agent-0.0.12-py3-none-any.whl").write_bytes(
        b"hostile wheel bytes: import gpt2agent; exfiltrate()"
    )
    (dist / "gpt2agent-0.0.12.tar.gz").write_bytes(
        b"hostile sdist bytes: build_backend = exfiltrate"
    )
    return dist


def _response(category: str, url: str, *, sites_enabled: bool = True):
    from scripts.verify_account_receipt import RawResponse

    payloads = {
        "plan_entitlement": {
            "accounts": {
                "personal": {
                    "entitlement": {
                        "subscription_plan": "pro",
                        "has_active_subscription": True,
                    }
                }
            }
        },
        "chat_models": {"models": [{"slug": "synthetic"}]},
        "work_models": {"models": [{"slug": "synthetic"}]},
        "apps": {"apps": [{"id": "synthetic"}]},
        "plugins": [{"id": "synthetic"}],
        "installed_plugins": {
            "plugins": {"results": [{"id": "synthetic"}], "page": {"has_more": False}}
        },
        "background_jobs": {"tasks": [{"task_id": "synthetic"}]},
        "scheduled_automations": {"items": [{"id": "synthetic"}], "cursor": None},
        "sites_access": {
            "enabled": sites_enabled,
            "custom_domains_enabled": False,
            "requires_workspace_slug": False,
        },
        "site_catalog": {"items": [{"id": "synthetic"}], "cursor": None},
        "custom_gpts": {"items": [{"gizmo": {"short_url": "synthetic"}}]},
        "codex": {"environments": [{"id": "synthetic"}]},
    }
    if category == "projects_candidate":
        return RawResponse(
            status=404,
            headers={"Content-Type": "application/json"},
            body=b"{}",
            url=url,
        )
    body = json.dumps(payloads[category]).encode("utf-8")
    return RawResponse(
        status=200,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
        url=url,
    )


def _gate_kwargs(tmp_path: Path) -> tuple[dict[str, object], Path]:
    checkout, commit, tree = _checkout(tmp_path)
    dist = _dist(tmp_path)
    output = tmp_path / "receipt.json"
    return (
        {
            "checkout": checkout,
            "dist": dist,
            "output": output,
            "declared_commit": commit,
            "declared_tree": tree,
            "expected_plan": "pro",
            "ci_repository": "robotlearning123/gpt2agent",
            "ci_run_id": "12345",
            "ci_run_attempt": "2",
            "ci_artifact_id": "67890",
            "ci_artifact_digest": "sha256:" + "a" * 64,
            "ci_artifact_size": "31415",
            "ci_artifact_expires_at": "2099-07-10T13:17:42Z",
        },
        output,
    )


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _trusted_payload(
    *,
    sites_enabled: bool = True,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict[str, object]:
    from scripts.verify_account_receipt import run_probe_sequence

    def requester(**request):
        from scripts.verify_account_receipt import probe_from_url

        probe = probe_from_url(request["url"])
        return _response(probe.category, request["url"], sites_enabled=sites_enabled)

    now = datetime.now(timezone.utc)
    completed_at = now if completed_at is None else completed_at
    started_at = completed_at - timedelta(seconds=1) if started_at is None else started_at
    return {
        "schema_version": "4",
        "package_version": "0.0.12",
        "plan_class": "pro",
        "started_at": _stamp(started_at),
        "completed_at": _stamp(completed_at),
        "shape_results": run_probe_sequence(
            requester=requester,
            auth_headers={"Authorization": f"Bearer {TOKEN_CANARY}"},
        ),
    }


def test_create_gate_never_executes_or_imports_candidate_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.verify_account_receipt import run_create_gate

    kwargs, output = _gate_kwargs(tmp_path)
    imported: list[str] = []
    commands: list[tuple[str, ...]] = []
    original_import = builtins.__import__
    original_run = subprocess.run

    def guarded_import(name, *args, **options):
        imported.append(name)
        if name == "gpt2agent" or name.startswith("gpt2agent."):
            raise AssertionError("candidate package import attempted")
        return original_import(name, *args, **options)

    def guarded_run(argv, *args, **options):
        command = tuple(str(value) for value in argv)
        commands.append(command)
        assert command[0] == "/usr/bin/git"
        return original_run(argv, *args, **options)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(subprocess, "run", guarded_run)

    digest = run_create_gate(
        **kwargs,
        trusted_probe_runner=lambda expected_plan: _trusted_payload(),
    )

    assert len(digest) == 64
    assert output.is_file()
    assert commands and all(command[0] == "/usr/bin/git" for command in commands)
    assert all(name != "gpt2agent" and not name.startswith("gpt2agent.") for name in imported)


def test_create_gate_rechecks_inert_artifact_bytes_after_live_probe(tmp_path: Path) -> None:
    from scripts.verify_account_receipt import ReceiptError, run_create_gate

    kwargs, output = _gate_kwargs(tmp_path)
    wheel = Path(kwargs["dist"]) / "gpt2agent-0.0.12-py3-none-any.whl"

    def mutate_candidate(_expected_plan: str) -> dict[str, object]:
        wheel.write_bytes(wheel.read_bytes() + b"changed")
        return _trusted_payload()

    with pytest.raises(ReceiptError, match="changed during the live gate"):
        run_create_gate(**kwargs, trusted_probe_runner=mutate_candidate)
    assert not output.exists()


@pytest.mark.parametrize("timing", ("stale", "future", "too-long"))
def test_create_gate_rejects_replayed_or_implausible_live_probe_timestamps(
    tmp_path: Path,
    timing: str,
) -> None:
    from scripts.verify_account_receipt import ReceiptError, run_create_gate

    kwargs, output = _gate_kwargs(tmp_path)
    now = datetime.now(timezone.utc)
    if timing == "stale":
        completed = now - timedelta(minutes=31)
        started = completed - timedelta(seconds=1)
    elif timing == "future":
        completed = now + timedelta(minutes=2)
        started = completed - timedelta(seconds=1)
    else:
        completed = now
        started = completed - timedelta(minutes=11)

    with pytest.raises(ReceiptError, match="freshness"):
        run_create_gate(
            **kwargs,
            trusted_probe_runner=lambda _expected: _trusted_payload(
                started_at=started,
                completed_at=completed,
            ),
        )
    assert not output.exists()


def test_exact_plan_route_and_parser_fail_closed() -> None:
    from scripts.verify_account_receipt import (
        ReceiptError,
        parse_active_pro_entitlement,
        validate_probe_request,
    )

    plan = validate_probe_request(
        "GET", "/backend-api/accounts/check/v4-2023-04-27", None
    )
    assert plan.category == "plan_entitlement"
    with pytest.raises(ReceiptError, match="not permitted"):
        validate_probe_request(
            "GET", "/backend-api/accounts/check/v4-2023-04-27", {"account": "personal"}
        )

    for slug in ("pro", "chatgptpro"):
        assert (
            parse_active_pro_entitlement(
                {
                    "accounts": {
                        "personal": {
                            "entitlement": {
                                "subscription_plan": slug,
                                "has_active_subscription": True,
                            }
                        }
                    }
                }
            )
            == "pro"
        )

    assert (
        parse_active_pro_entitlement(
            {
                "accounts": {
                    "personal": {
                        "entitlement": {
                            "subscription_plan": "chatgptpro",
                            "has_active_subscription": True,
                        }
                    },
                    "secondary": {
                        "entitlement": {
                            "subscription_plan": "pro",
                            "has_active_subscription": True,
                        }
                    },
                }
            }
        )
        == "pro"
    )

    rejected_entitlements = (
        {"subscription_plan": "plus", "has_active_subscription": True},
        {"subscription_plan": "free", "has_active_subscription": False},
        {"subscription_plan": "pro", "has_active_subscription": False},
        {"subscription_plan": "PRO", "has_active_subscription": True},
        {"subscription_plan": "pro", "has_active_subscription": "true"},
        {"subscription_plan": "pro"},
        {},
    )
    for entitlement in rejected_entitlements:
        with pytest.raises(ReceiptError, match="plan"):
            parse_active_pro_entitlement(
                {"accounts": {"personal": {"entitlement": entitlement}}}
            )

    ambiguous = {
        "accounts": {
            "one": {
                "entitlement": {
                    "subscription_plan": "pro",
                    "has_active_subscription": True,
                }
            },
            "two": {
                "entitlement": {
                    "subscription_plan": "plus",
                    "has_active_subscription": True,
                }
            },
        }
    }
    with pytest.raises(ReceiptError, match="plan"):
        parse_active_pro_entitlement(ambiguous)


@pytest.mark.parametrize(
    "body",
    (
        b'{"accounts":{},"accounts":{"account":{"entitlement":{"subscription_plan":"pro","has_active_subscription":true}}}}',
        b'{"accounts":{"same":{"entitlement":{"subscription_plan":"plus","has_active_subscription":true}},"same":{"entitlement":{"subscription_plan":"pro","has_active_subscription":true}}}}',
        b'{"accounts":{"account":{"entitlement":{"subscription_plan":"plus","subscription_plan":"pro","has_active_subscription":true}}}}',
        b'{"accounts":{"account":{"entitlement":{"subscription_plan":"pro","has_active_subscription":false,"has_active_subscription":true}}}}',
    ),
)
def test_plan_probe_rejects_duplicate_json_keys_without_echoing_values(body: bytes) -> None:
    from scripts.verify_account_receipt import RawResponse, ReceiptError, execute_plan_probe

    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    response = RawResponse(
        status=200,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
        url=url,
    )

    with pytest.raises(ReceiptError, match="plan response is invalid") as caught:
        execute_plan_probe(requester=lambda **_request: response, auth_headers={})

    assert "same" not in str(caught.value)
    assert "plus" not in str(caught.value)


def test_plan_probe_rejects_overflowed_json_number() -> None:
    from scripts.verify_account_receipt import RawResponse, ReceiptError, execute_plan_probe

    body = (
        b'{"accounts":{"account":{"entitlement":{"subscription_plan":"pro",'
        b'"has_active_subscription":true}}},"ignored_score":1e9999}'
    )
    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    response = RawResponse(
        status=200,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
        url=url,
    )

    with pytest.raises(ReceiptError, match="plan response is invalid"):
        execute_plan_probe(requester=lambda **_request: response, auth_headers={})


@pytest.mark.parametrize(
    "body",
    (
        b'{"models":[],"models":[{"slug":"synthetic"}]}',
        b'{"models":[{"slug":"hidden","slug":"synthetic"}]}',
        b'{"models":[{"slug":"synthetic","score":NaN}]}',
        b'{"models":[{"slug":"synthetic","score":Infinity}]}',
        b'{"models":[{"slug":"synthetic","score":-Infinity}]}',
        b'{"models":[{"slug":"synthetic","score":1e9999}]}',
    ),
)
def test_route_shape_rejects_non_strict_json_without_echoing_values(body: bytes) -> None:
    from scripts.verify_account_receipt import PROBES, RawResponse, ReceiptError, execute_probe

    probe = PROBES[0]
    url = "https://chatgpt.com/backend-api/models?history_and_training_disabled=false"
    response = RawResponse(
        status=200,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
        url=url,
    )

    with pytest.raises(ReceiptError, match="not valid JSON") as caught:
        execute_probe(
            probe,
            requester=lambda **_request: response,
            auth_headers={},
        )

    assert "hidden" not in str(caught.value)


def test_trusted_live_probe_keeps_token_only_in_fake_transport_headers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.verify_account_receipt import probe_from_url, run_trusted_live_probe

    observed_headers: list[dict[str, str]] = []
    closed: list[bool] = []

    def requester(**request):
        observed_headers.append(dict(request["headers"]))
        probe = probe_from_url(request["url"])
        return _response(probe.category, request["url"], sites_enabled=False)

    @contextmanager
    def requester_context(**_kwargs):
        try:
            yield requester
        finally:
            closed.append(True)

    before = set(tmp_path.iterdir())
    payload = run_trusted_live_probe(
        "pro",
        token_loader=lambda: TOKEN_CANARY,
        requester_context=requester_context,
    )
    after = set(tmp_path.iterdir())

    assert closed == [True]
    assert before == after
    assert observed_headers
    assert all(headers["Authorization"] == f"Bearer {TOKEN_CANARY}" for headers in observed_headers)
    assert all(
        set(headers)
        == {
            "Accept",
            "Authorization",
            "OAI-Client-Build-Number",
            "OAI-Client-Version",
            "OAI-Device-Id",
            "OAI-Language",
            "OAI-Session-Id",
            "Origin",
            "Referer",
            "User-Agent",
            "X-OpenAI-Target-Path",
        }
        for headers in observed_headers
    )
    assert {headers["OAI-Device-Id"] for headers in observed_headers} == {
        observed_headers[0]["OAI-Device-Id"]
    }
    assert {headers["OAI-Session-Id"] for headers in observed_headers} == {
        observed_headers[0]["OAI-Session-Id"]
    }
    assert all(headers["OAI-Client-Build-Number"] == "5955942" for headers in observed_headers)
    assert all(
        headers["OAI-Client-Version"]
        == "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad"
        for headers in observed_headers
    )
    rendered = json.dumps(payload, sort_keys=True)
    captured = capsys.readouterr()
    assert TOKEN_CANARY not in rendered
    assert TOKEN_CANARY not in captured.out
    assert TOKEN_CANARY not in captured.err
    assert set(payload) == {
        "schema_version",
        "package_version",
        "plan_class",
        "started_at",
        "completed_at",
        "shape_results",
    }
    assert next(
        record for record in payload["shape_results"] if record["route_category"] == "site_catalog"
    )["status"] == "not_requested"


def test_trusted_live_probe_constructs_transport_before_loading_token() -> None:
    from scripts.verify_account_receipt import ReceiptError, run_trusted_live_probe

    events: list[str] = []

    @contextmanager
    def requester_context(**_kwargs):
        events.append("transport-enter")
        try:
            yield lambda **_request: None
        finally:
            events.append("transport-close")

    def token_loader() -> str:
        events.append("token-load")
        raise ReceiptError("synthetic auth failure")

    with pytest.raises(ReceiptError, match="synthetic auth failure"):
        run_trusted_live_probe(
            "pro",
            token_loader=token_loader,
            requester_context=requester_context,
        )

    assert events == ["transport-enter", "token-load", "transport-close"]


def test_trusted_live_probe_never_loads_token_when_transport_setup_fails() -> None:
    from scripts.verify_account_receipt import ReceiptError, run_trusted_live_probe

    token_loads = 0

    @contextmanager
    def requester_context(**_kwargs):
        raise ReceiptError("synthetic transport failure")
        yield  # pragma: no cover

    def token_loader() -> str:
        nonlocal token_loads
        token_loads += 1
        return TOKEN_CANARY

    with pytest.raises(ReceiptError, match="synthetic transport failure"):
        run_trusted_live_probe(
            "pro",
            token_loader=token_loader,
            requester_context=requester_context,
        )

    assert token_loads == 0


def test_auth_file_requires_owned_regular_private_bounded_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.verify_account_receipt import _account_token_from_file

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"tokens": {"access_token": TOKEN_CANARY}}),
        encoding="utf-8",
    )
    auth.chmod(0o600)
    assert _account_token_from_file(auth, codex=True) == TOKEN_CANARY

    auth.chmod(0o640)
    assert _account_token_from_file(auth, codex=True) is None
    auth.chmod(0o600)

    auth.write_bytes(
        b'{"tokens":{"access_token":"eyJfirst.canary.signature",'
        b'"access_token":"eyJsecond.canary.signature"}}'
    )
    assert _account_token_from_file(auth, codex=True) is None

    auth.write_bytes(
        b'{"tokens":{"access_token":"eyJsynthetic.canary.signature"},'
        b'"ignored_score":1e9999}'
    )
    assert _account_token_from_file(auth, codex=True) is None

    link = tmp_path / "auth-link.json"
    link.symlink_to(auth)
    assert _account_token_from_file(link, codex=True) is None

    if hasattr(os, "getuid"):
        monkeypatch.setattr(os, "getuid", lambda: auth.stat().st_uid + 1)
        assert _account_token_from_file(auth, codex=True) is None


def test_trusted_transport_disables_environment_proxy_redirects_and_closes_session(
    tmp_path: Path,
) -> None:
    from scripts.verify_account_receipt import trusted_curl_cffi_requester

    options: dict[str, object] = {}

    class Session:
        trust_env = False
        default_headers = False
        closed = False

        def close(self) -> None:
            self.closed = True

    session = Session()

    def factory(**kwargs):
        options.update(kwargs)
        return session

    runtime = SimpleNamespace(
        curl_opt=SimpleNamespace(MAXFILESIZE_LARGE=123),
        requests=SimpleNamespace(),
        original_sys_path=tuple(receipt_path for receipt_path in __import__("sys").path),
        inserted_site=None,
    )

    with trusted_curl_cffi_requester(
        trusted_site_packages=tmp_path,
        session_factory=factory,
        runtime_loader=lambda **_kwargs: runtime,
    ) as requester:
        assert callable(requester)

    assert options["trust_env"] is False
    assert options["impersonate"] == "chrome131"
    assert options["default_headers"] is False
    assert options["curl_options"] == {123: 4 * 1024 * 1024}
    assert session.closed is True


def test_trusted_transport_requires_an_explicit_isolated_site_packages() -> None:
    from scripts.verify_account_receipt import ReceiptError, trusted_curl_cffi_requester

    with pytest.raises(ReceiptError, match="runtime"):
        with trusted_curl_cffi_requester():
            raise AssertionError("ambient dependency context must not open")


@pytest.mark.parametrize(
    ("implementation", "version", "platform_name", "machine", "expected"),
    (
        ("cpython", (3, 12, 13), "linux", "x86_64", True),
        ("cpython", (3, 12, 12), "linux", "x86_64", False),
        ("cpython", (3, 12, 13), "darwin", "x86_64", False),
        ("cpython", (3, 12, 13), "linux", "aarch64", False),
        ("pypy", (3, 12, 13), "linux", "x86_64", False),
    ),
)
def test_trusted_runtime_identity_is_exactly_reviewed_target(
    implementation: str,
    version: tuple[int, int, int],
    platform_name: str,
    machine: str,
    expected: bool,
) -> None:
    from scripts.verify_account_receipt import _trusted_runtime_identity_is_exact

    assert (
        _trusted_runtime_identity_is_exact(
            implementation=implementation,
            version=version,
            platform_name=platform_name,
            machine=machine,
        )
        is expected
    )


def test_trusted_runtime_rejects_writable_ancestor_above_private_base() -> None:
    from scripts.verify_account_receipt import _path_has_secure_ancestors

    root = Path(tempfile.mkdtemp(prefix=".gpt2agent-ancestor-test.", dir=Path.home()))
    root.chmod(0o700)
    try:
        safe = root / "safe"
        safe.mkdir(mode=0o700)
        assert _path_has_secure_ancestors(safe, current_uid=os.getuid()) is True

        unsafe = root / "unsafe"
        unsafe.mkdir(mode=0o770)
        unsafe.chmod(0o770)
        assert unsafe.stat().st_mode & 0o777 == 0o770
        private_child = unsafe / "private-child"
        private_child.mkdir(mode=0o700)
        assert _path_has_secure_ancestors(private_child, current_uid=os.getuid()) is False
    finally:
        shutil.rmtree(root)


def test_trusted_runtime_rejects_record_hash_tampering(tmp_path: Path) -> None:
    from scripts.verify_account_receipt import ReceiptError, _require_exact_distribution_closure

    venv, site, payload = _exact_distribution_fixture(tmp_path)
    original = payload.read_bytes()
    payload.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    with pytest.raises(ReceiptError, match="integrity"):
        _require_exact_distribution_closure(site, venv)


def test_trusted_runtime_rejects_record_size_tampering(tmp_path: Path) -> None:
    from scripts.verify_account_receipt import ReceiptError, _require_exact_distribution_closure

    venv, site, payload = _exact_distribution_fixture(tmp_path)
    payload.write_bytes(payload.read_bytes() + b"#")

    with pytest.raises(ReceiptError, match="integrity"):
        _require_exact_distribution_closure(site, venv)


def test_trusted_runtime_rejects_unrecorded_importable_file(tmp_path: Path) -> None:
    from scripts.verify_account_receipt import ReceiptError, _require_exact_distribution_closure

    venv, site, _payload = _exact_distribution_fixture(tmp_path)
    (site / "unrecorded_backdoor.py").write_text("raise SystemExit(86)\n", encoding="utf-8")

    with pytest.raises(ReceiptError, match="unrecorded importable"):
        _require_exact_distribution_closure(site, venv)


def test_trusted_transport_rejects_symlinked_dependency_origin(tmp_path: Path) -> None:
    from scripts.verify_account_receipt import _dependency_origin_is_trusted

    package = tmp_path / "site-packages" / "curl_cffi"
    package.mkdir(parents=True)
    target = package / "real.py"
    target.write_text("# synthetic dependency\n", encoding="utf-8")
    link = package / "__init__.py"
    link.symlink_to(target)

    assert _dependency_origin_is_trusted(link, ()) is False


@pytest.mark.parametrize("unsafe", ("file-mode", "parent-mode"))
def test_trusted_transport_rejects_group_or_world_writable_dependency_paths(
    tmp_path: Path,
    unsafe: str,
) -> None:
    from scripts.verify_account_receipt import _dependency_origin_is_trusted

    package = tmp_path / "site-packages" / "curl_cffi"
    package.mkdir(parents=True)
    origin = package / "__init__.py"
    origin.write_text("# synthetic dependency\n", encoding="utf-8")
    if unsafe == "file-mode":
        origin.chmod(0o666)
    else:
        package.chmod(0o777)

    assert _dependency_origin_is_trusted(origin, ()) is False


def test_trusted_live_probe_does_not_retry_a_context_factory_type_error() -> None:
    from scripts.verify_account_receipt import ReceiptError, run_trusted_live_probe

    calls = 0

    @contextmanager
    def requester_context(**kwargs):
        nonlocal calls
        calls += 1
        if kwargs:
            raise TypeError("synthetic context defect")
        yield lambda **_request: None

    with pytest.raises(ReceiptError, match="transport"):
        run_trusted_live_probe(
            "pro",
            token_loader=lambda: TOKEN_CANARY,
            requester_context=requester_context,
        )
    assert calls == 1


def test_curl_requester_enforces_fixed_request_options_and_bounded_headers() -> None:
    from scripts.verify_account_receipt import (
        MAX_RESPONSE_HEADERS,
        CurlCffiRequester,
        RawResponse,
        ReceiptError,
        _trusted_headers,
    )

    class Response:
        status_code = 200
        url = "https://chatgpt.com/backend-api/apps/list"
        closed = False

        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

        def iter_content(self, chunk_size: int):
            assert chunk_size == 64 * 1024
            yield b'{"apps":[]}'

        def close(self) -> None:
            self.closed = True

    class Session:
        def __init__(self, response: Response) -> None:
            self.response = response
            self.calls: list[tuple[str, dict[str, object]]] = []

        def get(self, url: str, **kwargs):
            self.calls.append((url, kwargs))
            return self.response

    headers = _trusted_headers(TOKEN_CANARY)
    headers["X-OpenAI-Target-Path"] = "/backend-api/apps/list"
    response = Response({"Content-Type": "application/json"})
    session = Session(response)
    requester = CurlCffiRequester(session)

    result = requester(
        method="GET",
        url=response.url,
        headers=headers,
        timeout=20,
        max_bytes=4 * 1024 * 1024,
    )

    assert isinstance(result, RawResponse)
    assert response.closed is True
    assert session.calls == [
        (
            response.url,
            {
                "headers": headers,
                "timeout": 20,
                "allow_redirects": False,
                "max_redirects": 0,
                "proxy": "",
                "verify": True,
                "stream": True,
                "discard_cookies": True,
                "default_headers": False,
                "accept_encoding": None,
            },
        )
    ]

    oversized_headers = {
        f"X-Synthetic-{index}": "value" for index in range(MAX_RESPONSE_HEADERS + 1)
    }
    oversized_response = Response(oversized_headers)
    with pytest.raises(ReceiptError, match="metadata"):
        CurlCffiRequester(Session(oversized_response))(
            method="GET",
            url=oversized_response.url,
            headers=headers,
            timeout=20,
            max_bytes=4 * 1024 * 1024,
        )
    assert oversized_response.closed is True


def test_curl_requester_rejects_oversize_bearer_before_session_call() -> None:
    from scripts.verify_account_receipt import (
        MAX_RESPONSE_BYTES,
        REQUEST_TIMEOUT_SECONDS,
        CurlCffiRequester,
        ReceiptError,
        _MAX_TOKEN_BYTES,
        _trusted_headers,
    )

    class Session:
        def get(self, *_args, **_kwargs):
            raise AssertionError("invalid authorization must not reach the session")

    headers = _trusted_headers(TOKEN_CANARY)
    headers["Authorization"] = "Bearer eyJ" + "a" * _MAX_TOKEN_BYTES + ".b.c"
    headers["X-OpenAI-Target-Path"] = "/backend-api/apps/list"

    with pytest.raises(ReceiptError, match="headers"):
        CurlCffiRequester(Session())(
            method="GET",
            url="https://chatgpt.com/backend-api/apps/list",
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_bytes=MAX_RESPONSE_BYTES,
        )


def test_create_gate_rejects_live_runner_adapter_self_attestation(tmp_path: Path) -> None:
    from scripts.verify_account_receipt import ReceiptError, run_create_gate

    kwargs, output = _gate_kwargs(tmp_path)
    poisoned = _trusted_payload()
    poisoned["adapter_status"] = "passed"
    poisoned["adapter_counts"] = {
        "adapters_declared": 11,
        "adapters_exercised": 11,
        "adapters_passed": 11,
        "adapters_not_requested": 0,
    }

    with pytest.raises(ReceiptError, match="schema"):
        run_create_gate(**kwargs, trusted_probe_runner=lambda _expected: poisoned)
    assert not output.exists()


def test_verifier_version_six_and_fixed_offline_adapter_counts_when_sites_skip(
    tmp_path: Path,
) -> None:
    from scripts.verify_account_receipt import run_create_gate

    kwargs, output = _gate_kwargs(tmp_path)
    run_create_gate(
        **kwargs,
        trusted_probe_runner=lambda expected_plan: _trusted_payload(sites_enabled=False),
    )
    receipt = json.loads(output.read_text(encoding="utf-8"))

    assert receipt["schema_version"] == "4"
    assert receipt["verifier"] == {
        "name": "gpt2agent-account-receipt",
        "version": "6",
    }
    assert {
        key: receipt["counts"][key]
        for key in (
            "adapters_declared",
            "adapters_exercised",
            "adapters_passed",
            "adapters_not_requested",
        )
    } == {
        "adapters_declared": 11,
        "adapters_exercised": 11,
        "adapters_passed": 11,
        "adapters_not_requested": 0,
    }


def test_bearer_verifier_has_no_candidate_import_or_execution_boundary() -> None:
    project_root = Path(__file__).resolve().parents[1]
    verifier_tree = ast.parse(
        (project_root / "scripts" / "verify_account_receipt.py").read_text(encoding="utf-8")
    )
    corpus_tree = ast.parse(
        (project_root / "scripts" / "verify_installed_adapter_corpus.py").read_text(
            encoding="utf-8"
        )
    )

    verifier_imports = {
        name
        for node in ast.walk(verifier_tree)
        for name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module]
            if isinstance(node, ast.ImportFrom) and node.module is not None
            else []
        )
    }
    assert all(
        name != "gpt2agent" and not name.startswith("gpt2agent.")
        for name in verifier_imports
    )

    subprocess_calls = [
        node
        for node in ast.walk(verifier_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert len(subprocess_calls) == 1
    command = subprocess_calls[0].args[0]
    assert isinstance(command, ast.List)
    assert isinstance(command.elts[0], ast.Constant)
    assert command.elts[0].value == "/usr/bin/git"
    assert not any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in {"__import__", "eval", "exec"})
            or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and (
                    node.func.attr in {"popen", "system"}
                    or node.func.attr.startswith(("exec", "spawn"))
                )
            )
        )
        for node in ast.walk(verifier_tree)
    )

    corpus_imports = {
        node.module
        for node in ast.walk(corpus_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert any(
        name == "gpt2agent" or name.startswith("gpt2agent.") for name in corpus_imports
    )
