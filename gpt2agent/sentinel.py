"""Sentinel gate: fetch chat-requirements, solve POW + turnstile."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from typing import TYPE_CHECKING, Any, Mapping

from gpt2agent._vendored import pow as _pow
from gpt2agent._vendored import turnstile as _turn
from gpt2agent.backend import (
    _BoundedResponseBody,
    _account_session_options,
    _reject_tls_key_logging,
    _is_filesize_exceeded,
)
from gpt2agent.errors import BackendContractError, BackendHTTPError, backend_http_error
from curl_cffi.requests import AsyncSession

if TYPE_CHECKING:
    from gpt2agent.backend import BackendClient

_CHAT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_HEADER_TOKEN_CHARS = 16 * 1024
_MAX_POW_SEED_CHARS = 256
_MAX_POW_DIFFICULTY_HEX_CHARS = 128
_MAX_TURNSTILE_DX_CHARS = 64 * 1024
_MAX_TURNSTILE_DECODED_BYTES = 48 * 1024
_VISIBLE_ASCII_RE = re.compile(r"[\x21-\x7e]+\Z")
_EVEN_HEX_RE = re.compile(r"(?:[0-9a-fA-F]{2})+\Z")


def _validate_json_size(response: Any, body: bytearray) -> None:
    headers = getattr(response, "headers", None)
    raw_length = None
    if headers is not None:
        raw_length = headers.get("Content-Length") or headers.get("content-length")
    if raw_length is not None:
        try:
            declared = int(raw_length)
        except (TypeError, ValueError):
            declared = -1
        if declared > _MAX_JSON_BYTES:
            raise BackendContractError("sentinel", "response exceeds 4 MiB")

    if len(body) > _MAX_JSON_BYTES:
        raise BackendContractError("sentinel", "response exceeds 4 MiB")


def _bounded_visible_ascii(value: object, *, max_chars: int) -> str | None:
    if type(value) is not str or not 0 < len(value) <= max_chars:
        return None
    if _VISIBLE_ASCII_RE.fullmatch(value) is None:
        return None
    return value


def _challenge_block(response: dict[str, Any], name: str) -> dict[str, Any]:
    block = response.get(name)
    if block is None:
        return {}
    if type(block) is not dict:
        raise BackendContractError("sentinel", f"{name} must be an object")
    return block


def _required(block: dict[str, Any], name: str) -> bool:
    required = block.get("required", False)
    if type(required) is not bool:
        raise BackendContractError(
            "sentinel", f"{name}.required must be a boolean"
        )
    return required


def _pow_inputs(block: dict[str, Any]) -> tuple[str, str]:
    seed = _bounded_visible_ascii(
        block.get("seed"), max_chars=_MAX_POW_SEED_CHARS
    )
    difficulty = block.get("difficulty")
    difficulty_valid = (
        type(difficulty) is str
        and 2 <= len(difficulty) <= _MAX_POW_DIFFICULTY_HEX_CHARS
        and len(difficulty) % 2 == 0
        and _EVEN_HEX_RE.fullmatch(difficulty) is not None
    )
    if seed is None or not difficulty_valid:
        raise BackendContractError(
            "sentinel", "required proof-of-work challenge is invalid"
        )
    return seed, difficulty


def _turnstile_input(block: dict[str, Any]) -> str:
    dx = block.get("dx")
    if type(dx) is not str or not 4 <= len(dx) <= _MAX_TURNSTILE_DX_CHARS:
        raise BackendContractError(
            "sentinel", "required Turnstile challenge could not be solved"
        )
    if len(dx) % 4 != 0:
        raise BackendContractError(
            "sentinel", "required Turnstile challenge could not be solved"
        )
    try:
        decoded = base64.b64decode(dx, validate=True)
    except (binascii.Error, ValueError):
        raise BackendContractError(
            "sentinel", "required Turnstile challenge could not be solved"
        ) from None
    if not decoded or len(decoded) > _MAX_TURNSTILE_DECODED_BYTES:
        raise BackendContractError(
            "sentinel", "required Turnstile challenge could not be solved"
        )
    return dx


def _solver_token(value: object) -> str | None:
    return _bounded_visible_ascii(value, max_chars=_MAX_HEADER_TOKEN_CHARS)


class SentinelGate:
    def __init__(self, backend: "BackendClient") -> None:
        self._backend = backend

    async def get_tokens(
        self, operation_headers: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Fetch single-use Sentinel tokens under one operation auth snapshot."""
        headers = (
            dict(operation_headers)
            if operation_headers is not None
            else self._backend.request_headers()
        )
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "*/*"

        ua = headers.get("User-Agent") or _CHAT_UA
        p = _pow.get_requirements_token(ua)

        url = "https://chatgpt.com/backend-api/sentinel/chat-requirements"

        body = _BoundedResponseBody(_MAX_JSON_BYTES)
        async with AsyncSession(**_account_session_options(_MAX_JSON_BYTES)) as s:
            response_oversized = False
            network_failed = False
            _reject_tls_key_logging()
            try:
                r = await s.post(
                    url,
                    headers=headers,
                    json={"p": p},
                    timeout=20,
                    allow_redirects=False,
                    content_callback=body,
                )
            except Exception as exc:
                response_oversized = body.overflowed or _is_filesize_exceeded(exc)
                network_failed = not response_oversized
            if response_oversized:
                raise BackendContractError(
                    "sentinel", "response exceeds 4 MiB"
                ) from None
            if network_failed:
                raise BackendHTTPError(
                    "POST",
                    "/backend-api/sentinel/chat-requirements",
                    None,
                    code="temporarily_failed",
                    retryable=True,
                ) from None

        if r.status_code != 200:
            raise backend_http_error(
                "POST",
                "/backend-api/sentinel/chat-requirements",
                r.status_code,
                fixed_probe=True,
            )

        _validate_json_size(r, body.content)
        decoded = False
        resp: object = None
        try:
            resp = json.loads(body.content)
        except Exception:
            pass
        else:
            decoded = True
        if not decoded:
            raise BackendContractError(
                "sentinel", "chat-requirements must return a JSON object"
            ) from None
        if not isinstance(resp, dict):
            raise BackendContractError(
                "sentinel", "chat-requirements must return an object"
            )
        chat_token = _bounded_visible_ascii(
            resp.get("token"), max_chars=_MAX_HEADER_TOKEN_CHARS
        )
        if chat_token is None:
            raise BackendContractError(
                "sentinel",
                "chat-requirements token must be a bounded header-safe string",
            )

        out: dict[str, str] = {"chat-requirements": chat_token}

        pow_block = _challenge_block(resp, "proofofwork")
        if _required(pow_block, "proofofwork"):
            seed, diff = _pow_inputs(pow_block)
            proof: object = None
            solver_failed = False
            try:
                proof = await asyncio.to_thread(_pow.solve_pow, seed, diff, ua)
            except Exception:
                solver_failed = True
            proof_token = _solver_token(proof)
            if solver_failed or proof_token is None:
                raise BackendContractError(
                    "sentinel", "required POW challenge could not be solved"
                ) from None
            out["proof"] = proof_token
        else:
            out["proof"] = ""

        turn_block = _challenge_block(resp, "turnstile")
        if _required(turn_block, "turnstile"):
            dx = _turnstile_input(turn_block)
            proof_for_xor = out.get("proof") or p
            tok: object = None
            solver_failed = False
            try:
                tok = await asyncio.to_thread(
                    _turn.solve_turnstile, dx, proof_for_xor
                )
            except Exception:
                solver_failed = True
            turnstile_token = _solver_token(tok)
            if solver_failed or turnstile_token is None:
                raise BackendContractError(
                    "sentinel", "required Turnstile challenge could not be solved"
                ) from None
            out["turnstile"] = turnstile_token

        return out
