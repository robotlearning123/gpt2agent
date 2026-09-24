# TASKS — DR verification, both accounts, via MCP (2026-09-23)

Owner directive: "must verify, 2 account must work for all" + "fix" +
"use workflow, must verify, use /my-review".

Accounts:
- A = default `~/.codex/auth.json` (account_id ffb1d75…)
- B = `CODEX_HOME=~/.codex-cx2` (account_id 606e1424…)

Code under test: this worktree (v0.0.23 + fix commits, see git log).
Python: /home/robot/.local/share/gpt2agent-venv/bin/python (editable → this worktree).

## Matrix — ALL GREEN

- [x] 0. Preflight: doctor on A and B — 24 OK each, chatgptpro, bridge mint OK
- [x] 0b. DR quota: A remaining=176 (resets 10-17), B remaining=249 (resets 10-23)
- [x] 1. Light `deep_research` via fresh MCP on A — PRE-FIX FAIL (in-band
      "Error in message stream", 33.4s) → POST-FIX PASS 15.1s / 1486 chars
      (A-light-fixed.md, real python.org citations + Sources)
- [x] 2. Light on B — PRE-FIX FAIL (11.8s) → POST-FIX PASS 15.9s / 1274 chars
      (B-light-fixed.md)
- [x] 3. Heavy on A — PASS 144.6s real report (A-heavy.md)
- [x] 4. Heavy on B — PASS 152.8s real report (B-heavy.md)
- [x] 5. Receipts + summary matrix in this dir; committed incrementally
- [ ] 6. /my-review on the diff (running)
- [ ] 7. Final report to owner (zh)

## Root cause (evidence: E0-E10 probes + PROD-frames.jsonl + workflow analysts)

1. Upstream retired the research-hint lane (2026-09-22 GPT-6 rollout):
   ANY search/research-class system_hints turn aborts in-band before
   assistant content; no-hint turns auto-search and stream fine. Model slug
   irrelevant (E4: gpt-6-pro + hint also aborts).
2. Client parser gap: light loop dropped all v1 delta frames (chat/heavy
   were unified in 0.0.23, light missed) — even a healthy stream returned
   empty text.
3. Two subtler wire shapes fixed from live captures: bare-list batch frames
   ({"v":[…]}, no o flag) carry the finished-status flip; envelope-then-
   patch ref ordering nests lists at _append_value → citation crash.

Fix commits: e6fc630 (model+parser), bare-list+flatten follow-up, docs.

## Client wiring (owner: "why only 1 gpt work")

~/.claude.json had ONE gpt2agent entry (no env → always account A).
Added `gpt2agent-b` with env CODEX_HOME=~/.codex-cx2 (backup
~/.claude.json.bak-gpt2agent-b-20260923). Client restart picks it up.
Docs: docs/clients.md "Multiple ChatGPT accounts in one client".

Power rule honored: light-DR FAIL verdicts were only recorded after heavy
DR passed on the same account in the same window (pipeline proven good).
