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
proxies your full account, so it binds loopback only and always refuses a
non-loopback host. There is no remote override. The server also enables native
MCP Host and Origin validation to reject non-loopback DNS-rebinding attempts.
See the README's **Security & risk** section.

Only Claude Code URL registration is automated:

```bash
gpt2agent install --client claude-code --transport http --http-port 9000
gpt2agent run --http --port 9000
```

The first command writes the URL entry. It does not start or supervise the
second command. Keep the server running separately before restarting Claude
Code. HTTP installation for Codex, Cursor, Windsurf, Claude Desktop, Zed, or a
mixed auto-detected target set fails before any configuration is written.
