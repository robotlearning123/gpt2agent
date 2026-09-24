# TASKS — DR verification, both accounts, via MCP (2026-09-23)

Owner directive: "must verify, 2 account must work for all".

Accounts:
- A = default `~/.codex/auth.json` (account_id fffb1d75…)
- B = `CODEX_HOME=~/.codex-cx2` (account_id 606e1424…)

Code under test: this worktree @ 183b928 (v0.0.23 + fresh MCP runner).
Python: /home/robot/.local/share/gpt2agent-venv/bin/python (editable → this worktree).

## Matrix

- [ ] 0. Preflight: `gpt2agent doctor` on A and B (sentinel bridge, token, plan)
- [ ] 0b. DR quota headroom via `gpt2agent usage` on A and B (heavy consumes monthly quota)
- [ ] 1. Light `deep_research` via fresh MCP on A
- [ ] 2. Light `deep_research` via fresh MCP on B
- [ ] 3. Heavy `deep_research_heavy` via fresh MCP on A (tools/run_heavy_mcp_once.py)
- [ ] 4. Heavy `deep_research_heavy` via fresh MCP on B
- [ ] 5. Receipts + summary matrix in this dir; commit
- [ ] 6. Report to owner (zh), honest per-cell verdicts

Power rule (anti-hallucination): a light-DR FAIL only counts as "upstream broken" if
heavy DR passes on the same account in the same window (pipeline proven good).

Stop conditions: quota exhausted on an account (skip its heavy, report);
both heavies done; any UpstreamChallengeError → check bridge, one retry, then report.
