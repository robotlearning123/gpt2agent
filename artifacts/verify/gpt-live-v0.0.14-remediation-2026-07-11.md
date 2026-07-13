# v0.0.14 GPT-Live bridge — honesty remediation verify receipt (2026-07-11)

Branch `release/v0.0.14-live-voice`, uncommitted working tree on top of `be5f2af`.
Re-scope (owner): **human → agent only, no agent→Live speak**. Reliable path = real
signed-in Chrome + `sidecar/extension` + `sidecar/agent-gateway.mjs`; puppeteer
`browser/sidecar.mjs` is a test harness (fake mic not transcribed).

## What changed (net −9 LOC over the pulled Mac work; 29 files)
- **Cut the agent→Live write channel** (false-success): removed `voice_live_send_text`,
  `POST /send_text`, `export.mjs` speak-queue/`buildSpeakWire`, `session.mjs.speak`.
- **Real parser on every path**: `ModeBExport.ingest`/`handleUtterance` use
  `TranscriptAssembler` + `isActionable`; the reliable extension path routes through
  the gateway's shared bridge; the gateway serves the control plane so `voice_live_*`
  observe the real path.
- **Fixes**: `/end` deadlock (respond-before-onEnd + shutdown timeout); `agent-runner.mjs`
  group-killing timeout (returns in ~200ms, was 5003ms) + EPIPE guard; gateway loopback +
  no-CORS + body cap + optional token (extension-compatible); recursive secret redaction
  (py+js) incl. embedded JWT; version 0.0.14 across pyproject/__init__/server.json/plugin;
  tool count **30 / 12 modules** consistent across all current advertising surfaces;
  packaging disclosure (sidecar ships in source repo, not the wheel).
- Spec: `docs/superpowers/plans/2026-07-11-gpt-live-bridge-layer-spec.md`.

## Gates (run locally, real output)
- `cd sidecar && npm test` → **50 passed, 0 failed, 3 skipped** (incl. new
  `agent-runner.test.mjs` timeout regression + `handleUtterance` filter test).
- `PYTHONPATH=$PWD .venv/bin/python -m pytest tests/ -q` → **366 passed, 15 skipped**.
- `ruff check gpt2agent/ tests/` → **All checks passed**.
- `git diff --check be5f2af` → clean.

## Cross-model review (writer = Mac agent / Opus edits; reviewer = cx GPT-5.6 + Opus)
- **cx round-1**: BLOCK — send_text phantom success, wrong parser, version/packaging,
  /end deadlock, gateway. (All addressed.)
- **cx round-2**: findings #2–#5 **RESOLVED** with live probes (filler → `filtered:true`;
  one actionable call → 1 human + 1 agent transcript; `/status turns:1 transcriptCount:2`;
  `runAgent sleep 5 @200ms` → `[agent timed out]` in 202ms, descendant PID reaped;
  token optional+extension-compatible; packaging disclosure present). Remaining: tool-count
  doc staleness.
- **cx round-3 / round-4**: progressively deeper count scan → fixed 31-group
  (faq/how-it-works/install.py/tools-reference ToC+anchor) then 26-era group
  (marketplace.json/CONTRIBUTING/CLAUDE.md/docs/README). Final scan: no count != 30 on any
  current surface.
- **Residual (rebutted, not fixed)**: `QA_REPORT.html:254` says "25 tools" — but it is a
  **dated v0.0.2 generated QA report** (`<title>gpt2agent v0.0.2 — Pre-Release QA Report`,
  Generated 2026-05-27), unchanged from `be5f2af`. Editing it would falsify a historical
  record; it is out of scope (same class as CHANGELOG/plan docs). **Recommend: delete the
  stale generated artifact as a separate cleanup (owner decision).**

## Verdicts
- **Opus (this session)**: PASS — v0.0.14 is honest & internally consistent; the sole cx
  residual is a historical artifact (rebutted by evidence).
- **cx (GPT-5.6)**: functional review PASS (round-2 live-probe verified); count-consistency
  PASS on all current surfaces; literal round-4 verdict cited only `QA_REPORT.html` (dated
  v0.0.2 report — excluded by class).

## Not done (explicit)
- NOT committed / pushed / merged (awaiting owner go).
- Live spoken round-trip is inherently gated by real signed-in Chrome (Turnstile) — not
  demonstrated headlessly by design; the extension path is the human-run route.
