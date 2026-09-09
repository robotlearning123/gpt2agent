"""Native SSE client for /backend-api/conversation — no proxy required."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from curl_cffi.requests import AsyncSession

from gpt2agent._log_redact import redact_error as _redact_error
from gpt2agent.backend import BackendClient, _BASE
from gpt2agent.sentinel import SentinelGate  # noqa: F401  (used in stream)

_log = logging.getLogger(__name__)

_CONV_URL = _BASE + "/backend-api/conversation"

_INCOMPLETE_RESPONSE_MESSAGE = (
    "ChatGPT stream ended before completion; partial output was discarded"
)


class _IncompleteStreamError(RuntimeError):
    def __init__(self, conversation_id: str | None = None) -> None:
        super().__init__(_INCOMPLETE_RESPONSE_MESSAGE)
        self.conversation_id = conversation_id


def _is_successful_assistant_terminal(message: dict) -> bool:
    recipient = message.get("recipient")
    content_type = (message.get("content") or {}).get("content_type")
    return (
        (message.get("author") or {}).get("role") == "assistant"
        and recipient in (None, "all")
        and content_type in ("text", "multimodal_text")
        and message.get("status") == "finished_successfully"
    )


def _safe_body(resp: object) -> str:
    try:
        text = getattr(resp, "text", "") or ""
    except Exception:
        return ""
    return _redact_error(text) if text else ""


def _raise_for_sse_error(obj: dict) -> None:
    """Surface in-band SSE error frames instead of silently dropping them."""
    raw: object = None
    err = obj.get("error")
    if isinstance(err, dict):
        raw = err.get("message") or err.get("detail") or err.get("code") or err
    elif isinstance(err, str):
        raw = err
    elif obj.get("type") in {"error", "conversation_error"}:
        raw = obj.get("message") or obj.get("detail") or obj.get("code") or obj
    if raw is None:
        return
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False)
    raise RuntimeError(f"ChatGPT SSE error: {_redact_error(raw, max_len=500)}")


# /backend-api/f/conversation — the frontend-facing endpoint used by the web app.
# Required for Deep Research heavy path; regular /conversation also works for normal chat.
_F_CONV_URL = _BASE + "/backend-api/f/conversation"

#: Model slug for legacy Deep Research (resolves to i-mini-m / web-search backend)
DR_MODEL = "research"

#: Model slug for heavy Deep Research — gpt-6-pro with extended thinking + DR connector
HEAVY_DR_MODEL = "gpt-6-pro"

#: System hint for heavy Deep Research (connector identifier from chatgpt.com frontend)
HEAVY_DR_HINT = "connector:connector_openai_deep_research"


def _raw_dump(obj: dict, *, phase: str) -> None:
    path = os.environ.get("GPT2AGENT_RAW_DUMP")
    if not path:
        return
    record = {"phase": phase, "obj": obj}
    try:
        out = Path(path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except (OSError, AttributeError):
            pass
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    except Exception as exc:
        _log.warning("GPT2AGENT_RAW_DUMP write failed (%s)", exc)


def _has_citation_payload(meta: dict | None) -> bool:
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("content_references") or meta.get("search_result_groups"))


def _citation_payload(*metas: dict | None) -> tuple[list, list]:
    refs: list = []
    groups: list = []
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        if not refs:
            refs = meta.get("content_references") or []
        if not groups:
            groups = meta.get("search_result_groups") or []
    return refs, groups


#: Prefix a tool node uses when the connector widget state is replayed into the
#: conversation transcript as plain text.
_WIDGET_STATE_TEXT_PREFIX = "The latest state of the widget is: "
_DR_APP_RESOURCE = "Deep Research App_start"
_DR_CONNECTOR_ID = "connector_openai_deep_research"
_DR_CONNECTOR_URI = f"connectors://{_DR_CONNECTOR_ID}"
_DR_RESOURCE_URI = f"/{_DR_CONNECTOR_ID}/implicit_link::{_DR_CONNECTOR_ID}/start"
_DR_WIDGET_EXCLUSIVE_KEY = f"widget_state:{_DR_APP_RESOURCE}"


def _coerce_widget_state(obj: object) -> dict | None:
    """Return the widget-state dict from a dict or a JSON-string carrier.

    The Deep Research App connector exposes the widget state two ways: as a
    ``"The latest state of the widget is: {…}"`` text part (tool node) and as a
    JSON string under ``message.metadata.chatgpt_sdk.widget_state`` (returned
    when the conversation is fetched with ``include_widget_state=true``). Both
    decode to the same object; some carriers wrap it as ``{"widget_state": {…}}``.
    """
    if isinstance(obj, str):
        brace = obj.find("{")
        if brace < 0:
            return None
        try:
            obj = json.loads(obj[brace:])
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(obj, dict):
        return None
    if "report_message" in obj:
        return obj
    inner = obj.get("widget_state")
    return inner if isinstance(inner, dict) else None


def _dr_report_from_widget_state(detail: dict | None) -> tuple[str, list]:
    """Recover the Deep Research report from a connector widget-state node.

    The "Deep Research App" connector (pineapple URI
    ``connectors://connector_openai_deep_research``) never writes its final
    report as an assistant text node in the conversation ``mapping``; the report
    text lives in ``widget_state.report_message`` and renders client-side. This
    walks the mapping for either widget-state carrier (see
    :func:`_coerce_widget_state`) and returns ``(report_text, content_references)``
    for the longest *completed* report found, or ``("", [])`` if none is present.

    Hardening (audits 2026-06-18 and 2026-07-10):

    * Only ``tool`` nodes with the observed Deep Research-specific backend
      envelope are accepted. Arbitrary assistant, user, or tool content with a
      widget-shaped payload is ignored. These envelope checks are fail-closed
      provenance validation, not cryptographic authentication; the payload itself
      is unsigned and can still contain incorrect or prompt-injected research.
    * The text carrier must *start with* the prefix, not merely contain it.
    * An in-progress draft is ignored: both the top-level widget status and the
      nested report status must carry their explicit completed values, so polling
      never emits a half-written report as the final answer.
    """
    mapping = (detail or {}).get("mapping") or {}
    if not isinstance(mapping, dict):
        return "", []
    best_text = ""
    best_refs: list = []
    for node in mapping.values():
        msg = (node or {}).get("message")
        if not isinstance(msg, dict):
            continue
        author = msg.get("author") or {}
        metadata = msg.get("metadata") or {}
        content = msg.get("content") or {}
        if (
            not isinstance(author, dict)
            or not isinstance(metadata, dict)
            or not isinstance(content, dict)
            or author.get("role") != "tool"
            or msg.get("recipient") != "all"
            or msg.get("status") != "finished_successfully"
        ):
            continue
        carriers: list[object] = []
        parts = content.get("parts") or []
        if (
            author.get("name") == "api_tool.widget_state"
            and content.get("content_type") == "text"
            and metadata.get("exclusive_key") == _DR_WIDGET_EXCLUSIVE_KEY
            and metadata.get("is_visually_hidden_from_conversation") is True
            and parts
            and isinstance(parts[0], str)
            and parts[0].startswith(_WIDGET_STATE_TEXT_PREFIX)
        ):
            carriers.append(parts[0])
        sdk = metadata.get("chatgpt_sdk")
        invoked_resource = metadata.get("invoked_resource")
        if (
            author.get("name") == "api_tool.call_tool"
            and content.get("content_type") == "code"
            and isinstance(sdk, dict)
            and sdk.get("resource_name") == _DR_APP_RESOURCE
            and sdk.get("attribution_id") == _DR_CONNECTOR_ID
            and sdk.get("resolved_pineapple_uri") == _DR_CONNECTOR_URI
            and sdk.get("distribution_channel") == "openai"
            and sdk.get("connector_type") == "FIRST_PARTY_ECOSYSTEM"
            and isinstance(invoked_resource, dict)
            and invoked_resource.get("resource_uri") == _DR_RESOURCE_URI
            and sdk.get("widget_state") is not None
        ):
            carriers.append(sdk["widget_state"])
        for carrier in carriers:
            state = _coerce_widget_state(carrier)
            report = (state or {}).get("report_message")
            if not isinstance(report, dict):
                continue
            # Only emit the exact completed shape observed from the connector.
            if (
                (state or {}).get("status") != "completed"
                or report.get("status") != "finished_successfully"
            ):
                continue
            report_content = report.get("content") or {}
            if (
                not isinstance(report_content, dict)
                or report_content.get("content_type") != "text"
            ):
                continue
            rparts = report_content.get("parts") or []
            text = rparts[0] if rparts and isinstance(rparts[0], str) else ""
            if text and len(text) > len(best_text):
                best_text = text
                refs = (report.get("metadata") or {}).get("content_references") or []
                best_refs = refs if isinstance(refs, list) else []
    return best_text, best_refs


def _pointer_parts(path: str, prefix: str) -> list[str]:
    tail = path[len(prefix) :].strip("/")
    if not tail:
        return []
    return [p.replace("~1", "/").replace("~0", "~") for p in tail.split("/")]


def _new_container(next_part: str) -> list | dict:
    return [] if next_part == "-" or next_part.isdigit() else {}


def _ensure_list_slot(seq: list, part: str, value_factory):
    if part == "-":
        seq.append(value_factory())
        return seq[-1]
    if not part.isdigit():
        return None
    idx = int(part)
    while len(seq) <= idx:
        seq.append(None)
    if not isinstance(seq[idx], (dict, list)):
        seq[idx] = value_factory()
    return seq[idx]


def _merge_metadata_path(meta: dict, path: str, op: str, value) -> dict:
    if path == "/message/metadata":
        if op in ("append", "patch") and isinstance(value, dict):
            return {**meta, **value}
        if op == "replace" and isinstance(value, dict):
            return value
        return meta

    if not path.startswith("/message/metadata/"):
        return meta

    out = dict(meta)
    parts = _pointer_parts(path, "/message/metadata")
    if not parts:
        return out

    cur: dict | list = out
    for i, part in enumerate(parts[:-1]):
        next_part = parts[i + 1]
        if isinstance(cur, dict):
            nxt = cur.get(part)
            if not isinstance(nxt, (dict, list)):
                nxt = _new_container(next_part)
                cur[part] = nxt
            cur = nxt
        elif isinstance(cur, list):
            nxt = _ensure_list_slot(cur, part, lambda: _new_container(next_part))
            if nxt is None:
                return out
            cur = nxt

    key = parts[-1]
    if isinstance(cur, dict):
        if op == "append":
            existing = cur.get(key)
            if isinstance(existing, list):
                cur[key] = [*existing, value]
            elif isinstance(existing, str) and isinstance(value, str):
                cur[key] = existing + value
            else:
                cur[key] = value
        elif op == "patch" and isinstance(value, dict) and isinstance(cur.get(key), dict):
            cur[key] = {**cur[key], **value}
        else:
            cur[key] = value
    elif isinstance(cur, list):
        if key == "-" or op == "append":
            cur.append(value)
        elif key.isdigit():
            idx = int(key)
            while len(cur) <= idx:
                cur.append(None)
            cur[idx] = value
    return out


def _is_connector_dispatch_text(text: str) -> bool:
    return text.startswith('{"path":') and "connector_openai_deep_research" in text


def _build_payload(
    model: str,
    messages: list[dict],
    *,
    gizmo_id: str | None = None,
    temporary: bool = True,
) -> dict:
    payload: dict = {
        "action": "next",
        "messages": [
            {
                "id": str(uuid4()),
                "author": {"role": m["role"]},
                "content": {"content_type": "text", "parts": [m["content"]]},
            }
            for m in messages
        ],
        "parent_message_id": str(uuid4()),
        "model": model,
        "conversation_mode": {"kind": "primary_assistant"},
        "force_paragen": False,
        "force_rate_limit": False,
        "force_use_sse": True,
        "timezone_offset_min": -480,
        "history_and_training_disabled": temporary,
        "system_hints": [],
    }
    if gizmo_id:
        payload["gizmo_id"] = gizmo_id
        payload["conversation_origin"] = {"type": "custom_gpt", "gizmo_id": gizmo_id}
    return payload


def _build_dr_payload(
    query: str,
    *,
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
) -> dict:
    """Build payload for legacy Deep Research: model=research + system_hints=['research'].

    This resolves to i-mini-m (web-search/SearchGPT backend), NOT the Pro-tier
    multi-section deep research.  Use _build_heavy_dr_payload() for the full DR.

    NOTE: history_and_training_disabled must be False here. ChatGPT refuses
    Deep Research in "temporary chats" (the True setting), returning
    "Research is not currently supported in temporary chats". DR requires a
    persistent conversation so the connector can poll for the final report.

    When ``conversation_id`` + ``parent_message_id`` are supplied, the payload
    continues an existing conversation — used by multi-turn clarification
    handling in ``ConversationClient.deep_research``.
    """
    payload = _build_payload(DR_MODEL, [{"role": "user", "content": query}])
    payload["system_hints"] = ["research"]
    payload["history_and_training_disabled"] = False
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if parent_message_id:
        payload["parent_message_id"] = parent_message_id
    return payload


_CLARIFICATION_HINTS = (
    "could you confirm",
    "could you clarify",
    "could you tell me",
    "would you like",
    "do you want me",
    "shall i proceed",
    "before i start",
    "before i begin",
    "to make sure",
    "to ensure i",
    "i'd like to clarif",
    "i'd like to confirm",
    "can you specify",
    "just one key clarif",
    "one quick clarif",
    "one clarif",
    "a quick question",
    "i have one question",
    "i need to confirm",
)

_DR_AUTO_PROCEED = (
    "Proceed with your best interpretation of any ambiguity. "
    "Do not ask further clarifying questions. Begin the research now."
)

# A genuine clarification request is a question or a short list of questions —
# a few sentences. Anything longer is a report, even if its prose happens to
# contain a hint phrase ("to make sure the comparison is fair, …").
_CLARIFICATION_MAX_LEN = 1200


def _looks_like_clarification(text: str) -> bool:
    """Heuristic: does this assistant 'done' text look like a clarification request?

    Matches a curated phrase list ("could you confirm", "before I start", "just
    one key clarification", "shall I proceed", etc.), but only on short text
    (≤ _CLARIFICATION_MAX_LEN chars). Without the length ceiling, a real
    multi-thousand-word report containing an ordinary phrase like "to make
    sure" anywhere in its body trips the substring match — the wrapper then
    burns a DR round on _DR_AUTO_PROCEED and overwrites the real report with
    the follow-up response. The earlier "short text ending in ?" branch stays
    removed — real reports often end with rhetorical questions. The heuristic
    is conservative; if a clarification slips through, the caller still gets
    the question text and can re-invoke explicitly.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > _CLARIFICATION_MAX_LEN:
        return False
    lower = stripped.lower()
    return any(p in lower for p in _CLARIFICATION_HINTS)


def _build_heavy_dr_payload(query: str, *, model: str | None = None) -> dict:
    """Build payload for heavy Deep Research — the true Pro-tier 5–30 min DR path.

    Ground-truth reverse-engineered from chatgpt.com/deep-research browser traffic
    (2026-04-23).  Key differences from legacy DR:

    * URL target: /backend-api/f/conversation  (frontend endpoint)
    * model: gpt-6-pro
    * system_hints: ["connector:connector_openai_deep_research"]
    * thinking_effort: "extended"
    * message.metadata contains deep_research_version / venus_model_variant / caterpillar fields

    The server_ste_metadata from the SSE stream will show tool_name="ApiToolWrapper"
    and tool_invoked=true, confirming the DR connector fired.  The resolved_model_slug
    in the user-message echo is "i-mini-m" (the orchestration layer); the actual heavy
    reasoning runs as a background tool call inside the connector.

    Rate-limited by the account-reported "deep_research" feature quota; limits
    and reset timing can vary.
    """
    msg_id = str(uuid4())
    return {
        "action": "next",
        "messages": [
            {
                "id": msg_id,
                "author": {"role": "user"},
                "create_time": time.time(),
                "content": {"content_type": "text", "parts": [query]},
                "metadata": {
                    "caterpillar_selected_sources": [],
                    "developer_mode_connector_ids": [],
                    "selected_mcp_sources": [],
                    "selected_sources": [],
                    "selected_github_repos": [],
                    "selected_all_github_repos": False,
                    "system_hints": [HEAVY_DR_HINT],
                    "deep_research_version": "standard",
                    "venus_model_variant": "standard",
                    "serialization_metadata": {"custom_symbol_offsets": []},
                    "user_timezone": "UTC",
                },
            }
        ],
        "parent_message_id": str(uuid4()),
        "model": model or HEAVY_DR_MODEL,
        "client_prepare_state": "success",
        "timezone_offset_min": -480,
        "timezone": "UTC",
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_followups": True,
        "system_hints": [HEAVY_DR_HINT],
        "thinking_effort": "extended",
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "force_parallel_switch": "auto",
        "paragen_cot_summary_display_override": "allow",
        # MUST be False — Deep Research is rejected by the server in
        # "temporary chats" ("Research is not currently supported in temporary chats").
        # DR also depends on the conversation persisting so Phase 2 polling
        # at /backend-api/conversation/{id} can fetch the final report.
        "history_and_training_disabled": False,
        "force_use_sse": True,
    }


class ConversationClient:
    def __init__(self, backend: BackendClient) -> None:
        self._backend = backend

    async def stream(
        self,
        model: str,
        messages: list[dict],
        tools: list | None = None,
        *,
        gizmo_id: str | None = None,
        temporary: bool = True,
    ) -> AsyncIterator[str | dict]:
        # Yields text chunks (str). As the final item it may yield a single
        # ``{"_conversation_id": ...}`` dict sentinel for ``complete()`` to detect
        # agent-mode async runs — callers that join chunks must skip non-str items.
        self._backend._reload_token_if_stale()
        headers = dict(self._backend._session.headers)
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"

        sentinel = await SentinelGate(self._backend).get_tokens()
        headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel[
            "chat-requirements"
        ]
        if sentinel.get("proof"):
            headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
        if sentinel.get("turnstile"):
            headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]

        payload = _build_payload(model, messages, gizmo_id=gizmo_id, temporary=temporary)
        if tools:
            payload["tools"] = tools

        async with AsyncSession(impersonate="chrome131", verify=True) as s:
            resp = await s.post(
                _CONV_URL,
                headers=headers,
                json=payload,
                timeout=300,
                stream=True,
            )
            if resp.status_code == 401:
                body = _safe_body(resp)
                raise RuntimeError(
                    "401 Unauthorized — run `codex login`"
                    + (f": {body}" if body else "")
                )
            if resp.status_code == 403:
                body = _safe_body(resp)
                raise RuntimeError(
                    "403 Forbidden — token may have expired"
                    + (f": {body}" if body else "")
                )
            if resp.status_code not in (200, 201):
                body = _safe_body(resp)
                raise RuntimeError(
                    f"HTTP {resp.status_code} from /backend-api/conversation"
                    + (f": {body}" if body else "")
                )

            current_msg_id: str | None = None
            last_text = ""
            _conversation_id: str | None = None
            done_received = False
            message_completed = False

            def _reset_if_new_msg(msg_id: str | None) -> bool:
                """Return True if this frame starts a new message (caller must not dedupe)."""
                nonlocal current_msg_id, last_text
                if msg_id and msg_id != current_msg_id:
                    current_msg_id = msg_id
                    last_text = ""
                    return True
                return False

            def _track_message_lifecycle(message: dict) -> None:
                nonlocal message_completed
                role = (message.get("author") or {}).get("role")
                if role in ("assistant", "tool"):
                    message_completed = _is_successful_assistant_terminal(message)

            async for raw_line in resp.aiter_lines():
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8", errors="replace")
                line = raw_line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done_received = True
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                _raise_for_sse_error(obj)

                # Capture conversation_id from any event that carries it
                cid = obj.get("conversation_id")
                if cid and not _conversation_id:
                    _conversation_id = cid

                # Format A: v-patch (live streaming mode)
                v = obj.get("v")
                if v is not None:
                    if isinstance(v, str):
                        # String v-patch — continuation of current message id
                        if v:
                            message_completed = False
                            yield v
                            last_text += v
                        continue
                    if isinstance(v, dict):
                        # `{"v":{"message":null}}` makes .get("message", {}) return
                        # None (key present), so chained .get() would AttributeError.
                        vmsg = v.get("message") or {}
                        _track_message_lifecycle(vmsg)
                        msg_id = vmsg.get("id")
                        is_new = _reset_if_new_msg(msg_id)
                        parts = (vmsg.get("content") or {}).get("parts") or []
                        if parts and isinstance(parts[0], str):
                            new = parts[0]
                            if is_new:
                                # New message — yield fresh, don't dedupe against prior stream
                                if new:
                                    yield new
                                    last_text = new
                            elif new.startswith(last_text):
                                delta = new[len(last_text) :]
                                if delta:
                                    yield delta
                                last_text = new
                            elif new:
                                yield new
                                last_text = new
                        continue

                # Format B: full message replacement (history_disabled mode)
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                _track_message_lifecycle(msg)
                if msg.get("author", {}).get("role") != "assistant":
                    continue
                content = msg.get("content") or {}
                ct = content.get("content_type")
                if ct not in ("text", "multimodal_text"):
                    continue
                parts = content.get("parts") or []
                if not parts or not isinstance(parts[0], str):
                    continue
                msg_id = msg.get("id")
                is_new = _reset_if_new_msg(msg_id)
                new = parts[0]
                if is_new:
                    if new:
                        yield new
                        last_text = new
                elif new.startswith(last_text):
                    delta = new[len(last_text) :]
                    if delta:
                        yield delta
                    last_text = new
                elif new:
                    yield new
                    last_text = new

            if not (done_received or message_completed):
                raise _IncompleteStreamError(_conversation_id)

            # Emit conversation_id for complete() to use (agent mode async detection)
            if _conversation_id:
                yield {"_conversation_id": _conversation_id}

    async def complete(
        self,
        model: str,
        messages: list[dict],
        *,
        gizmo_id: str | None = None,
        temporary: bool = True,
        poll_async: bool = False,
    ) -> str:
        chunks: list[str] = []
        conv_id: str | None = None
        try:
            async for event in self.stream(
                model, messages, gizmo_id=gizmo_id, temporary=temporary
            ):
                if isinstance(event, dict):
                    if event.get("_conversation_id"):
                        conv_id = event["_conversation_id"]
                    continue  # never let a non-str sentinel reach "".join(chunks)
                chunks.append(event)
        except _IncompleteStreamError as exc:
            if poll_async and exc.conversation_id:
                recovered = await self._poll_async_response(exc.conversation_id)
                if recovered:
                    return recovered
            raise
        text = "".join(chunks)

        # Agent mode: the stream ends immediately with async_status and the real
        # response arrives later, so we poll the conversation for up to 5 min.
        # This MUST be opt-in (poll_async): conv_id is captured on nearly every
        # stream, so an unconditional "not text and conv_id" poll would make an
        # ordinary chat that returns empty text hang for the full poll window.
        if poll_async and not text and conv_id:
            text = await self._poll_async_response(conv_id)

        return text

    async def _poll_async_response(
        self,
        conversation_id: str,
        poll_interval: float = 3.0,
        max_wait: float = 300.0,
    ) -> str:
        """Poll for async agent-mode response after SSE stream ends with async_status."""
        detail_path = f"/backend-api/conversation/{conversation_id}"
        deadline = time.monotonic() + max_wait
        poll_errors = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                det = await asyncio.to_thread(self._backend.get, detail_path)
            except Exception as exc:
                poll_errors += 1
                if poll_errors >= 5:
                    raise RuntimeError(
                        f"Agent poll: {poll_errors} consecutive errors, giving up"
                    ) from exc
                _log.warning("Agent poll error (%s) — continuing", exc)
                continue

            poll_errors = 0

            mapping = (det or {}).get("mapping") or {}
            best_text = ""
            best_time = 0
            for node in mapping.values():
                msg = (node or {}).get("message")
                if not isinstance(msg, dict):
                    continue
                role = (msg.get("author") or {}).get("role", "")
                if role != "assistant":
                    continue
                content = msg.get("content") or {}
                ct = content.get("content_type", "")
                if ct not in ("text", "multimodal_text"):
                    continue
                parts = content.get("parts") or []
                str_parts = [p for p in parts if isinstance(p, str)]
                if str_parts and msg.get("status") == "finished_successfully":
                    t = msg.get("create_time") or 0
                    if t > best_time:
                        best_time = t
                        best_text = str_parts[0]

            if best_text:
                return best_text

        _log.warning("Agent poll timed out after %ss for %s", max_wait, conversation_id)
        return ""
    async def image_gen(
        self,
        prompt: str,
        *,
        model: str = "gpt-5-6",
        poll_interval: float = 5.0,
        max_wait: float = 300.0,
    ) -> dict:
        """Generate an image via ChatGPT's built-in image generation tool.

        Sends the prompt through a non-temporary conversation (required for
        image gen). The server auto-invokes the image tool, then processes
        the image asynchronously. This method polls until the image is ready.

        Returns dict with keys:
          conversation_id, assets (list of {asset_pointer, width, height,
          size_bytes, download_url, file_name, file_id}), metadata

        Raises RuntimeError if image gen fails or times out.
        """
        self._backend._reload_token_if_stale()
        headers = dict(self._backend._session.headers)
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"

        sentinel = await SentinelGate(self._backend).get_tokens()
        headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel[
            "chat-requirements"
        ]
        if sentinel.get("proof"):
            headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
        if sentinel.get("turnstile"):
            headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]

        payload = _build_payload(
            model, [{"role": "user", "content": prompt}], temporary=False
        )

        conversation_id: str | None = None
        processing_text = ""

        async with AsyncSession(impersonate="chrome131", verify=True) as s:
            resp = await s.post(
                _CONV_URL, headers=headers, json=payload, timeout=300, stream=True,
            )
            if resp.status_code not in (200, 201):
                body = _safe_body(resp)
                raise RuntimeError(
                    f"HTTP {resp.status_code} from image gen" + (f": {body}" if body else "")
                )

            async for raw_line in resp.aiter_lines():
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8", errors="replace")
                line = raw_line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                _raise_for_sse_error(obj)

                cid = obj.get("conversation_id")
                if cid and not conversation_id:
                    conversation_id = cid

                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue
                role = msg.get("author", {}).get("role", "")
                ct = (msg.get("content") or {}).get("content_type", "")
                parts = (msg.get("content") or {}).get("parts", [])

                if role == "tool" and ct == "multimodal_text":
                    result = self._extract_image_result(conversation_id, msg)
                    if result.get("assets"):
                        return result

                if role == "tool" and ct == "text" and parts and isinstance(parts[0], str):
                    processing_text = parts[0]

        # Image is async — poll the conversation until multimodal_text arrives
        if not conversation_id:
            raise RuntimeError("Image gen: no conversation_id returned")

        return await self._poll_image_result(
            conversation_id,
            poll_interval=poll_interval,
            max_wait=max_wait,
            processing_text=processing_text,
        )

    def _extract_image_result(self, conversation_id: str | None, msg: dict) -> dict:
        """Extract image assets from a multimodal_text tool response."""
        parts = (msg.get("content") or {}).get("parts", [])
        meta = msg.get("metadata") or {}
        assets = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if p.get("content_type") != "image_asset_pointer":
                continue
            asset_pointer = p.get("asset_pointer", "")
            file_id = asset_pointer.removeprefix("sediment://") if asset_pointer else ""
            assets.append({
                "asset_pointer": asset_pointer,
                "file_id": file_id,
                "width": p.get("width"),
                "height": p.get("height"),
                "size_bytes": p.get("size_bytes"),
                "metadata": p.get("metadata"),
            })

        return {
            "conversation_id": conversation_id,
            "assets": assets,
            "metadata": meta,
        }

    async def _poll_image_result(
        self,
        conversation_id: str,
        *,
        poll_interval: float = 5.0,
        max_wait: float = 300.0,
        processing_text: str = "",
    ) -> dict:
        """Poll conversation until multimodal_text with image assets arrives."""
        detail_path = f"/backend-api/conversation/{conversation_id}"
        deadline = time.monotonic() + max_wait
        poll_errors = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                det = await asyncio.to_thread(self._backend.get, detail_path)
                poll_errors = 0  # reset on success
            except Exception as exc:
                poll_errors += 1
                if poll_errors >= 5:
                    raise RuntimeError(
                        f"Image poll failed {poll_errors} times in a row: {exc}"
                    ) from exc
                backoff = min(poll_interval * (2 ** poll_errors), 30)
                _log.warning("Image poll error (%s) — retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                continue

            mapping = (det or {}).get("mapping") or {}
            candidates = []
            for node in mapping.values():
                msg = (node or {}).get("message")
                if not isinstance(msg, dict):
                    continue
                role = (msg.get("author") or {}).get("role", "")
                ct = (msg.get("content") or {}).get("content_type", "")
                if role == "tool" and ct == "multimodal_text":
                    result = self._extract_image_result(conversation_id, msg)
                    if result.get("assets"):
                        candidates.append((msg.get("create_time") or 0, result))
            if candidates:
                candidates.sort(key=lambda c: c[0])
                return candidates[-1][1]

        raise RuntimeError(
            f"Image gen timed out after {max_wait}s. "
            f"Last status: {processing_text[:200]}"
        )

    async def tool_call(
        self,
        prompt: str,
        *,
        model: str = "gpt-5-6",
        temporary: bool = False,
        poll_interval: float = 5.0,
        max_wait: float = 300.0,
    ) -> dict:
        """Send a prompt that triggers a tool-based feature (code interpreter,
        canvas, image gen) and return the structured result.

        Returns dict with keys:
          conversation_id, text (assistant text response),
          tool_calls (list of {recipient, content_type, parts}),
          tool_responses (list of {content_type, parts}),
          multimodal_assets (list of image asset dicts if any)
        """
        self._backend._reload_token_if_stale()
        headers = dict(self._backend._session.headers)
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"

        sentinel = await SentinelGate(self._backend).get_tokens()
        headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel[
            "chat-requirements"
        ]
        if sentinel.get("proof"):
            headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
        if sentinel.get("turnstile"):
            headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]

        payload = _build_payload(
            model, [{"role": "user", "content": prompt}], temporary=temporary
        )

        conversation_id: str | None = None
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_responses: list[dict] = []
        multimodal_assets: list[dict] = []
        done_received = False
        message_completed = False

        async with AsyncSession(impersonate="chrome131", verify=True) as s:
            resp = await s.post(
                _CONV_URL, headers=headers, json=payload, timeout=300, stream=True,
            )
            if resp.status_code not in (200, 201):
                body = _safe_body(resp)
                raise RuntimeError(
                    f"HTTP {resp.status_code} from tool_call" + (f": {body}" if body else "")
                )

            last_text = ""
            async for raw_line in resp.aiter_lines():
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8", errors="replace")
                line = raw_line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done_received = True
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                _raise_for_sse_error(obj)

                cid = obj.get("conversation_id")
                if cid and not conversation_id:
                    conversation_id = cid

                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue
                role = msg.get("author", {}).get("role", "")
                recipient = msg.get("recipient", "all")
                content = msg.get("content") or {}
                ct = content.get("content_type", "")
                parts = content.get("parts") or []
                status = msg.get("status", "")
                # A newer assistant/tool lifecycle supersedes any earlier
                # message-level terminal status. [DONE] remains independent.
                if role in ("assistant", "tool"):
                    message_completed = False

                # Assistant text (to "all")
                if role == "assistant" and recipient == "all" and ct in ("text", "multimodal_text"):
                    str_parts = [p for p in parts if isinstance(p, str)]
                    message_completed = status == "finished_successfully"
                    if str_parts and status == "finished_successfully":
                        last_text = str_parts[0]
                    elif str_parts:
                        new = str_parts[0]
                        if new.startswith(last_text):
                            delta = new[len(last_text):]
                            if delta:
                                text_parts.append(delta)
                        else:
                            text_parts.append(new)
                        last_text = new

                # Tool call (assistant to non-all recipient)
                elif role == "assistant" and recipient != "all":
                    call_parts = []
                    for p in parts:
                        if isinstance(p, str) and p:
                            call_parts.append(p)
                        elif isinstance(p, dict):
                            call_parts.append(json.dumps(p, ensure_ascii=False))
                    tool_calls.append({
                        "recipient": recipient,
                        "content_type": ct,
                        "parts": call_parts,
                    })

                # Tool response
                elif role == "tool" and recipient == "all":
                    message_completed = status == "finished_successfully"
                    resp_parts = []
                    img_assets = []
                    for p in parts:
                        if isinstance(p, str):
                            resp_parts.append(p)
                        elif isinstance(p, dict):
                            if p.get("content_type") == "image_asset_pointer":
                                asset_pointer = p.get("asset_pointer", "")
                                file_id = asset_pointer.replace("sediment://", "")
                                img_assets.append({
                                    "asset_pointer": asset_pointer,
                                    "file_id": file_id,
                                    "width": p.get("width"),
                                    "height": p.get("height"),
                                    "size_bytes": p.get("size_bytes"),
                                })
                            else:
                                resp_parts.append(json.dumps(p, ensure_ascii=False))
                    tool_responses.append({
                        "content_type": ct,
                        "parts": resp_parts,
                    })
                    multimodal_assets.extend(img_assets)

        if not (done_received or message_completed):
            raise RuntimeError(_INCOMPLETE_RESPONSE_MESSAGE)
        final_text = last_text or "".join(text_parts)

        return {
            "conversation_id": conversation_id,
            "text": final_text,
            "tool_calls": tool_calls,
            "tool_responses": tool_responses,
            "multimodal_assets": multimodal_assets,
        }

    async def deep_research(
        self,
        query: str,
        *,
        max_clarification_rounds: int = 2,
    ) -> AsyncIterator[dict]:
        """Stream Deep Research events for *query*.

        Yields dicts of shape:
          {"type": "progress", "text": <partial_text>}   — intermediate text deltas
          {"type": "tool",     "call": <search_call>}    — tool invocations (search/browse)
          {"type": "done",     "text": <full_text>,
           "content_references": [...], "search_result_groups": [...]}
          {"type": "clarification_auto_reply", "round": N, "question": <text>}
              — emitted when the first turn was a clarification question and the
              wrapper auto-replied with a "proceed with best interpretation"
              follow-up. Real DR continues on the next round.

        Uses model='research' + system_hints=['research'] which triggers the
        ChatGPT web-search deep-research backend (confirmed working 2026-04-24).
        Timeout is 1800 s per round to accommodate multi-minute research runs.

        ChatGPT's research mode often opens with a clarifying question instead
        of starting research immediately. ``max_clarification_rounds`` caps how
        many auto-replies the wrapper sends before giving up (default 2).
        """
        conversation_id: str | None = None
        last_assistant_msg_id: str | None = None
        current_query = query

        for round_num in range(max_clarification_rounds + 1):
            # Re-read codex token + fetch fresh sentinel each round. The Bearer
            # in _session.headers may have been refreshed by a concurrent
            # backend.get/post; the sentinel is single-use and short-lived,
            # so reusing the round-1 sentinel for a later auto-proceed POST
            # silently 403s ("token may have expired").
            self._backend._reload_token_if_stale()
            headers = dict(self._backend._session.headers)
            headers["Accept"] = "text/event-stream"
            headers["Content-Type"] = "application/json"
            sentinel = await SentinelGate(self._backend).get_tokens()
            headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel[
                "chat-requirements"
            ]
            if sentinel.get("proof"):
                headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
            if sentinel.get("turnstile"):
                headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]

            payload = _build_dr_payload(
                current_query,
                conversation_id=conversation_id,
                parent_message_id=last_assistant_msg_id,
            )

            async with AsyncSession(impersonate="chrome131", verify=True) as s:
                resp = await s.post(
                    _CONV_URL,
                    headers=headers,
                    json=payload,
                    timeout=1800,
                    stream=True,
                )
                if resp.status_code == 401:
                    raise RuntimeError("401 Unauthorized — run `codex login`")
                if resp.status_code == 403:
                    raise RuntimeError("403 Forbidden — token may have expired")
                if resp.status_code not in (200, 201):
                    body = ""
                    async for chunk in resp.aiter_content():
                        body += (
                            chunk.decode("utf-8", errors="replace")
                            if isinstance(chunk, bytes)
                            else chunk
                        )
                        if len(body) > 500:
                            break
                    raise RuntimeError(
                        f"HTTP {resp.status_code} from /backend-api/conversation: "
                        f"{_redact_error(body, max_len=500)}"
                    )

                last_text = ""
                done_text = ""
                round_completed_successfully = False
                stream_succeeded = False
                try:
                    async for raw_line in resp.aiter_lines():
                        if isinstance(raw_line, bytes):
                            raw_line = raw_line.decode("utf-8", errors="replace")
                        line = raw_line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(obj, dict):
                            continue
                        _raise_for_sse_error(obj)

                        # Capture conversation_id for multi-turn continuation.
                        cid = obj.get("conversation_id")
                        if cid and not conversation_id:
                            conversation_id = cid

                        msg = obj.get("message", {})
                        if not isinstance(msg, dict):
                            continue

                        role = msg.get("author", {}).get("role", "")
                        content = msg.get("content", {})
                        ct = content.get("content_type", "")
                        status = msg.get("status", "")
                        meta = msg.get("metadata", {})
                        recipient = msg.get("recipient")

                        # Capture latest assistant message id so the next turn
                        # (auto-proceed reply) can use it as parent_message_id.
                        msg_id = msg.get("id")
                        if msg_id and role == "assistant":
                            last_assistant_msg_id = msg_id

                        # Completion belongs to the latest relevant lifecycle,
                        # not to any earlier clean `done`. A later tool response
                        # proves the round continued and invalidates that candidate.
                        if role == "tool":
                            round_completed_successfully = False
                            done_text = ""
                            last_text = ""

                        # Tool invocation events (search/browse). Assistant
                        # messages addressed to a tool are dispatch envelopes,
                        # even when the backend represents them as plain text.
                        if role == "assistant" and (
                            ct == "code" or recipient not in (None, "all")
                        ):
                            round_completed_successfully = False
                            done_text = ""
                            last_text = ""
                            parts = content.get("parts") or []
                            call_text = content.get("text", "") or (
                                parts[0]
                                if parts and isinstance(parts[0], str)
                                else ""
                            )
                            if call_text:
                                yield {"type": "tool", "call": call_text}
                            continue

                        # Text streaming — assistant in-progress or finished
                        if role == "assistant" and ct == "text":
                            parts = content.get("parts") or []
                            new = (
                                parts[0]
                                if parts and isinstance(parts[0], str)
                                else ""
                            )

                            if status == "finished_successfully":
                                yield {
                                    "type": "done",
                                    "text": new,
                                    "content_references": meta.get(
                                        "content_references", []
                                    ),
                                    "search_result_groups": meta.get(
                                        "search_result_groups", []
                                    ),
                                }
                                round_completed_successfully = True
                                last_text = new
                                done_text = new
                            else:
                                # Even an empty newer in-progress snapshot
                                # supersedes an earlier completed candidate.
                                round_completed_successfully = False
                                done_text = ""
                                if status != "in_progress":
                                    last_text = new
                                    continue
                            if status == "in_progress" and new:
                                # Emit incremental text delta
                                if new.startswith(last_text):
                                    delta = new[len(last_text) :]
                                    if delta:
                                        yield {"type": "progress", "text": delta}
                                else:
                                    yield {"type": "progress", "text": new}
                                last_text = new
                            elif status == "in_progress":
                                last_text = ""
                    stream_succeeded = True
                finally:
                    # Emit a synthetic abnormal terminal whenever normal EOF
                    # leaves the latest relevant lifecycle incomplete, even if
                    # an older candidate had once reached finished_successfully.
                    # On exception, propagate without faking a done event,
                    # so the caller doesn't mistake partial output for a
                    # complete answer (cf. code-review medium #2).
                    if stream_succeeded and not round_completed_successfully:
                        terminal = {
                            "type": "done",
                            "text": last_text,
                            "content_references": [],
                            "search_result_groups": [],
                            "terminated_abnormally": True,
                        }
                        if round_num > 0:
                            terminal["clarification_followup_incomplete"] = True
                        yield terminal
                        done_text = last_text

            # End of one round — decide whether to continue.
            if not round_completed_successfully:
                return

            # If the model asked for clarification AND we have a captured
            # conversation_id (so a follow-up can land in the same thread),
            # auto-reply and keep going.
            if (
                conversation_id
                and last_assistant_msg_id
                and _looks_like_clarification(done_text)
                and round_num < max_clarification_rounds
            ):
                yield {
                    "type": "clarification_auto_reply",
                    "round": round_num + 1,
                    "question": done_text,
                }
                current_query = _DR_AUTO_PROCEED
                continue

            if _looks_like_clarification(done_text):
                # No continuation can be sent (missing thread identity or the
                # configured round limit was reached). Override the preceding
                # clarification-shaped `done` with an explicit abnormal terminal
                # event rather than reporting the question as successful output.
                yield {
                    "type": "done",
                    "text": done_text,
                    "content_references": [],
                    "search_result_groups": [],
                    "terminated_abnormally": True,
                    "clarification_unresolved": True,
                }

            return

    async def deep_research_heavy(
        self,
        query: str,
        *,
        model: str | None = None,
    ) -> AsyncIterator[dict]:
        """Stream true Pro-tier Deep Research events for *query*.

        Two-phase: (1) SSE kickoff at /backend-api/f/conversation speaking
        "delta_encoding v1" JSON-patches; (2) if the stream closes before the
        assistant message reaches finished_successfully (async DR on complex
        queries), poll /backend-api/conversation/{id} until it does.

        Payload + endpoint ground-truth reverse-engineered from
        chatgpt.com/deep-research browser traffic (2026-04-23):

            model = gpt-6-pro
            system_hints = ["connector:connector_openai_deep_research"]
            thinking_effort = "extended"
            message.metadata.deep_research_version = "standard"
            message.metadata.venus_model_variant = "standard"

        Yields dicts of shape:
          {"type": "progress", "text": <partial>}   — streaming text deltas
          {"type": "tool",     "call": <call_text>} — tool/connector invocations
          {"type": "meta",     "data": <ste_meta>}  — server_ste_metadata events
          {"type": "done",     "text": <full_text>,
           "content_references": [...], "search_result_groups": [...]}

        Rate: consumes from the account-reported "deep_research" quota; limits
        and reset timing can vary.
        Timeout: 1800 s for initial SSE; poll phase adds up to 1800 s more.

        Note: the resolved_model_slug in user-message echo will show "i-mini-m"
        (the orchestration layer). The actual heavy reasoning runs inside the
        connector_openai_deep_research tool call.
        """
        # --- Quota guard ---
        # Probe /backend-api/conversation/init (POST) to check deep_research quota.
        # Response shape: limits_progress: [{"feature_name": "deep_research", ...}].
        # Fail-open on probe error; only "remaining <= 0" aborts.
        _INIT_PATH = "/backend-api/conversation/init"
        remaining: int | None = None
        try:
            init_data = await asyncio.to_thread(
                self._backend.post,
                _INIT_PATH,
                json={"conversation_mode_kind": "primary_assistant"},
            )
            limits = (init_data or {}).get("limits_progress") or []
            for lim in limits:
                if isinstance(lim, dict) and lim.get("feature_name") == "deep_research":
                    raw = lim.get("remaining")
                    if raw is not None:
                        remaining = int(raw)
                    break
        except Exception as _exc:
            _log.warning("DR quota check failed (%s) — proceeding anyway", _exc)
        if remaining is not None and remaining <= 0:
            raise RuntimeError(
                f"Deep Research quota exhausted. "
                f"Check {_BASE}{_INIT_PATH} (POST) to verify quota reset."
            )

        # Re-read codex token before snapshotting headers — heavy DR runs
        # for 5–30 min and codex may refresh ~/.codex/auth.json mid-stream.
        self._backend._reload_token_if_stale()
        headers = dict(self._backend._session.headers)
        headers["Accept"] = "text/event-stream"
        headers["Content-Type"] = "application/json"

        sentinel = await SentinelGate(self._backend).get_tokens()
        headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel[
            "chat-requirements"
        ]
        if sentinel.get("proof"):
            headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
        if sentinel.get("turnstile"):
            headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]

        payload = _build_heavy_dr_payload(query, model=model)

        # --- Phase 1: SSE kickoff with JSON-patch delta parser ---
        # /f/conversation speaks "delta_encoding v1". The first assistant envelope
        # arrives as {"v": {"message": {...}}, "c": N}. Subsequent text chunks
        # arrive as {"p": "/message/content/parts/0", "o": "append", "v": "..."}
        # or the shortcut {"v": "..."} (continuation of last path).
        # Batches: {"p": "", "o": "patch", "v": [<sub_patches>]}.
        state = {
            "conversation_id": None,
            "resume_token": None,
            "current_asst_id": None,
            "asst_text": "",
            "asst_status": "",
            "asst_metadata": {},
            "last_path": None,
            "tool_invoked": False,
            "tool_failed": False,
            "done_emitted": False,
            "citation_metadata": {},
            # True while the current assistant envelope is the connector-dispatch
            # JSON ({"path": ".../connector_openai_deep_research/start", ...}).
            # That envelope reaches finished_successfully almost immediately —
            # but its text is the dispatch payload, not the real report. Reset
            # when a fresh assistant envelope (the real report) arrives.
            "is_connector_dispatch": False,
        }

        def _emit_done(events: list) -> None:
            if state["done_emitted"]:
                return
            md = state["asst_metadata"] or {}
            citation_md = state["citation_metadata"] or {}
            refs, groups = _citation_payload(md, citation_md)
            payload: dict = {
                "type": "done",
                "text": state["asst_text"],
                "content_references": refs,
                "search_result_groups": groups,
            }
            if state["tool_failed"]:
                payload["connector_failed"] = True
            events.append(payload)
            state["done_emitted"] = True

        def _on_envelope(env: dict, events: list) -> None:
            msg = env.get("message") or {}
            role = (msg.get("author") or {}).get("role")
            recipient = msg.get("recipient")
            content = msg.get("content") or {}
            ct = content.get("content_type")
            if (
                role == "assistant"
                and recipient == "all"
                and ct in ("text", "multimodal_text")
            ):
                state["current_asst_id"] = msg.get("id")
                parts = content.get("parts") or []
                initial = parts[0] if parts and isinstance(parts[0], str) else ""
                state["asst_text"] = initial
                state["asst_status"] = msg.get("status") or ""
                state["asst_metadata"] = msg.get("metadata") or {}
                if _has_citation_payload(state["asst_metadata"]):
                    state["citation_metadata"] = state["asst_metadata"]
                state["is_connector_dispatch"] = _is_connector_dispatch_text(initial)
                if initial and not state["is_connector_dispatch"]:
                    events.append({"type": "progress", "text": initial})
                # Suppress _emit_done on:
                #   (a) connector-dispatch envelope (matches text heuristic), OR
                #   (b) any assistant envelope arriving with EMPTY text +
                #       finished_successfully — the dispatch placeholder for
                #       async DR (observed in production: heavy DR opens with
                #       parts=[""], status=finished_successfully, then the
                #       real report streams later via path patches or arrives
                #       in Phase 2 polling).
                if (
                    state["asst_status"] == "finished_successfully"
                    and not state["is_connector_dispatch"]
                    and state["asst_text"]
                ):
                    _emit_done(events)
            elif (
                role == "assistant"
                and isinstance(recipient, str)
                and recipient.startswith("api_tool")
            ):
                parts = content.get("parts") or []
                call = parts[0] if parts and isinstance(parts[0], str) else ""
                if call:
                    events.append({"type": "tool", "call": call})
                state["tool_invoked"] = True
            elif role == "tool" and recipient == "all":
                # Tool response — detect connector-not-available errors so
                # the caller can distinguish "DR ran" from "DR silently
                # fell through to i-mini-m because the connector isn't
                # provisioned on this account".
                parts = content.get("parts") or []
                text = parts[0] if parts and isinstance(parts[0], str) else ""
                if text and ("Resource not found" in text or text.startswith("Error")):
                    events.append({"type": "tool_error", "message": text})
                    state["tool_failed"] = True

        def _apply_path(path: str, op: str, value, events: list) -> None:
            if path == "/message/content/parts/0":
                if op == "append" and isinstance(value, str):
                    next_text = state["asst_text"] + value
                    was_dispatch = bool(state["is_connector_dispatch"])
                    next_is_dispatch = was_dispatch or _is_connector_dispatch_text(next_text)
                    state["asst_text"] = next_text
                    state["is_connector_dispatch"] = next_is_dispatch
                    if value and not next_is_dispatch:
                        events.append({"type": "progress", "text": value})
                elif op == "replace" and isinstance(value, str):
                    new_is_dispatch = _is_connector_dispatch_text(value)
                    if not new_is_dispatch and value.startswith(state["asst_text"]):
                        delta = value[len(state["asst_text"]) :]
                        if delta:
                            events.append({"type": "progress", "text": delta})
                    elif value and not new_is_dispatch:
                        events.append({"type": "progress", "text": value})
                    state["asst_text"] = value
                    state["is_connector_dispatch"] = new_is_dispatch
            elif path == "/message/status":
                if op == "replace" and isinstance(value, str):
                    state["asst_status"] = value
                    # Same empty-text guard as in _on_envelope: a status flip
                    # to finished_successfully on an empty assistant text
                    # buffer is the dispatch placeholder, not the real report.
                    if (
                        value == "finished_successfully"
                        and not state["is_connector_dispatch"]
                        and state["asst_text"]
                    ):
                        _emit_done(events)
            elif path == "/message/metadata" or path.startswith("/message/metadata/"):
                state["asst_metadata"] = _merge_metadata_path(
                    state["asst_metadata"], path, op, value
                )
                if _has_citation_payload(state["asst_metadata"]):
                    state["citation_metadata"] = state["asst_metadata"]

        def _apply_patch(obj: dict, events: list) -> None:
            t = obj.get("type")
            if t == "resume_conversation_token":
                state["resume_token"] = obj.get("token")
                if obj.get("conversation_id"):
                    state["conversation_id"] = obj["conversation_id"]
                return
            if t in ("message_marker", "message_stream_complete"):
                if obj.get("conversation_id"):
                    state["conversation_id"] = obj["conversation_id"]
                return
            if t == "server_ste_metadata":
                md = obj.get("metadata") or {}
                if md.get("tool_invoked"):
                    state["tool_invoked"] = True
                events.append({"type": "meta", "data": md})
                return
            if t == "input_message":
                return
            if t is not None:
                return

            p = obj.get("p")
            o = obj.get("o")
            has_v = "v" in obj
            v = obj.get("v")

            # Full envelope: explicit {"p": "", "o": "add", ...}
            # or implicit {"v": {"message": ...}, "c": N}
            if (
                isinstance(v, dict)
                and "message" in v
                and ((p == "" and o == "add") or (p is None and o is None))
            ):
                _on_envelope(v, events)
                state["last_path"] = None
                return

            # Batch patch
            if p == "" and o == "patch" and isinstance(v, list):
                for sub in v:
                    if isinstance(sub, dict):
                        _apply_patch(sub, events)
                return

            # Path-scoped patch
            if isinstance(p, str) and p:
                _apply_path(p, o or "replace", v, events)
                state["last_path"] = p
                return

            # Shortcut: bare "v" continues the last path (text-append)
            if p is None and o is None and has_v and state["last_path"]:
                _apply_path(state["last_path"], "append", v, events)
                return

        async with AsyncSession(impersonate="chrome131", verify=True) as s:
            resp = await s.post(
                _F_CONV_URL,
                headers=headers,
                json=payload,
                timeout=1800,
                stream=True,
            )
            if resp.status_code == 401:
                raise RuntimeError("401 Unauthorized — run `codex login`")
            if resp.status_code == 403:
                raise RuntimeError("403 Forbidden — token may have expired")
            if resp.status_code not in (200, 201):
                body = ""
                async for chunk in resp.aiter_content():
                    body += (
                        chunk.decode("utf-8", errors="replace")
                        if isinstance(chunk, bytes)
                        else chunk
                    )
                    if len(body) > 500:
                        break
                raise RuntimeError(
                    f"HTTP {resp.status_code} from {_F_CONV_URL}: "
                    f"{_redact_error(body, max_len=500)}"
                )

            async for raw_line in resp.aiter_lines():
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8", errors="replace")
                line = raw_line.strip()
                if not line or line.startswith(":") or line.startswith("event:"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                _raise_for_sse_error(obj)
                # Capture conversation_id from ANY frame that carries it at top
                # level. _apply_patch only sets it from a few typed events
                # (resume_conversation_token / message_marker /
                # message_stream_complete); the regular stream() captures it
                # generically. Without this, an async heavy-DR stream that closes
                # before one of those markers leaves state["conversation_id"]
                # None, so the Phase-2 poll gate below never fires and the report
                # is silently lost.
                if not state["conversation_id"]:
                    cid = obj.get("conversation_id")
                    if cid:
                        state["conversation_id"] = cid
                _raw_dump(obj, phase="heavy_sse")

                events: list[dict] = []
                _apply_patch(obj, events)
                for e in events:
                    yield e

        # --- Phase 2: Async polling fallback ---
        # If the stream closed without finished_successfully AND the DR
        # connector fired, poll /backend-api/conversation/{id} until the
        # real answer lands.
        if (
            not state["done_emitted"]
            and state["conversation_id"]
            and state["tool_invoked"]
        ):
            async for evt in self._poll_dr_completion(
                state["conversation_id"],
                seed_text=state["asst_text"],
                connector_failed=state["tool_failed"],
            ):
                yield evt
            return

        if not state["done_emitted"] and state["asst_text"]:
            # Stream ended mid-text without finalize — surface what we have.
            yield {
                "type": "done",
                "text": state["asst_text"],
                "content_references": [],
                "search_result_groups": [],
                "terminated_abnormally": True,
            }

    async def _poll_dr_completion(
        self,
        conv_id: str,
        *,
        seed_text: str = "",
        connector_failed: bool = False,
        interval: float = 120.0,
        max_wait: float = 1800.0,
    ) -> AsyncIterator[dict]:
        """Poll /backend-api/conversation/{id} until the DR answer lands.

        Walks mapping[*].message for the latest assistant text node; yields
        incremental progress until its status reaches finished_successfully
        (or max_wait elapses). The Deep Research App connector never writes its
        report as an assistant text node, so we also fetch the hidden widget
        state and recover the report from ``widget_state.report_message`` (see
        :func:`_dr_report_from_widget_state`).
        """
        detail_path = (
            f"/backend-api/conversation/{conv_id}"
            "?include_visually_hidden_messages=true&include_widget_state=true"
        )
        deadline = time.monotonic() + max_wait
        last_emitted = "" if _is_connector_dispatch_text(seed_text) else seed_text

        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                det = await asyncio.to_thread(self._backend.get, detail_path)
            except Exception as exc:
                _log.warning("DR poll error (%s) — continuing", exc)
                if "HTTP 429" in str(exc):
                    await asyncio.sleep(max(interval * 2, 300.0))
                continue
            if isinstance(det, dict):
                _raw_dump(det, phase="heavy_poll")

            mapping = (det or {}).get("mapping") or {}
            candidates = []
            citation_candidates = []
            for node in mapping.values():
                msg = (node or {}).get("message")
                if not isinstance(msg, dict):
                    continue
                meta = msg.get("metadata") or {}
                if _has_citation_payload(meta):
                    citation_candidates.append((msg.get("create_time") or 0, meta))
                if (msg.get("author") or {}).get("role") != "assistant":
                    continue
                recipient = msg.get("recipient")
                if recipient and recipient != "all":
                    continue
                content = msg.get("content") or {}
                if content.get("content_type") not in ("text", "multimodal_text"):
                    continue
                parts = content.get("parts") or []
                text = parts[0] if parts and isinstance(parts[0], str) else ""
                if not text:
                    continue
                if _is_connector_dispatch_text(text):
                    continue
                candidates.append(
                    (
                        msg.get("create_time") or 0,
                        msg.get("status") or "",
                        text,
                        meta,
                    )
                )
            # Deep Research App connector: the report is not an assistant text
            # node — it lives in the hidden widget state. If it's present, the
            # connector has finished; emit it as the final answer.
            widget_text, widget_refs = _dr_report_from_widget_state(det)
            if widget_text:
                if widget_text != last_emitted:
                    yield {"type": "progress", "text": widget_text}
                yield {
                    "type": "done",
                    "text": widget_text,
                    "content_references": widget_refs,
                    "search_result_groups": [],
                    "connector_failed": connector_failed,
                }
                return

            if not candidates:
                continue
            candidates.sort(key=lambda c: c[0])
            _, latest_status, latest_text, latest_meta = candidates[-1]

            if latest_text != last_emitted:
                if latest_text.startswith(last_emitted):
                    delta = latest_text[len(last_emitted) :]
                    if delta:
                        yield {"type": "progress", "text": delta}
                else:
                    yield {"type": "progress", "text": latest_text}
                last_emitted = latest_text

            if latest_status == "finished_successfully":
                refs = latest_meta.get("content_references") or []
                groups = latest_meta.get("search_result_groups") or []
                if citation_candidates and (not refs or not groups):
                    turn_keys = ("working_turn_id", "turn_exchange_id")
                    same_turn = [
                        meta
                        for _, meta in sorted(citation_candidates, key=lambda c: c[0])
                        if any(
                            latest_meta.get(k) and latest_meta.get(k) == meta.get(k)
                            for k in turn_keys
                        )
                    ]
                    fallback_metas = same_turn or [
                        meta
                        for _, meta in sorted(citation_candidates, key=lambda c: c[0])
                    ]
                    for meta in reversed(fallback_metas):
                        if not refs:
                            refs = meta.get("content_references") or []
                        if not groups:
                            groups = meta.get("search_result_groups") or []
                        if refs and groups:
                            break
                yield {
                    "type": "done",
                    "text": latest_text,
                    "content_references": refs,
                    "search_result_groups": groups,
                    "connector_failed": connector_failed,
                }
                return

        yield {
            "type": "done",
            "text": last_emitted,
            "content_references": [],
            "search_result_groups": [],
            "connector_failed": connector_failed,
            "terminated_abnormally": True,
            "timeout": True,
        }
