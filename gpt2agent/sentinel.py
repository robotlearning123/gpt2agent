"""Sentinel gate: fetch chat-requirements, solve POW + turnstile."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from curl_cffi.requests import AsyncSession

from gpt2agent._log_redact import redact_error as _redact_error
from gpt2agent._vendored import pow as _pow
from gpt2agent._vendored import turnstile as _turn
from gpt2agent.backend import UpstreamChallengeError

if TYPE_CHECKING:
    from gpt2agent.backend import BackendClient

_log = logging.getLogger(__name__)

_CHAT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Appended to every "the challenge could not be solved" raise. The unsolved
# stage is named on the first line of the message; this explains what that means
# for the user. Kept short — the whole message lands in MCP client logs.
_UPSTREAM_CHALLENGE_NOTE = (
    "This is a change on ChatGPT's side, not a problem with your token or "
    "configuration — re-logging in will not fix it.\n"
    "Blocked tools: chat, agent, gpt_chat, deep_research, deep_research_heavy, "
    "generate_image, code_interpreter, canvas_execute, memory_create_via_chat.\n"
    "Read-only tools (list_models, list_conversations, memory_list, ...) are "
    "unaffected. Run `gpt2agent doctor` for a live status table."
)


class SentinelGate:
    def __init__(self, backend: "BackendClient") -> None:
        self._backend = backend

    async def get_tokens(self) -> dict[str, str]:
        headers = dict(self._backend._session.headers)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "*/*"

        ua = headers.get("User-Agent") or _CHAT_UA
        p = _pow.get_requirements_token(ua)

        url = "https://chatgpt.com/backend-api/sentinel/chat-requirements"

        async with AsyncSession(impersonate="chrome131", verify=True) as s:
            r = await s.post(url, headers=headers, json={"p": p}, timeout=20)

        if r.status_code != 200:
            body = r.text if hasattr(r, "text") else str(r.content)
            raise RuntimeError(
                f"sentinel/chat-requirements HTTP {r.status_code}: "
                f"{_redact_error(body)}"
            )

        try:
            resp = r.json()
        except Exception as exc:
            body = r.text if hasattr(r, "text") else str(r.content)
            raise RuntimeError(
                f"sentinel/chat-requirements non-JSON 200: {_redact_error(body)}"
            ) from exc
        if not isinstance(resp, dict):
            raise RuntimeError(
                "sentinel/chat-requirements unexpected response shape: "
                f"{_redact_error(json.dumps(resp, ensure_ascii=False))}"
            )
        chat_token = resp.get("token")
        if not chat_token:
            # Redact json.dumps(...) not str(dict): the latter uses single
            # quotes and would bypass the JSON-key redaction regexes.
            raise RuntimeError(
                "sentinel/chat-requirements no token: "
                f"{_redact_error(json.dumps(resp, ensure_ascii=False))}"
            )

        out: dict[str, str] = {"chat-requirements": chat_token}

        pow_block = resp.get("proofofwork") or {}
        if pow_block.get("required"):
            seed = pow_block.get("seed")
            diff = pow_block.get("difficulty")
            if not seed or not diff:
                raise RuntimeError(f"sentinel POW missing seed/difficulty: {pow_block}")
            proof = await asyncio.to_thread(_pow.solve_pow, seed, diff, ua)
            if not proof:
                raise UpstreamChallengeError(
                    "required POW challenge could not be solved.\n"
                    + _UPSTREAM_CHALLENGE_NOTE
                )
            out["proof"] = proof
        else:
            out["proof"] = ""

        turn_block = resp.get("turnstile") or {}
        if turn_block.get("required"):
            dx = turn_block.get("dx")
            if not dx:
                raise UpstreamChallengeError(
                    "required Turnstile challenge could not be solved "
                    "(chat-requirements demanded Turnstile but sent no challenge "
                    "payload).\n" + _UPSTREAM_CHALLENGE_NOTE
                )
            proof_for_xor = out.get("proof") or p
            tok = await asyncio.to_thread(
                _turn.solve_turnstile, dx, proof_for_xor
            )
            if not tok:
                raise UpstreamChallengeError(
                    "required Turnstile challenge could not be solved "
                    "(the vendored solver returned no token for the challenge "
                    "chatgpt.com sent).\n" + _UPSTREAM_CHALLENGE_NOTE
                )
            out["turnstile"] = tok

        return out
