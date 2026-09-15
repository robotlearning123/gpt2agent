# Release validation

Runbook for validating a gpt2agent release before tagging. Archive every
receipt under `artifacts/verify/` so the run is auditable later.

## 1. Pre-release health check

```bash
gpt2agent --version
gpt2agent doctor | tee artifacts/verify/doctor-$(date +%Y%m%d).txt
```

Record the version and date in the release notes. Expect read-only rows to
report OK; rows for tools blocked by the upstream Sentinel/Turnstile challenge
must be unchanged from the previous release — a newly blocked or newly fixed
row means the environment changed, not the code.

## 2. Manual-handoff roundtrip

While `/backend-api/conversation` is blocked, verify the Phase 0 fallback
end to end exactly once:

1. Call `chat("release check", manual=True)` and confirm it returns a JSON
   handoff (`status: "manual_handoff"`) with zero network calls.
2. Paste `prompt` into chatgpt.com by hand and send it.
3. Read the reply back with `list_conversations` → `get_conversation`.

Save the handoff JSON and the fetched conversation under
`artifacts/verify/` as the roundtrip receipt.

## 3. Regression

```bash
pytest -q | tee artifacts/verify/pytest-$(date +%Y%m%d).txt
```

Must be fully green — 0 failures, no new skips versus the previous run.

## 4. Blocked-tool annotation check

The README tool-status table must match the `doctor` output from step 1
row-for-row. If `doctor` reports a tool newly blocked or newly working,
update the README table in the same release — do not ship a stale status.
