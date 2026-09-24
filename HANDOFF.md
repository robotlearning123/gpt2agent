# HANDOFF — dr-recovery branch (2026-09-24)

For the next agent/session picking this up. Everything below is repo-visible
fact; owner-side notes live in the owner's session memory.

## State

- Branch `fix/dr-recovery-20260923`, 16 commits, **PR #83 OPEN, review
  COMPLETE, awaiting owner merge** (two protection layers per merge gates).
- Suite: 619 passed / 13 skipped (truly offline — the one unguarded live
  test now has a SKIP_LIVE guard), ruff clean (`bash .claude/verify.sh`
  runs both; uv needs `--extra dev`, which verify.sh handles).
- Live verification at final HEAD, both accounts, receipts in
  `taskruns/20260923-dr-2acct/` + `artifacts/verify/dr-2acct-recovery-2026-09-23.md`:
  light DR A 16.7 s / B 16.6 s PASS; heavy A 144.6 s / B 152.8 s PASS.
- Review chain (recorded in the receipt): Devin S1 → S3 substitute gate
  FIX-FIRST (grok lane stalled 3×, NIM 4/4 timeout) → S4 round 2 SHIP →
  /simplify applied.

## What the branch does (one paragraph)

Upstream retired the research-hint lane in the 2026-09-22 GPT-6 rollout;
light `deep_research` now rides the configured chat model (no `research`
hint) and its SSE loop finally parses the v1 delta encoding (envelopes,
batch frames incl. no-`o` lists, path patches, bare-`v` continuations, ref
flattening, deterministic reordered-envelope merge, clean-done preference).
Docs rewritten accordingly; quota semantics measured and documented
(image_gen 999/day is a real counter; the 100/3 h + 15 s numbers are OUR
ratelimit.py defaults, not an upstream cap).

## After merge (in order)

1. `scripts/fleet-sync.sh origin main` — the fleet editable install points
   at THIS worktree until synced (2026-09-18 incident class).
2. Repoint/remove the worktree per release runbook §8; kill leftover shells.
3. Release: if this ships as a version, follow `docs/release-validation.md`
   (4-file bump, CHANGELOG [Unreleased] → dated, verify_release.py, owner
   publish gate, annotated tag).

## Environment notes (this workstation)

- Python for everything: `/home/robot/.local/share/gpt2agent-venv/bin/python`
  (editable → the worktree until fleet-sync).
- Accounts: A = default `~/.codex`; B = `CODEX_HOME=~/.codex-cx2`. MCP
  client entries `gpt2agent` + `gpt2agent-b` exist in `~/.claude.json`
  (restart the client to activate the second).
- Sentinel bridge: `~/.gpt2agent/sentinel-bridge/` + `ENABLED` marker. Tests
  set `GPT2AGENT_SENTINEL_BRIDGE_OFF=1` via conftest.
- Live probes: never parallelize conversation POSTs across accounts beyond
  the shared limiter; the runner is `tools/run_dr_mcp_once.py`
  (`--tool deep_research|deep_research_heavy`, account via CODEX_HOME).

## Follow-ups (recorded, not in this PR)

- `stream()`'s batch-patch branch still requires `o == "patch"` — it would
  drop the no-`o` batch frames the DR loop now handles (pre-existing chat
  path gap; fix + test in its own PR).
- Dispatcher consolidation: three hand-rolled frame dispatchers exist
  (`stream()._handle_frame`, heavy, light `_frame`); folding the mechanical
  classification into `_MessageDelta` was reviewed and deferred (DR observer
  semantics make it non-drop-in).
- Light-DR quota gate stays: measured 1 point per completed search turn
  from the `deep_research` bucket (aborted turns cost 0); heavy bills an
  independent cap.
