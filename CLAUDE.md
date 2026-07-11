# gpt2agent

MCP server exposing full ChatGPT Plus/Pro account features to any MCP client.

## Build & Test

```bash
pytest                    # full suite must pass; live/network tests auto-skip (SKIP_LIVE)
python -m gpt2agent run   # start MCP server (stdio)
```

## Key Files

- `gpt2agent/server.py` — SSE-backed MCP tool registration, config, and local HTTP guard
- `gpt2agent/sse.py` — Async SSE client for `/backend-api/conversation` (chat, DR, agent, image gen, code interpreter, canvas)
- `gpt2agent/backend.py` — Sync HTTP client (`curl_cffi`), request-local auth, bounded responses, request policy
- `gpt2agent/tools/` — 15 registration modules (26 of 32 tools; 6 SSE tools live in `server.py`)
- `gpt2agent/tool_contracts.py` / `tool_manifest.py` — exact tool names and MCP annotations
- `gpt2agent/resources.py` / `gpt2agent/resources/*.json` — 2 static packaged MCP resources
- `gpt2agent/model_catalog.py` — auth-generation-keyed General/Work model metadata cache and validation
- `gpt2agent/sentinel.py` — POW + Turnstile solver
- `gpt2agent/install.py` — `gpt2agent install` subcommand

## Critical Invariants

- `temporary=True` sets `history_and_training_disabled=True`, which blocks image gen, code interpreter, canvas, and memory persistence. Tools that need these features MUST pass `temporary=False`.
- Authorization is request-local, never stored in shared session headers. Multi-request operations use one auth snapshot; model metadata caches are keyed by auth generation.
- General and Work model namespaces remain separate. `chat`/`agent` validate their selected general model; `chat` validates optional `thinking_effort`, and omitted effort stays omitted from the payload.
- Ordinary `BackendClient` REST/JSON requests share one process policy (default 4 in flight, configured range 1-8); route-local 429 cooldowns are capped at 60 seconds. Direct SSE/Sentinel streams use endpoint timeouts instead and heavy Deep Research stays serial.
- Streamable HTTP is loopback-only and uses native MCP Host/Origin protection. There is no remote bypass.
- Capability probes and static resources must never return account content, credentials, raw backend envelopes, or Voice/realtime data.
- Never commit credentials, tokens, or `.env` files.
- `BackendClient` is synchronous. Async tools must wrap sync calls in `asyncio.to_thread()`.

## Adding Tools

1. Create `gpt2agent/tools/<name>.py` with `register(mcp, client, conv=None)`.
2. Add all four hints to `TOOL_ANNOTATION_MANIFEST` and register the tool with `tool_annotations(name)`.
3. Add it to `tools/__init__.py` `register_all()`; `tool_manifest.TOOL_NAMES` is derived from the annotation manifest.
4. SSE-based tools: use the `conv` singleton (passed from server.py).
5. REST-based tools: use the shared async backend helper and an explicit bounded adapter; never expose a raw response.
6. Add exact manifest, annotation, contract, and package-smoke tests. New resources register through `resources.py` and must be deterministic, static, and non-secret.
