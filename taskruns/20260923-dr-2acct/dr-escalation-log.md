# dr-escalation-log — 2026-09-23/24 serial audit (this lane)

Trigger: owner-relayed claim "已实测:gpt2agent 普通会话 404(light DR 和直连
complete() 都死);heavy DR connector 是唯一可用 gpt-6-pro 通道".

## Serial audit (one probe at a time, timestamps local)

| # | Time | Probe (account) | Result |
|---|---|---|---|
| 0 | 23日 22:0x | light DR via MCP, A + B (final HEAD 1d62e99) | **PASS** 16.7 s / 1371 chars; **PASS** 16.6 s / 1192 chars |
| 1 | 23日 23:5x | direct `complete(gpt-5-6)`, A | **PASS** 10.6 s, out='OK' |
| 2 | 23日 23:5x | endpoint liveness, no-sentinel empty POST | `/backend-api/conversation` → **422**; `/backend-api/f/conversation` → **422** (both alive + auth OK + payload-gated; **no 404**) |
| 3 | 23日 23:5x | direct `complete(gpt-5-6)`, B (`CODEX_HOME=~/.codex-cx2`) | **PASS** 7.7 s, out='OK' |

## Verdict on the claim

Does NOT reproduce on either account, either endpoint, at this HEAD:
- 普通会话 (complete) works on A and B (probes 1, 3).
- light DR works on A and B (probe 0, receipts A/B-light-final.md).
- No 404 on either conversation endpoint (probe 2); a retired endpoint
  would 404 on an empty authenticated POST, not 422.

## Why the claim likely read as true (hypotheses, labeled)

- (unverified) The reporting lane ran the FLEET install at the main checkout
  (e911a3a): there light DR aborts in-band ("Error in message stream") on
  every research turn — that matches "light DR 死", though it is an abort,
  not a 404.
- (unverified) A lane WITHOUT the sentinel bridge (`GPT2AGENT_SENTINEL_BRIDGE_OFF=1`
  or missing `~/.gpt2agent/sentinel-bridge`) fails at the legacy gate
  (turnstile BLOCKED per doctor) — every conversation-family tool dies
  there, which reads as "普通会话都死".
- (unverified) A stale/flagged session identity can 4xx conversation POSTs
  until cookies/session rotate; our sim profile rotated on 23日 and passes.
- "heavy-only" matches the PRE-fix state of this branch (light broken,
  heavy working) — i.e. a report generated before commit e6fc630.

Heavy DR as "the only gpt-6-pro channel" was true for research turns
pre-fix; post-fix both lanes work (see dr-2acct-recovery receipt).

## Follow-ups handed to owner

- If the 404s persist on a specific lane: name the lane + exact
  command + timestamp and this log gets a reproduction entry.
- Fleet installs must run `scripts/fleet-sync.sh origin/main` after merge —
  stale editable installs reporting stale behavior is the standing trap
  (2026-09-18 incident, release runbook §7).
