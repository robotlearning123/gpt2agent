"""Regression tests for the v0.0.10 authentication audit fixes."""

from __future__ import annotations

import getpass
import json
from pathlib import Path

import pytest


def _write_token(path: Path, token: str, *, codex: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"tokens": {"access_token": token}} if codex else {"access_token": token}
    path.write_text(json.dumps(payload))


def test_noninteractive_get_token_never_calls_browser_sources(monkeypatch) -> None:
    from gpt2agent import auth

    interactive_calls: list[str] = []
    monkeypatch.setattr(auth, "_from_codex", lambda: None)
    monkeypatch.setattr(auth, "_from_saved", lambda: None)
    monkeypatch.setattr(
        auth,
        "_from_browser_use",
        lambda: interactive_calls.append("browser-use"),
    )
    monkeypatch.setattr(
        auth,
        "_from_browser",
        lambda: interactive_calls.append("manual"),
    )

    with pytest.raises(RuntimeError, match="No ChatGPT token found"):
        auth.get_token(interactive=False)

    assert interactive_calls == []


def test_browser_use_absence_never_requires_posix_which(monkeypatch) -> None:
    from gpt2agent import auth

    monkeypatch.setenv("PATH", "")

    def unexpected_subprocess(*args, **kwargs):
        raise AssertionError("browser-use subprocess must not run when it is absent")

    monkeypatch.setattr(auth.subprocess, "run", unexpected_subprocess)
    assert auth._from_browser_use() is None


def test_auth_manual_token_uses_hidden_input(monkeypatch) -> None:
    from gpt2agent import auth

    token = "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"
    monkeypatch.setattr(auth.webbrowser, "open", lambda *args, **kwargs: True)
    monkeypatch.setattr(getpass, "getpass", lambda *args, **kwargs: token)
    monkeypatch.setattr(
        "builtins.input",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("secret token input must not echo through input()")
        ),
    )

    assert auth._from_browser() == {"access_token": token, "source": "browser"}


def test_setup_manual_token_is_hidden_and_validated(monkeypatch) -> None:
    from gpt2agent import setup

    values = iter(["not-a-jwt", "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"])
    monkeypatch.setattr(setup.webbrowser, "open", lambda *args, **kwargs: True)
    monkeypatch.setattr(getpass, "getpass", lambda *args, **kwargs: next(values))
    monkeypatch.setattr(
        "builtins.input",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("secret token input must not echo through input()")
        ),
    )

    assert setup._token_via_manual() is None
    assert setup._token_via_manual() == "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"


@pytest.mark.parametrize(
    "token",
    [
        "eyJ..",
        "eyJ.payload.",
        "eyJ.pay+load.signature",
        "eyJ.pay/load.signature",
        "eyJ.payload.sign=ature",
        "eyJ.payload. signature",
        "eyJ.one.two.three",
    ],
)
def test_access_token_rejects_malformed_jwt_segments(token) -> None:
    from gpt2agent.auth import _is_access_token

    assert _is_access_token(token) is False


def test_backend_switches_to_new_preferred_codex_source(tmp_path, monkeypatch) -> None:
    from gpt2agent import backend

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    saved = tmp_path / ".gpt2agent" / "token.json"
    codex = tmp_path / ".codex" / "auth.json"
    _write_token(saved, "SAVED_OLD", codex=False)

    client = backend.BackendClient()
    assert "Authorization" not in client._session.headers
    assert client.request_headers()["Authorization"] == "Bearer SAVED_OLD"
    assert client._token_source == saved

    _write_token(codex, "CODEX_NEW", codex=True)
    client._reload_token_if_stale()

    assert "Authorization" not in client._session.headers
    assert client.request_headers()["Authorization"] == "Bearer CODEX_NEW"
    assert client._token_source == codex


def test_backend_falls_back_when_codex_source_disappears(tmp_path, monkeypatch) -> None:
    from gpt2agent import backend

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    saved = tmp_path / ".gpt2agent" / "token.json"
    codex = tmp_path / ".codex" / "auth.json"
    _write_token(saved, "SAVED_FALLBACK", codex=False)
    _write_token(codex, "CODEX_OLD", codex=True)

    client = backend.BackendClient()
    assert client._token_source == codex

    codex.unlink()
    client._reload_token_if_stale()

    assert "Authorization" not in client._session.headers
    assert client.request_headers()["Authorization"] == "Bearer SAVED_FALLBACK"
    assert client._token_source == saved


def test_backend_error_names_selected_codex_auth_path(tmp_path, monkeypatch) -> None:
    from gpt2agent import backend

    home = tmp_path / "home"
    codex_home = tmp_path / "alternate-codex"
    home.mkdir()
    codex_home.mkdir()
    selected = codex_home / "auth.json"
    selected.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(RuntimeError) as exc_info:
        backend._load_token_with_source()

    assert str(selected) in str(exc_info.value)


def test_setup_reports_selected_codex_auth_path(tmp_path, monkeypatch, capsys) -> None:
    from gpt2agent import setup

    codex_home = tmp_path / "alternate-codex"
    selected = codex_home / "auth.json"
    _write_token(selected, "ALT_TOKEN", codex=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert setup.get_token() == "ALT_TOKEN"
    assert str(selected) in capsys.readouterr().out


class _PlanClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error

    def get(self, path: str):
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    "client,match",
    [
        (_PlanClient({"accounts": {}}), "verify"),
        (_PlanClient(error=RuntimeError("401 Unauthorized")), "401 Unauthorized"),
    ],
)
def test_setup_does_not_claim_plus_for_unverified_account(monkeypatch, client, match) -> None:
    from gpt2agent import backend, setup

    monkeypatch.setattr(backend, "BackendClient", lambda: client)

    with pytest.raises(RuntimeError, match=match):
        setup.detect_plan()


def test_setup_detects_verified_free_account(monkeypatch) -> None:
    from gpt2agent import backend, setup

    response = {
        "accounts": {
            "acct": {
                "entitlement": {
                    "subscription_plan": "free",
                    "has_active_subscription": False,
                }
            }
        }
    }
    monkeypatch.setattr(backend, "BackendClient", lambda: _PlanClient(response))

    assert setup.detect_plan() == "free"


def test_setup_detect_plan_uses_injected_authenticated_client() -> None:
    from gpt2agent import setup

    response = {
        "accounts": {
            "acct": {
                "entitlement": {
                    "subscription_plan": "pro",
                    "has_active_subscription": True,
                }
            }
        }
    }
    client = _PlanClient(response)

    assert setup.detect_plan(client) == "pro"


def test_setup_detects_live_pro_slug_only_with_explicit_active_entitlement() -> None:
    from gpt2agent import setup

    active = _PlanClient(
        {
            "accounts": {
                "acct": {
                    "entitlement": {
                        "subscription_plan": "chatgptpro",
                        "has_active_subscription": True,
                    }
                }
            }
        }
    )
    assert setup.detect_plan(active) == "pro"

    for untrusted in (
        {"subscription_plan": "chatgptpro"},
        {"subscription_plan": "promotional", "has_active_subscription": True},
        {"subscription_plan": "chatgptpro", "has_active_subscription": "true"},
    ):
        client = _PlanClient({"accounts": {"acct": {"entitlement": untrusted}}})
        with pytest.raises(RuntimeError, match="plan"):
            setup.detect_plan(client)


@pytest.mark.parametrize("stale_plan", ["plus", "pro"])
def test_setup_explicit_inactive_subscription_is_free(monkeypatch, stale_plan) -> None:
    from gpt2agent import backend, setup

    response = {
        "accounts": {
            "acct": {
                "entitlement": {
                    "subscription_plan": stale_plan,
                    "has_active_subscription": False,
                }
            }
        }
    }
    monkeypatch.setattr(backend, "BackendClient", lambda: _PlanClient(response))

    assert setup.detect_plan() == "free"


def test_setup_malformed_entitlement_fails_before_install(monkeypatch) -> None:
    from gpt2agent import backend, install, setup

    response = {"accounts": {"acct": {"entitlement": "unknown"}}}
    installed: list[str] = []
    monkeypatch.setattr(backend, "BackendClient", lambda: _PlanClient(response))
    monkeypatch.setattr(setup, "get_token", lambda: "eyJ.header.signature")
    monkeypatch.setattr(setup, "save_token", lambda token: None)
    monkeypatch.setattr(
        install,
        "run_install",
        lambda **kwargs: installed.append("installed") or 0,
    )

    with pytest.raises(SystemExit) as exc_info:
        setup.run_setup()

    assert exc_info.value.code == 1
    assert installed == []
