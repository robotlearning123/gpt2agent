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
row means the environment changed, not the code. Exit code note: `doctor`
exits 1 while any row is blocked upstream (by design, `doctor.py`), so during
the blockade the healthy gate is **exit 1 with "0 failed"** in the summary —
exit 2 means no token, any "N failed" row is a real regression.

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

## 5. Artifact outsider emulation (added 2026-09-15, v0.0.14)

Exercise the BUILT artifact the way a first-time user would — build, wheel
install into a clean venv, no-token first run, client registration with an
isolated HOME, a real MCP stdio client session (tool schemas + live
read-only calls + a `manual=True` handoff), the 0.0.13→new upgrade path,
and uninstall cleanliness:

```bash
scripts/release-emulation-test.sh <release-worktree> | tee artifacts/verify/human-emulation-$(date +%Y%m%d).log
```

Must end `RESULT: N passed, 0 failed`. Introduced after v0.0.14's first cut
caught a missed `.claude-plugin/plugin.json` version bump and a test-harness
stdio flag error before they reached users.
