from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from gpt2agent import sentinel as sentinel_mod
from gpt2agent.errors import BackendContractError


class _Backend:
    def request_headers(self) -> dict[str, str]:
        return {"User-Agent": "test-agent"}


def _patch_response(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    encoded = json.dumps(payload).encode()

    class _Response:
        status_code = 200
        headers: dict[str, str] = {}

    class _Session:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **kwargs: Any) -> _Response:
            callback = kwargs.get("content_callback")
            if callback is not None:
                callback(encoded)
            return _Response()

    monkeypatch.setattr(sentinel_mod, "AsyncSession", _Session)
    monkeypatch.setattr(
        sentinel_mod._pow,
        "get_requirements_token",
        lambda _user_agent: "requirements-token",
    )


def _get_tokens(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> dict[str, str]:
    _patch_response(monkeypatch, payload)
    gate = sentinel_mod.SentinelGate(_Backend())  # type: ignore[arg-type]
    return asyncio.run(gate.get_tokens())


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(7, id="non-string"),
        pytest.param("", id="empty"),
        pytest.param("x" * 16_385, id="oversized"),
        pytest.param("safe\r\nInjected: value", id="header-control"),
    ],
)
def test_chat_requirements_token_must_be_bounded_header_safe_string(
    monkeypatch: pytest.MonkeyPatch, token: Any
) -> None:
    with pytest.raises(BackendContractError):
        _get_tokens(monkeypatch, {"token": token})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("seed", 7, id="seed-non-string"),
        pytest.param("seed", "", id="seed-empty"),
        pytest.param("seed", "x" * 257, id="seed-oversized"),
        pytest.param("seed", "seed\nvalue", id="seed-control"),
        pytest.param("difficulty", 7, id="difficulty-non-string"),
        pytest.param("difficulty", "", id="difficulty-empty"),
        pytest.param("difficulty", "f", id="difficulty-odd-length"),
        pytest.param("difficulty", "gg", id="difficulty-non-hex"),
        pytest.param("difficulty", "00" * 65, id="difficulty-oversized"),
    ],
)
def test_invalid_pow_inputs_never_reach_solver(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    challenge: dict[str, Any] = {
        "required": True,
        "seed": "seed",
        "difficulty": "0fffff",
    }
    challenge[field] = value
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sentinel_mod._pow,
        "solve_pow",
        lambda *args: calls.append(args) or "proof-token",
    )

    with pytest.raises(BackendContractError):
        _get_tokens(
            monkeypatch,
            {"token": "chat-token", "proofofwork": challenge},
        )

    assert calls == []


@pytest.mark.parametrize(
    "dx",
    [
        pytest.param(7, id="non-string"),
        pytest.param("", id="empty"),
        pytest.param("***=", id="non-base64"),
        pytest.param("AAAAA", id="invalid-padding"),
        pytest.param("A" * 65_540, id="oversized"),
    ],
)
def test_invalid_turnstile_dx_never_reaches_solver(
    monkeypatch: pytest.MonkeyPatch, dx: Any
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        sentinel_mod._turn,
        "solve_turnstile",
        lambda *args: calls.append(args) or "turnstile-token",
    )

    with pytest.raises(BackendContractError):
        _get_tokens(
            monkeypatch,
            {
                "token": "chat-token",
                "turnstile": {"required": True, "dx": dx},
            },
        )

    assert calls == []


@pytest.mark.parametrize("block_name", ["proofofwork", "turnstile"])
def test_required_flag_must_be_exact_boolean_before_solver_dispatch(
    monkeypatch: pytest.MonkeyPatch, block_name: str
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        sentinel_mod._pow,
        "solve_pow",
        lambda *_args: calls.append("pow") or "proof-token",
    )
    monkeypatch.setattr(
        sentinel_mod._turn,
        "solve_turnstile",
        lambda *_args: calls.append("turnstile") or "turnstile-token",
    )
    block: dict[str, Any] = {"required": "false"}
    if block_name == "proofofwork":
        block.update(seed="seed", difficulty="0fffff")
    else:
        block["dx"] = "W10="

    with pytest.raises(BackendContractError):
        _get_tokens(monkeypatch, {"token": "chat-token", block_name: block})

    assert calls == []


@pytest.mark.parametrize(
    ("solver_name", "payload", "message"),
    [
        pytest.param(
            "pow",
            {
                "token": "chat-token",
                "proofofwork": {
                    "required": True,
                    "seed": "seed",
                    "difficulty": "0fffff",
                },
            },
            "required POW challenge could not be solved",
            id="pow",
        ),
        pytest.param(
            "turnstile",
            {
                "token": "chat-token",
                "turnstile": {"required": True, "dx": "W10="},
            },
            "required Turnstile challenge could not be solved",
            id="turnstile",
        ),
    ],
)
def test_solver_exception_becomes_static_chain_free_contract_error(
    monkeypatch: pytest.MonkeyPatch,
    solver_name: str,
    payload: dict[str, Any],
    message: str,
) -> None:
    secret = "SYNTHETIC-SOLVER-SECRET"

    def _explode(*_args: Any) -> str:
        raise RuntimeError(secret)

    if solver_name == "pow":
        monkeypatch.setattr(sentinel_mod._pow, "solve_pow", _explode)
    else:
        monkeypatch.setattr(sentinel_mod._turn, "solve_turnstile", _explode)

    with pytest.raises(BackendContractError) as caught:
        _get_tokens(monkeypatch, payload)

    assert str(caught.value) == f"contract_changed: sentinel: {message}"
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("solver_name", "result"),
    [
        pytest.param("pow", {"opaque": "value"}, id="pow-non-string"),
        pytest.param("pow", "x" * 16_385, id="pow-oversized"),
        pytest.param("pow", "proof\r\nInjected: value", id="pow-control"),
        pytest.param("turnstile", {"opaque": "value"}, id="turnstile-non-string"),
        pytest.param("turnstile", "x" * 16_385, id="turnstile-oversized"),
        pytest.param(
            "turnstile",
            "token\r\nInjected: value",
            id="turnstile-control",
        ),
    ],
)
def test_invalid_solver_output_is_treated_as_static_solver_failure(
    monkeypatch: pytest.MonkeyPatch, solver_name: str, result: Any
) -> None:
    if solver_name == "pow":
        monkeypatch.setattr(sentinel_mod._pow, "solve_pow", lambda *_args: result)
        payload = {
            "token": "chat-token",
            "proofofwork": {
                "required": True,
                "seed": "seed",
                "difficulty": "0fffff",
            },
        }
        message = "required POW challenge could not be solved"
    else:
        monkeypatch.setattr(
            sentinel_mod._turn,
            "solve_turnstile",
            lambda *_args: result,
        )
        payload = {
            "token": "chat-token",
            "turnstile": {"required": True, "dx": "W10="},
        }
        message = "required Turnstile challenge could not be solved"

    with pytest.raises(BackendContractError) as caught:
        _get_tokens(monkeypatch, payload)

    assert str(caught.value) == f"contract_changed: sentinel: {message}"


def test_valid_challenges_dispatch_with_validated_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, tuple[str, ...]] = {}

    def _solve_pow(seed: str, difficulty: str, user_agent: str) -> str:
        calls["pow"] = (seed, difficulty, user_agent)
        return "proof-token"

    def _solve_turnstile(dx: str, proof: str) -> str:
        calls["turnstile"] = (dx, proof)
        return "turnstile-token"

    monkeypatch.setattr(sentinel_mod._pow, "solve_pow", _solve_pow)
    monkeypatch.setattr(sentinel_mod._turn, "solve_turnstile", _solve_turnstile)

    result = _get_tokens(
        monkeypatch,
        {
            "token": "chat-token",
            "proofofwork": {
                "required": True,
                "seed": "seed",
                "difficulty": "0fffff",
            },
            "turnstile": {"required": True, "dx": "W10="},
        },
    )

    assert calls == {
        "pow": ("seed", "0fffff", "test-agent"),
        "turnstile": ("W10=", "proof-token"),
    }
    assert result == {
        "chat-requirements": "chat-token",
        "proof": "proof-token",
        "turnstile": "turnstile-token",
    }
