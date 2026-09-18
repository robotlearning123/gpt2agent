# gpt2agent

MCP server exposing full ChatGPT Plus/Pro account features to any MCP client.

## Build & Test

```bash
pytest                              # full suite must pass (513+; live auto-skip via SKIP_LIVE)
python -m ruff check gpt2agent tests
python -m gpt2agent run             # start MCP server (stdio)
```

## Key Files

- `gpt2agent/server.py` — MCP tool registration (25 tools), config loading
- `gpt2agent/sse.py` — Async SSE client for `/backend-api/conversation` (chat, DR, agent, image gen, code interpreter, canvas); sentinel bridge integration; inline citation rendering
- `gpt2agent/sentinel_bridge.py` — Owner-supplied bridge loader (fingerprint p + PoW + VM turnstile); opt-in via ENABLED marker or env
- `gpt2agent/citations.py` — DR inline citation rendering (citeturn markers → [N](url))
- `gpt2agent/browser.py` — Playwright Chrome transport (optional extra `gpt2agent[browser]`)
- `gpt2agent/backend.py` — Sync HTTP client (`curl_cffi`), token management
- `gpt2agent/tools/` — Tool modules (19 of 25; the 6 SSE tools live in server.py)
- `gpt2agent/sentinel.py` — Legacy POW + Turnstile gate (fallback path)

## Critical Invariants

- `temporary=True` (chat default) sets `history_and_training_disabled=True`, blocking image gen, code interpreter, canvas, and memory writes. Tools needing these MUST pass `temporary=False`.
- Deep Research requires `temporary=False` (refuses temporary chats).
- `manual=True` > `browser=True` > REST (transport precedence).
- Token reloaded from `~/.codex/auth.json` or `~/.gpt2agent/token.json` on each request (mtime check).
- `BackendClient` is synchronous. Async tools must wrap in `asyncio.to_thread()`.
- Sentinel bridge is opt-in: `~/.gpt2agent/sentinel-bridge/ENABLED` marker or `GPT2AGENT_SENTINEL_BRIDGE` env. `GPT2AGENT_SENTINEL_BRIDGE_OFF=1` forces legacy path (tests set this in conftest.py).
- The bridge directory is NOT part of this distribution — see docs/dev/specs/sentinel-vm.md for the replacement spec.

## Testing

- Offline: `pytest` (513+ unit/contract tests)
- Live matrix: `scripts/agent-user-journey.sh <worktree>` (15 cases)
- Release gate: `scripts/release-emulation-test.sh <worktree>` (11 checks)
- Parameter contracts: `tests/test_param_matrix.py` (34 cases)

## Release

1. Bump version in 4 files: pyproject.toml, __init__.py, plugin.json, server.json
2. Add CHANGELOG.md entry
3. `python scripts/verify_release.py`
4. Merge PR, tag merge SHA (annotated tag), push tag
5. CI publishes to PyPI (trusted publishing) + GitHub Release
6. Full runbook: docs/release-validation.md (incl. owner publish gate)
