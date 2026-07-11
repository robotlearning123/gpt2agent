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

## 4. Verify

```bash
gpt2agent --version          # 0.0.12 prints: gpt2agent 0.0.12
gpt2agent run --stdio        # smoke test: should start and wait on stdin (Ctrl-C to exit)
```

Then **restart your MCP client** (Claude Code spawns the server fresh on restart;
Codex picks it up on next run). Ask your agent to call `account_status`, then
list the server's MCP tools and resources. Version 0.0.12 exposes exactly 32
tools plus the static `chatgpt://feature-coverage` and
`chatgpt://update-evidence` resources.

## 5. First calls

- `chat` — talk to any model on your account (`model="gpt-5-5-pro"`, `o3-pro`, …).
- `deep_research` — web-augmented research with citations (~1 min).
- `generate_image` — ChatGPT's built-in image generation through the observed,
  undocumented private prepare/conduit flow.
- `account_capabilities` — get shape-only, explicit current account truth.
- `list_scheduled_tasks` — inspect scheduled automations; use `list_tasks` for
  generic background jobs.

> **Heads up:** `chat` defaults to `temporary=True`, which disables image gen / code
> interpreter / canvas. Use the dedicated tools (`generate_image`, `code_interpreter`,
> `canvas_execute`) for those — they handle the flag for you.

Stuck? See [troubleshooting.md](./troubleshooting.md). Worried about account safety?
See the README's **Security & risk** section and [faq.md](./faq.md). Upgrading an
older service? Read [the 0.0.12 migration guide](./migration-0.0.12.md).
