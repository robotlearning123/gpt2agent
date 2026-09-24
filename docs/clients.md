# Client setup

gpt2agent speaks MCP over **stdio**, which every popular host supports. `gpt2agent
install` auto-writes the right config for the clients below; the last two (VS Code,
Cline) are easy manual additions.

All auto-installed configs are **idempotent** and back up the prior file as
`<name>.bak-gpt2agent`. After installing, **restart the client** so it spawns the
server.

## Auto-installed (`gpt2agent install --client <name>`)

| Client | `--client` | Config file | Restart needed |
|---|---|---|---|
| Claude Code | `claude-code` | `~/.claude.json` | yes |
| Codex CLI | `codex` | `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`) | no (next run) |
| Cursor | `cursor` | `~/.cursor/mcp.json` | yes |
| Windsurf | `windsurf` | `~/.codeium/windsurf/mcp_config.json` | yes |
| Claude Desktop | `claude-desktop` | platform Claude config | yes |
| Zed | `zed` | `~/.config/zed/settings.json` | yes |

### The stdio entry these write

Most hosts use the `mcpServers` shape:

```json
{
  "mcpServers": {
    "gpt2agent": { "command": "gpt2agent", "args": ["run", "--stdio"] }
  }
}
```

Codex (`$CODEX_HOME/config.toml`, default `~/.codex/config.toml`):

```toml
[mcp_servers.gpt2agent]
command = "gpt2agent"
args = ["run", "--stdio"]
```

Zed nests the command under `context_servers`:

```json
{
  "context_servers": {
    "gpt2agent": { "command": { "path": "gpt2agent", "args": ["run", "--stdio"] }, "settings": {} }
  }
}
```

## Multiple ChatGPT accounts in one clientToken selection follows `CODEX_HOME` (default `~/.codex`), so a second
account is a second server entry with its own `env` — both run side by side
in the same client, each with its own token, quota, and rate-limit budget:

```json
{
  "mcpServers": {
    "gpt2agent":   { "command": "gpt2agent", "args": ["run", "--stdio"] },
    "gpt2agent-b": {
      "command": "gpt2agent",
      "args": ["run", "--stdio"],
      "env": { "CODEX_HOME": "/home/you/.codex-second-account" }
    }
  }
}
```

Log the second account in once with
`CODEX_HOME=~/.codex-second-account codex login`, then restart the client.
Tools are identical under both entries — pick the entry (and thus the
account) by which server you call.

## Claude Code plugin

Instead of `gpt2agent install --client claude-code`, you can install via the plugin
marketplace (bundles the MCP server registration + both skills):

```text
/plugin marketplace add robotlearning123/gpt2agent
/plugin install gpt2agent@gpt2agent
```

You still need the `gpt2agent` CLI on PATH (`pipx install gpt2agent`) — the plugin
wires `gpt2agent run --stdio` and the skills, not the Python package.

## MCP registries

`server.json` (repo root) is the [official MCP registry](https://registry.modelcontextprotocol.io)
descriptor (PyPI package `gpt2agent`, stdio). Glama / mcp.so / PulseMCP auto-index
from GitHub (topics + README). Publishing to the official registry is an owner step
(`mcp-publisher` with GitHub auth).

## Manual setup

### VS Code (GitHub Copilot MCP)

Add to `.vscode/mcp.json` in your workspace (note the top-level key is `servers`):

```json
{
  "servers": {
    "gpt2agent": { "type": "stdio", "command": "gpt2agent", "args": ["run", "--stdio"] }
  }
}
```

### Cline / Roo Code

Add the same `mcpServers` block (see above) to Cline's settings file
(`cline_mcp_settings.json`, reachable from the Cline MCP settings UI).

### Any other MCP host (generic stdio)

Point it at the command `gpt2agent` with args `["run", "--stdio"]`. That's all most
hosts need.

## HTTP transport (advanced, not recommended)

stdio is the default and safest. The HTTP transport is **unauthenticated** and
proxies your full account, so it binds `127.0.0.1` only and refuses non-loopback
hosts unless you set `GPT2AGENT_ALLOW_REMOTE=1` (put it behind your own auth proxy).
See the README's **Security & risk** section.

## Timeouts

gpt2agent tool calls can run long: light `deep_research` tens of seconds,
`deep_research_heavy` up to 30 minutes (server-side `max_wait` 1800 s). Check
your client's tool-call timeout if heavy runs get cut off.

- **Claude Code:** `MCP_TIMEOUT` (server startup, ms, default 30000) and
  `MCP_TOOL_TIMEOUT` (tool execution, ms, default 100000000 ≈ 28 h — heavy DR
  fits the default). A `.mcp.json` per-server `timeout` field overrides
  `MCP_TOOL_TIMEOUT`; HTTP/SSE/connector servers have a separate 60 s
  per-request cap unless the value is raised above 60000.
  Source: code.claude.com/docs env-vars reference (verified 2026-09-24).
- Other clients: look for a per-server or global tool timeout setting and
  raise it above 1800 s for heavy DR.
