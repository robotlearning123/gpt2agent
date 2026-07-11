# Configuration

## Config file

Optional. Searched in this order (first found wins):

1. `~/.gpt2agent/config.toml`
2. `./config.toml` (current directory)
3. `~/.config/gpt2agent/config.toml` (XDG)

```toml
[server]
# Retained for config compatibility. Version 0.0.12 uses stdio and does not bind.
host = "127.0.0.1"
port = 9000

[models]
chat     = "gpt-5-3"      # default model for the `chat` tool
agent    = "agent-mode"   # default for the `agent` tool
heavy_dr = "gpt-5-5-pro"  # override slug for `deep_research_heavy`
```

`chat` and `agent` validate their selected slug against the live general model
catalog. Optional `thinking_effort` values must appear in that model's
`thinking_efforts`; leaving the value unset preserves the model default. Work
models from `list_work_models` remain a separate namespace.

The host and port fields are inert under the supported stdio transport.

## Environment variables

| Var | Effect |
|---|---|
| `CODEX_HOME` | Use a non-default Codex directory, e.g. `~/.codex-work`, for multi-account setups. Honored for token loading, `setup`, Codex client detection, and registration in `$CODEX_HOME/config.toml`. |
| `GPT2AGENT_MAX_IN_FLIGHT` | Process-wide concurrency limit for ordinary REST/JSON backend requests. Must be an integer from 1 through 8; default 4. Permit acquisition waits at most one second. |

Legacy remote-bind and raw-dump variables were removed in 0.0.12 and now fail
closed. See [the migration guide](./migration-0.0.12.md) before upgrading an
older service definition.

## Transports

- **stdio** (default, recommended): local, not network-exposed. Plain
  `gpt2agent run` uses stdio, and this is what `gpt2agent install` wires by
  default for every client.
- **Network transport:** disabled in 0.0.12 because loopback TCP cannot isolate
  a full account from other local users or processes. Legacy launch and install
  requests fail before server construction or configuration writes.

Ordinary REST/JSON backend requests share the process limit. A 429 activates a
cooldown only for the normalized route that returned it; valid `Retry-After`
values are capped at 60 seconds. Direct SSE/Sentinel streams are not held by
this semaphore; endpoint timeouts bound them, and heavy Deep Research should run
serially. The policy is process-local, not a cross-process account rate limiter.
