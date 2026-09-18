# v0.0.17 verification — unified sim profile + f/conversation

Date: 2026-09-19. Branch: feat/human-sim-v0.0.17 (worktree gpt2agent-sim).

## Offline

- `pytest`: 530 passed / 12 skipped / 0 failed
- `ruff check gpt2agent tests`: clean
- `scripts/verify_release.py`: release metadata verified: 0.0.17

## Live (paced)

- `gpt2agent doctor`:
  - `sentinel (bridge)` → **OK** — real mint: requirements + PoW + VM
    turnstile + `f/conversation/prepare` conduit token, all inside the shared
    SimProfile session (chrome136 impersonation end-to-end).
  - `account_limits` → OK — reported `model gpt-6-pro resets
    2026-09-20T01:31:59Z`, `deep_research remaining=0`, `default model
    downgraded gpt-6-pro→gpt-5-6-thinking`.
  - `sentinel (legacy gate)` → BLOCKED (vendored turnstile still dead —
    informational only; gate tools correctly inherit bridge OK).
- `chat(gpt-5-6)` via bridge → `POST /f/conversation` HTTP 200, v1 delta
  encoding parsed (`{"p":"/message/content/parts/0","o":"append"}` batch
  patch), reply `SIM OK` — exact match.
- `chat(gpt-6-pro)` → `UsageLimitError: ChatGPT SSE error: You've hit your
  limit. … Resets at <ts>. Server-side fallback: 'gpt-5-6-thinking'.` —
  quota exhaustion surfaced truthfully, no silent mini substitution.

## Raw frame evidence (f/conversation, gpt-5-6)

```
NOND: v1
{"type":"resume_conversation_token", ...}
{"p":"","o":"add","v":{"message":{…role:user…}}}
{"v":{"message":{…role:system…}}}        ×4
{"v":{"message":{…role:assistant…}}}     ×2
{"p":"","o":"patch","v":[{"p":"/message/content/parts/0","o":"append","v":"SIM OK"}, …]}
{"type":"server_ste_metadata","metadata":{…,"model_slug":…}}
{"type":"message_stream_complete"}
[DONE]
```

## Known non-goals

- gpt-6-pro is account-capped until 2026-09-20T01:31:59Z — fingerprint
  work cannot lift a server-side quota; the tool now reports it.
- DR quota `remaining=0` until 2026-09-18T21:56Z — guarded with a typed
  error instead of an empty report.
- `list_conversations` observed intermittent HTTP 429 (account throttling),
  unrelated to the change set.
