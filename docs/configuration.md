# Configuration

## Config file

Optional. Searched in this order (first found wins):

1. `~/.gpt2agent/config.toml`
2. `./config.toml` (current directory)
3. `~/.config/gpt2agent/config.toml` (XDG)

```toml
[server]
# Loopback only. The HTTP transport is UNAUTHENTICATED and proxies your full
# ChatGPT account — only bind a public host behind your own auth proxy AND with
# GPT2AGENT_ALLOW_REMOTE=1 set.
host = "127.0.0.1"
port = 9000

[models]
chat     = "gpt-5-6"      # default model for the `chat` tool
agent    = "agent-mode"   # default for the `agent` tool
heavy_dr = "gpt-6-pro"  # override slug for `deep_research_heavy`
```

CLI flags override the file: `gpt2agent run --host 127.0.0.1 --port 9001`.

## Environment variables

| Var | Effect |
|---|---|
| `CODEX_HOME` | Use a non-default Codex directory, e.g. `~/.codex-work`, for multi-account setups. Honored for token loading, `setup`, Codex client detection, and registration in `$CODEX_HOME/config.toml`. |
| `GPT2AGENT_ALLOW_REMOTE=1` | Required to bind the HTTP transport to a non-loopback host. Without it, gpt2agent refuses to start HTTP on anything but loopback. Only use behind your own auth/firewall. |
| `GPT2AGENT_RAW_DUMP=<path>` | **Debug only.** Appends raw SSE/poll traffic — including prompts, responses, and resume tokens — to the given file, **unredacted**. On POSIX systems the file is created or tightened to mode `0600`; use an ignored name such as `gpt2agent-raw-dump.jsonl` and delete it afterward. |

## Transports

- **stdio** (default, recommended): local, not network-exposed. What `gpt2agent
  install` wires for every client.
- **streamable-http** (`gpt2agent run`, no `--stdio`): unauthenticated; loopback by
  default; see `GPT2AGENT_ALLOW_REMOTE` above and the README **Security & risk** section.
