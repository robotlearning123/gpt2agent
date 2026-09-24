# Receipt — light Deep Research recovery, 2-account verification + /my-review

Date: 2026-09-23 · Branch: `fix/dr-recovery-20260923` · PR #83 · Base: e911a3a (v0.0.23)
Owner directive: "must verify, 2 account must work for all" + "fix" + "use workflow, must verify, use /my-review".

## Root cause (established by execution)

1. Upstream retired the research-hint lane in the 2026-09-22 GPT-6 rollout:
   ANY `search`/`research`-class `system_hints` turn is accepted, streams the
   system preamble, then aborts in-band (`Error in message stream`) before
   assistant content. Model slug irrelevant (E4: `gpt-6-pro` + hint aborts
   too). No-hint turns auto-search and stream normally with `citeturn`
   markers + `content_references` (E10a3, ALIVE end-to-end).
   11 discriminating probes: `taskruns/20260923-dr-2acct/E*.jsonl`.
2. Client parser gap: the light-DR loop dropped every v1 delta frame
   (0.0.23 unified chat+heavy, missed light) — healthy streams → empty text.
3. Wire shapes from live captures: bare-list batch frames (`{"v":[…]}`
   without `o`) carry the finished-status flip (PROD-frames.jsonl frame 39);
   envelope-then-patch refs nest inside `_append_value` → citation crash.

## Live verification matrix (fresh stdio MCP, `tools/run_dr_mcp_once.py`)

| Tool | Account A (default) | Account B (`CODEX_HOME=~/.codex-cx2`) |
|---|---|---|
| deep_research (light) pre-fix | FAIL 33.4 s in-band abort | FAIL 11.8 s in-band abort |
| deep_research (light) post-fix | **PASS** 15.1 s / 1486 chars | **PASS** 15.9 s / 1274 chars |
| deep_research (light) FINAL HEAD | **PASS** 16.7 s / 1371 chars | **PASS** 16.6 s / 1192 chars |
| deep_research_heavy | **PASS** 144.6 s real report | **PASS** 152.8 s real report |
| doctor | 24 OK, bridge mint OK, Pro | 24 OK, bridge mint OK, Pro |

DR quota at start: A remaining=176 (resets 10-17), B remaining=249 (resets 10-23).
Citations verified in outputs (python.org Sources sections).
Power rule: light-FAIL verdicts recorded only after heavy PASS on the same
account in the same window (pipeline proven good → failure isolated upstream).

## Machine gates (S0)

- Suite: 619 passed, 13 skipped (was 603 pre-work; +16 tests pin the recipe,
  parser, batch/ref shapes, envelope-reset, clean-done preference, merge).
- ruff: clean. Ref pinned e911a3a..HEAD.
- `.claude/verify.sh` added: uv default sync skips the `dev` extra → the
  pre-commit gate's `uv run python -m pytest` failed with `No module named
  pytest` (environment, not suite).

## Review chain (/my-review)

- S1 Devin finder (2 runs, execution-confirmed): envelope must reset the
  bare-v continuation path (repro: done text `newSTRAY`) → fixed 5cce4fe.
- S3 gate — **lane failures recorded**: grok 3 attempts stalled (420–540 s,
  final 0 bytes); NIM kimi-k3 4/4 keys TimeoutError. Substitute per
  cross-model-review rule: Devin (different family from writer; execution-
  capable; not on the banned list).
- S3 Devin substitute (executed 7 adversarial shapes + variants): FIX-FIRST —
  S1 pre-envelope patch dropped (sse falsy-anchor guard), S3 foreign status
  → premature done, S6b last-done-wins discards clean answer → fixed
  081162a + 1d62e99 (deterministic reordered-envelope merge).
- S4 delta round 1: S3/S6b fixed, S1 residual → round 2: **SHIP**,
  S1 fixed (done `Hello`), 3 pinned merge semantics pass, 0 regressions.
- S5 /simplify: 4-agent pass (reuse/simplification/efficiency/altitude) —
  findings + dispositions below.
- Writer ≠ reviewer: writer = session (glm family); reviewer = Devin family,
  execution-based; session-model vote = live MCP matrix above + suite runs.
- Residuals (accepted, with rationale):
  - Unscoped patches carry no message id → interleaved-message scoping stays
    last-envelope-wins (wire-ambiguous; Devin S2 judged "only possible
    semantic"). Refs-before-any-envelope are dropped (never observed live).
  - Light DR quota gate (`_feature_remaining("deep_research")`) kept and
    MEASURED CORRECT (2026-09-24): each completed auto-search light turn
    costs 1 from the same bucket (A −9 over 9 completed search turns,
    B −1 over 1; aborted pre-fix turns cost 0), while heavy connector
    runs bill elsewhere (two full heavy reports moved this bucket by 0).
  - Fleet editable install currently points at THIS worktree; run
    `scripts/fleet-sync.sh origin/main` after merge (release runbook §7).

## Client wiring (owner: "why only 1 gpt work")

`~/.claude.json` had one `gpt2agent` entry (no env → always account A).
Added `gpt2agent-b` with `env.CODEX_HOME=~/.codex-cx2` (backup:
`~/.claude.json.bak-gpt2agent-b-20260923`); restart picks it up.
Docs: `docs/clients.md` § Multiple ChatGPT accounts in one client.

## Commands (repro)

- Matrix: `tools/run_dr_mcp_once.py --tool {deep_research,deep_research_heavy} --output …`
  (account via `CODEX_HOME`; venv `/home/robot/.local/share/gpt2agent-venv`).
- Suite: `bash .claude/verify.sh` · Lint: `python -m ruff check gpt2agent tests`.
- Evidence: `taskruns/20260923-dr-2acct/` (frames, probes, receipts, TASKS.md).
