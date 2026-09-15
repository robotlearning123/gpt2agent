# Spec: Phase 0 — Manual Handoff Mode (`manual=True`)

Date: 2026-09-15 · Owner: primary agent · Status: approved for implementation

## Goal

Conversation-class MCP tools gain a `manual: bool = False` parameter. When
`manual=True`, the tool makes **zero network calls** and returns a
self-contained JSON handoff: the exact prompt text the REST path would send,
where to paste it, and how to read the result back. This keeps blocked
workflows (chat, Deep Research, image gen, code interpreter, canvas, custom
GPTs, memory-via-chat) usable while the upstream Sentinel/Turnstile challenge
blocks `/backend-api/conversation` (measured 2026-09-08, still blocked).

## Non-goals

- No browser automation (Phase 1).
- No automatic result retrieval (readback stays a separate, existing tool).
- No changes to REST payloads, retry logic, or default tool behavior
  (`manual` defaults to `False`; REST path is byte-identical).
- No new dependencies.

## Interface

New module `gpt2agent/tools/manual.py`:

```python
def build_handoff(tool: str, prompt: str, *, model: str | None = None,
                  temporary: bool | None = None, extra: dict | None = None) -> dict
```

Returns:

```python
{
  "status": "manual_handoff",
  "tool": "chat",                      # calling tool's name
  "prompt": "<exact text to paste>",   # same wrapper text the REST path compiles
  "url": "https://chatgpt.com/",       # tool-specific landing URL
  "model_hint": "gpt-5-6",             # or None when the tool fixes the model
  "temporary_hint": True,              # suggested chat privacy mode
  "steps": [...],                      # numbered paste/send instructions
  "readback": {"list": "list_conversations", "fetch": "get_conversation"},
}
```

Tool-specific values:

| tool | url | model_hint | temporary_hint |
|---|---|---|---|
| chat | https://chatgpt.com/ | caller's `model` | caller's `temporary` |
| agent | https://chatgpt.com/ (Agent mode) | None (mode-driven) | False |
| deep_research | https://chatgpt.com/ (Deep Research mode) | None | False (DR refuses temporary) |
| deep_research_heavy | https://chatgpt.com/ (Deep Research, Pro tier) | None | False |
| gpt_chat | https://chatgpt.com/g/{gizmo_id} | None (GPT-fixed) | False |
| generate_image | https://chatgpt.com/ (Images mode) | None | False |
| code_interpreter | https://chatgpt.com/ | None | False |
| canvas_execute | https://chatgpt.com/ (Canvas) | caller's `model` if any | False |
| memory_create_via_chat | https://chatgpt.com/ | None | False |

Server tools (`gpt2agent/server.py`, plus `gpt2agent/tools/tools_features.py`
where the tool body lives) add `manual: bool = False` and early-return
`json.dumps(build_handoff(...), indent=2)` before touching the backend.

**Single source of truth**: where a REST path wraps the user's prompt in a
larger instruction string (e.g. `"Use Canvas to: {prompt}"`,
memory-create phrasing), extract that wrapper into one shared constant used by
BOTH the REST path and `build_handoff` — the manual prompt must equal the REST
prompt byte-for-byte.

## Acceptance oracle

1. `pytest -q tests/` fully green (existing suite unmodified in behavior).
2. `tests/test_manual_handoff.py` (new):
   - For every conversation-class tool: `manual=True` returns parsed JSON with
     `status == "manual_handoff"`, `prompt` equal to the exact wrapper text the
     REST path compiles for the same input, and correct `url`/`model_hint`.
   - Zero network: monkeypatch the backend `get`/`post` to raise
     `AssertionError("network touched")` — manual path must not trip it.
   - `manual` defaults to `False`; a normal call path is unchanged (existing
     tests cover this — do not weaken them).
3. `python -c "import gpt2agent.server"` exits 0.
4. `grep -rn "Use Canvas to" gpt2agent/` shows the wrapper defined exactly once.

## Deliverables

- `gpt2agent/tools/manual.py`, modified `gpt2agent/server.py` +
  `gpt2agent/tools/tools_features.py` (+ any wrapper extraction)
- `tests/test_manual_handoff.py`
- `docs/release-validation.md` — release validation runbook (see below)
- This spec stays at `docs/dev/specs/phase0-manual-handoff.md`

## docs/release-validation.md outline

1. Pre-release: `gpt2agent doctor` output archived (expect read-only OK rows,
   blocked rows unchanged); version + date recorded.
2. Manual-handoff roundtrip: one real `chat(manual=True)` → human pastes →
   `get_conversation` readback; receipt path `artifacts/verify/`.
3. Regression: `pytest -q` output archived.
4. Blocked-tool annotation check: README status table matches doctor.
