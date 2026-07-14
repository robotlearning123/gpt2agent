# Quickstart

Get gpt2agent running in any MCP client in ~5 minutes.

## 1. Prerequisites

- An active **ChatGPT Plus or Pro** subscription.
- **Python 3.10+** and **pipx** (`pip install --user pipx`, or `apt/dnf/brew install pipx`).
- The easiest auth path: the [`codex`](https://github.com/openai/codex) CLI, logged in
  (`codex login`). gpt2agent reuses `$CODEX_HOME/auth.json` (or
  `~/.codex/auth.json` by default) — no separate token to paste.

## 2. Install

```bash
curl -fsSL https://raw.githubusercontent.com/robotlearning123/gpt2agent/main/install.sh | bash
```

This installs the `gpt2agent` package (via pipx) and registers it with every MCP
client it detects. Or do it by hand:

```bash
pipx install gpt2agent
gpt2agent install            # auto-detect & register all installed clients
```

Target one client explicitly:

```bash
gpt2agent install --client claude-code   # or: codex, cursor, windsurf, claude-desktop, zed
```

See [clients.md](./clients.md) for per-client config details and clients that need
manual setup (VS Code, Cline).

## 3. Authenticate

If you've run `codex login`, you're done — gpt2agent picks up the token automatically
(and reloads it when codex refreshes it).

No codex? Run `gpt2agent setup` to paste a ChatGPT token once
(saved to `~/.gpt2agent/token.json`, mode `600`).

For Grok repository coding, install and authenticate the official Grok Build
CLI separately using xAI's [enterprise/authentication
guidance](https://docs.x.ai/build/enterprise). Do not paste its credential into
gpt2agent. Add an explicit root to `~/.gpt2agent/config.toml`; the empty default
disables all Build probes and actions:

```toml
[grok_build]
command = "grok"
roots = ["/absolute/path/to/repository-root"]
timeout_seconds = 120
max_output_bytes = 1048576
default_max_turns = 20
```

Start the MCP server from within that root. ChatGPT auth and Grok Build OAuth
remain independent and never fall back to one another.

## 4. Verify

```bash
gpt2agent --version          # 0.0.12 prints: gpt2agent 0.0.12
gpt2agent run --stdio        # smoke test: should start and wait on stdin (Ctrl-C to exit)
```

Then **restart your MCP client** (Claude Code spawns the server fresh on restart;
Codex picks it up on next run). Ask your agent to call `account_status`, then
list all registered tools and resources. The ChatGPT provider exposes the static
`chatgpt://feature-coverage` and `chatgpt://update-evidence` resources. For the
official CLI lane, call read-only `grok_build_status` and `grok_build_models`
only after configuring a root.

## 5. First calls

- `chat` — talk to any model on your account (`model="gpt-5-5-pro"`, `o3-pro`, …).
- `deep_research` — web-augmented research with citations (~1 min).
- `generate_image` — ChatGPT's built-in image generation through the observed,
  undocumented private prepare/conduit flow.
- `account_capabilities` — get shape-only, explicit current account truth.
- `list_scheduled_tasks` — inspect scheduled automations; use `list_tasks` for
  generic background jobs.
- `grok_build_agent` — repository coding through the official CLI. Use the
  default `mode="plan"`; select `mode="apply"` only when the user explicitly
  authorizes source changes. The tool is always annotated destructive.

The CLI retains its own session history. gpt2agent returns a sanitized session
ID but does not copy transcripts or expose resume/deletion operations. See the
official [CLI reference](https://docs.x.ai/build/cli/reference), [headless
scripting](https://docs.x.ai/build/cli/headless-scripting), and [modes and
commands](https://docs.x.ai/build/modes-and-commands) documentation.

> **Heads up:** `chat` defaults to `temporary=True`, which disables image gen / code
> interpreter / canvas. Use the dedicated tools (`generate_image`, `code_interpreter`,
> `canvas_execute`) for those — they handle the flag for you.

Stuck? See [troubleshooting.md](./troubleshooting.md). Worried about account safety?
See the README's **Security & risk** section and [faq.md](./faq.md). Upgrading an
older service? Read [the 0.0.12 migration guide](./migration-0.0.12.md).
