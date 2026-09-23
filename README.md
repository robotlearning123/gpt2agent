# gpt2agent

<!-- mcp-name: io.github.robotlearning123/gpt2agent -->

> **MCP server for your ChatGPT account: `codex login` → ChatGPT Plus/Pro inside any MCP client.**

An **MCP server** that puts your **ChatGPT Plus or Pro** subscription — every model
and the account-tier features below — inside Claude Code, Codex, Cursor, Windsurf,
Zed, and any MCP client.

[![PyPI version](https://img.shields.io/pypi/v/gpt2agent)](https://pypi.org/project/gpt2agent/)
[![Downloads](https://static.pepy.tech/badge/gpt2agent)](https://pepy.tech/projects/gpt2agent)
[![Downloads/month](https://img.shields.io/pypi/dm/gpt2agent)](https://pypistats.org/packages/gpt2agent)
[![CI](https://github.com/robotlearning123/gpt2agent/actions/workflows/ci.yml/badge.svg)](https://github.com/robotlearning123/gpt2agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/gpt2agent/)

📖 **[Quickstart](./docs/quickstart.md)** · **[Client setup](./docs/clients.md)** · **[Troubleshooting](./docs/troubleshooting.md)** · **[FAQ](./docs/faq.md)** · **[Docs index](./docs/README.md)** · **[Account safety](./docs/account-safety.md)**

---

## Status — REST conversations restored (checked 2026-09-23, v0.0.21)

> Re-checked 2026-09-23 against a live account: conversation tools were last
> live-verified end-to-end 2026-09-17 and `chat` was re-probed 2026-09-23.
> Re-check your own account any time with **`gpt2agent doctor`** (below).

The Sentinel/Turnstile challenge that blocked conversation tools since 2026-09-08
is now solved via a **sentinel bridge** (see below). **24 of 29 probed surfaces
pass `gpt2agent doctor`** (2026-09-23, v0.0.21); the rest are documented below.

| State | Tools |
|---|---|
| ✅ **Working (doctor-verified 2026-09-23, v0.0.21; conversation tools live-verified 2026-09-17, chat re-probed 2026-09-23)** | `chat` (gpt-6-pro ✅, gpt-5-6 ✅), `agent` ✅, `deep_research` ⚠ (upstream failure since 2026-09-23 — see Limitations), `code_interpreter` ✅, `generate_image` ✅, `list_models` (23), `account_status`, `list_conversations` (5), `get_conversation`, `list_custom_gpts` (0), `memory_list` (5), `memory_search`, `list_apps` (107), `list_codex_envs` (0), `list_codex_tasks` (0), `list_tasks` (1), `custom_instructions_get`, `account_limits`, `rate_limit`, `sentinel (bridge)` |
| ⚠ **Known limitations** | `chat(<Work-only slug>)` — GPT-6 Sol/Luna are Work & Codex-only; on the Chat surface the backend silently resolves their slugs to `gpt-5-6` and the tool appends a **Model note** (measured 2026-09-23); `gpt_chat` — 422 with `g-p-` prefix GPTs (public/store); `memory_create_via_chat` — model does not reliably invoke memory tool; `deep_research_heavy` — connector-dependent, may need Settings → Connectors → Deep Research enabled |
| ❓ Unverified | `custom_instructions_set`, `codex_task_create` — plain REST writes; `get_file_info`, `get_file_download_url` — need a `file_id`; not probed read-only |
| 🔇 **Fallback available** | All conversation tools support `manual=True` (zero-network handoff) and `browser=True` (real Chrome via `[browser]` extra) |

### The sentinel bridge

The upstream Sentinel challenge requires a bytecode-VM Turnstile token that the
built-in solver cannot produce. The **bridge** loads a solver from an
owner-supplied directory and mints the full header set in one consistent
session. It has **retry with backoff** (3 attempts) for transient failures.

**Setup** (one time):
```bash
# Place the bridge at ~/.gpt2agent/sentinel-bridge/ and create the marker:
mkdir -p ~/.gpt2agent/sentinel-bridge
touch ~/.gpt2agent/sentinel-bridge/ENABLED
```

The bridge directory must contain `wrapper/reverse/vm.py` (bytecode-VM turnstile
solver). Dependencies: `pip install esprima pillow colorama` (bridge internals). It is NOT part of this distribution — see
[docs/dev/specs/sentinel-vm.md](./docs/dev/specs/sentinel-vm.md) for the
from-scratch interpreter spec that will eventually replace the bridge.

**Control**:
- `GPT2AGENT_SENTINEL_BRIDGE=/path/to/bridge` — explicit bridge directory
- `GPT2AGENT_SENTINEL_BRIDGE_OFF=1` — force the legacy gate path (offline/test)
- The `ENABLED` marker file beside the bridge enables it persistently

### `gpt2agent doctor`

```bash
gpt2agent doctor
```

Probes each read-only surface and prints a status table plus a one-line summary.
It never sends a message, never creates a conversation, and never spends quota.

---

## What it does

gpt2agent exposes **30 MCP tools** that forward requests directly to ChatGPT's backend API.
No proxy process. No separate account. No platform API key. Your `codex login`,
your token, your quota.

If you already have the [`codex`](https://github.com/openai/codex) CLI logged in,
setup is **zero extra steps** — gpt2agent reuses `$CODEX_HOME/auth.json` (or
`~/.codex/auth.json` by default) and picks up its background-refreshed token
automatically.

Works with Claude Code, Codex CLI, and any client that speaks the MCP protocol over stdio.

---

## Install — one line

```bash
curl -fsSL https://raw.githubusercontent.com/robotlearning123/gpt2agent/main/install.sh | bash
```

That command:
1. Installs the published `gpt2agent` package via pipx in an isolated environment.
2. Reuses `$CODEX_HOME/auth.json` (or `~/.codex/auth.json`) if you've run `codex login` — no separate ChatGPT token paste needed.
3. Detects which MCP clients you have (Claude Code, Codex, Cursor, Windsurf, Claude Desktop, Zed) and writes the right config for each, honoring `CODEX_HOME` for Codex.
4. Drops the Claude Code skills (`deep-research` + `gpt2agent`) into `~/.claude/skills/`.

### Or step-by-step

```bash
# 1. Install the package globally (isolated venv)
pipx install gpt2agent

# 2. Register with all detected MCP clients (Claude Code, Codex)
gpt2agent install                          # auto-detect everything

# Want only one client?
gpt2agent install --client claude-code   # or: codex, cursor, windsurf, claude-desktop, zed
# (VS Code & Cline: see docs/clients.md for the manual snippet)

# HTTP transport instead of stdio?
gpt2agent install --transport http --http-port 9000
```

### Or as a Claude Code plugin

```text
/plugin marketplace add robotlearning123/gpt2agent
/plugin install gpt2agent@gpt2agent
```

This bundles the MCP server registration + both skills in one step. You still need
the `gpt2agent` CLI on PATH (`pipx install gpt2agent`) — the plugin wires the server
(`gpt2agent run --stdio`) and skills, not the Python package itself.

### Browser transport (optional)

```bash
pip install "gpt2agent[browser]"
```

```toml
# ~/.gpt2agent/config.toml
[browser]
enabled = true
headed = true                # visible Chrome (recommended; Turnstile needs it)
# profile_dir = "/custom/path"  # default: ~/.gpt2agent/chrome-profile
# timeout_s = 180
```

First launch opens a visible Chrome window for a **one-time login** — after that,
all 9 conversation tools work through the browser without any human steps.

### Manual config (if you'd rather not run install)

Claude Code — add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "gpt2agent": {
      "type": "stdio",
      "command": "gpt2agent",
      "args": ["run", "--stdio"]
    }
  }
}
```

Codex CLI — add to `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`):

```toml
[mcp_servers.gpt2agent]
command = "gpt2agent"
args = ["run", "--stdio"]
```

---

## Setup (manual token paste — only if codex isn't available)

```bash
gpt2agent setup
```

Prompts for a ChatGPT session token (saved to `~/.gpt2agent/token.json`, mode
`600`), detects your plan, and registers gpt2agent with your detected MCP clients
over **stdio** — the same wiring as `gpt2agent install`. The `codex login` flow is
preferred when available because codex auto-refreshes its token; gpt2agent reloads
the selected Codex auth file on mtime change so long calls don't 401 mid-flight.

---

## Tools (30)

> The tables below document the 25 conversation/account tools that `gpt2agent
> doctor` covers. The other five — `usage_stats` and the queue tools
> `queue_submit`, `queue_status`, `queue_result`, `queue_cancel` — are
> registered but not yet documented here; a `tools/list` call lists all 30.

### Chat & reasoning

| Tool | Parameters | What it does | Status |
|---|---|---|---|
| `chat` | `prompt`, `model`, `temporary`, `manual`, `browser` | Talk to any model on your account (`gpt-5-6` = GPT-5.6 Sol, default; override via `model=`). Pass `gpt-6-pro` (410K), `gpt-5-6-thinking` (262K), `o3-pro` (196K), … | ✅ **live-verified** (gpt-6-pro, gpt-5-6) |
| `agent` | `prompt`, `manual`, `browser` | **Agent Mode** — 262K context with autonomous browsing, code execution, tool use | ✅ **live-verified** |
| `deep_research` | `query`, `auto_confirm`, `manual`, `browser` | Web-augmented research with **inline `[N](url)` citations** (~30–120 s) | ✅ **live-verified** (incl. citations) |
| `deep_research_heavy` | `query`, `auto_confirm`, `manual`, `browser` | Long-form DR via `gpt-6-pro` + connector (5–30 min, monthly quota) | ⚠ connector-dependent |
| `gpt_chat` | `gizmo_id`, `prompt`, `manual`, `browser` | Talk through one of your Custom GPTs — *experimental* (`g-` prefix verified; `g-p-` store GPTs return 422) | ⚠ partial |

### Transport modes (every conversation tool)

| Mode | How | When |
|---|---|---|
| **REST** (default) | `chat("hi")` — direct backend call via sentinel bridge | Fastest; needs bridge set up |
| **Browser** | `chat("hi", browser=True)` — drives a real Chrome | Most reliable; needs one-time login + `[browser]` extra |
| **Manual** | `chat("hi", manual=True)` — returns a JSON handoff with the exact prompt, URL, and readback steps | Zero-network fallback; you paste into chatgpt.com yourself |

### Image & code execution

| Tool | What it does | Status |
|---|---|---|
| `generate_image` | Generate images via ChatGPT's built-in DALL-E. Returns download URLs + metadata (uses `temporary=False` internally) | ✅ **live-verified** |
| `code_interpreter` | Run Python in ChatGPT's sandbox. Returns output + charts/images (uses `temporary=False` internally) | ✅ **live-verified** |
| `canvas_execute` | Canvas was retired upstream (2026-05) — the tool returns the model's deprecation notice; use `code_interpreter` | ⚠ upstream-retired |
| `get_file_info` | Metadata for any ChatGPT file (needs a `file_id`) | ✅ |
| `get_file_download_url` | Temporary download URL (~1h expiry; needs a `file_id`) | ✅ |

### Account introspection

| Tool | What it does | Status |
|---|---|---|
| `account_status` | Plan, country, groups, feature count, subscription expiry | ✅ |
| `list_models` | All models (slug, max_tokens, reasoning_type, capabilities, thinking_efforts) | ✅ (23 models, 2026-09-23) |
| `list_conversations` | Recent conversations (titles: emails/phones redacted); `limit` parameter | ✅ |
| `get_conversation` | Full message history (multimodal, code, images, DR widget-state reports) | ✅ |
| `list_tasks` | Scheduled / completed ChatGPT tasks | ✅ |
| `list_apps` | Connected apps + connectors (bare-id shape with type classification) | ✅ (107) |
| `list_custom_gpts` | Your private GPTs (id, display_name, description, short_url) | ✅ (0) |

### Memory & instructions

| Tool | What it does | Status |
|---|---|---|
| `memory_list` | List all ChatGPT memories | ✅ (5) |
| `memory_search` | Keyword filter over memories (`query` parameter) | ✅ |
| `memory_create_via_chat` | Add a memory (model-initiated workaround — POST `/memories` is 405) | ⚠ model-dependent |
| `custom_instructions_get` | Read your current `about_user` / `about_model` | ✅ |
| `custom_instructions_set` | Update them (read-modify-write) | ❓ unverified |

### Codex (cloud agent)

| Tool | What it does | Status |
|---|---|---|
| `list_codex_envs` | Codex environments (label, repos, network policy) | ✅ |
| `list_codex_tasks` | Recent Codex tasks + status | ✅ |
| `codex_task_create` | Kick off a new Codex task (resolves env from `repo_label`) | ❓ unverified |

---

## Architecture

Native Python implementation — no proxy. The server calls
`/backend-api/conversation` (SSE) directly using `curl_cffi` for TLS
impersonation. The **sentinel bridge** mints the full OpenAI Sentinel header
set (fingerprint-config `p` + PoW + bytecode-VM Turnstile token) in one
consistent session, with retry-with-backoff for transient failures.

```
$CODEX_HOME/auth.json (default ~/.codex/auth.json) ← auto-refreshed by Codex
~/.gpt2agent/token.json                            ← manual fallback
        |
   gpt2agent  (stdio MCP server, token reloaded on each call)
        |
   ┌────────────────────────────────────────────────┐
   │ Transport selection (per tool call)             │
   │                                                  │
   │  REST (default)    Browser (browser=True)       │
   │  ↓                    ↓                         │
   │  sentinel_bridge     Playwright + Chrome        │
   │  (fingerprint p +    (real page, one-time       │
   │   PoW + VM token)     login, [browser] extra)   │
   │  ↓                    ↓                         │
   │  curl_cffi → chatgpt.com/backend-api/*          │
   └────────────────────────────────────────────────┘
        |
   30 MCP tools  (chat, agent, DR ×2, GPT chat, image gen,
                  code interpreter, canvas, memory r/w,
                  instructions r/w, codex r/w, account introspect)
```

### Citations

Deep Research replies include **inline `[N](url)` citation anchors** when the
model performs web searches (knowledge-only answers have no citations; numbering
may repeat for multiple URLs).
The `gpt2agent/citations.py` module rewrites raw `citeturn…` markers
(private-use unicode) from the stream's `content_references` mapping into
clickable markdown links, with a Sources section appended.

---

## Configuration

Optional, searched in order: `~/.gpt2agent/config.toml`, `./config.toml`,
`~/.config/gpt2agent/config.toml`. Full reference: [docs/configuration.md](./docs/configuration.md).

```toml
[server]
host = "127.0.0.1"   # loopback only; the HTTP transport is UNAUTHENTICATED
port = 9000

[models]
chat     = "gpt-5-6"        # default for chat tool (GPT-5.6 Sol; e.g. "gpt-6-pro")
agent    = "agent-mode"     # default for agent tool
heavy_dr = "gpt-6-pro"      # override slug for deep_research_heavy

[browser]
enabled  = false             # opt-in; requires "gpt2agent[browser]" extra
headed   = true              # visible Chrome (recommended for Turnstile)
# profile_dir = "~/.gpt2agent/chrome-profile"
# timeout_s   = 180
```

---

## Account safety

See [docs/account-safety.md](./docs/account-safety.md) for the full design.
Key rules:

- **One browser profile, forever** — never copy cookies between contexts
- **Human pacing** — min 20s between turns, burst cap, daily budget
- **Challenge = backoff**, never brute force
- **Temporary chats by default** (smaller account surface)

---

## Limitations

- **`gpt_chat`** with `g-p-` prefix GPTs (public/store) returns 422 — the
  `conversation_origin` payload was reverse-engineered for `g-` prefix only.
- **`chat(<Work-only slug>)`** — GPT-6 Sol and GPT-6 Luna are served on ChatGPT **Work and Codex only** (not Chat). Measured 2026-09-23: Chat-surface requests for `gpt-6-sol`, `gpt-6-luna`, `gpt-6-sol-wm`, or `gpt-6-luna-wm` are silently served by `gpt-5-6`, and the tool appends a *Model note* naming the resolved slug. Use `gpt-6-pro` for the deepest Chat model.
- **`deep_research` (light)** is failing upstream as of 2026-09-23: the
  research turn is accepted, then aborted with an in-band
  `Error in message stream` and never persisted (no DR quota is consumed).
  `deep_research_heavy` is the working research path until this recovers.
- **`canvas_execute`** — Canvas was retired upstream (2026-05); the tool now
  returns the model's deprecation notice. Use `code_interpreter`.
- **`memory_create_via_chat`** depends on the model choosing to invoke the
  memory tool; it doesn't always do so from a plain-text prompt.
- **`deep_research_heavy`** depends on the DR connector — check
  chatgpt.com → Settings → Connectors → Deep Research is enabled.
- **Deep Research quota:** limits and reset timing are account-reported.
- **Account-tier features not yet supported:** Sora video, Operator/CUA, voice
  sessions, Projects, Tasks (write), file upload.
- Requires an active ChatGPT Plus or Pro subscription.

---

## Security & risk — read before you run this

gpt2agent talks to ChatGPT's **private** backend the way the web app does. That
has real consequences; please understand them before pointing it at your account.

- **It impersonates the chatgpt.com web client.** It uses `curl_cffi` TLS
  fingerprint impersonation and a sentinel bridge to pass the OpenAI Sentinel
  challenge. This is **very likely against the OpenAI Terms of Service**, and
  automated/abnormal traffic can get your account **rate-limited, challenged,
  suspended, or banned**. Use an account you can afford to lose, keep volume
  human-scale, and don't rely on it for anything critical.
- **The HTTP transport is UNAUTHENTICATED.** Use stdio (the default).
- **Your token stays local.** Read from `$CODEX_HOME/auth.json` with
  `~/.gpt2agent/token.json` as fallback. Sent only to `chatgpt.com`.
- **PII redaction is limited.** Emails, phones, and secret shapes are masked;
  everything else is returned verbatim.
- **`GPT2AGENT_RAW_DUMP`** (debug) writes raw unredacted traffic to the given
  path. Use mode-600 file names and delete after debugging.

Found a security issue? See [SECURITY.md](./SECURITY.md).

---

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest                              # 513+ tests, live tests auto-skip (SKIP_LIVE)
python -m ruff check gpt2agent tests # lint
```

### Release

1. Prepare a release PR: bump version in 4 files (`pyproject.toml`,
   `gpt2agent/__init__.py`, `.claude-plugin/plugin.json`, `server.json`) +
   dated `CHANGELOG.md` entry.
2. Verify: `python scripts/verify_release.py`
3. After merge, tag the merge SHA and push: `git tag -a v$VERSION $SHA -m "gpt2agent $VERSION" && git push origin v$VERSION`
4. CI publishes to PyPI (trusted publishing) + creates a GitHub Release.
5. Full runbook: [docs/release-validation.md](./docs/release-validation.md)

After the release PR is merged, read its exact merge SHA, prove that commit is
on `origin/main`, check out that reviewed tree, then create and push only the
intended annotated tag:

```bash
set -euo pipefail
git fetch --no-tags origin main:refs/remotes/origin/main
read -r -p "Merged release PR number: " PR_NUMBER
RELEASE_SHA=$(gh pr view "$PR_NUMBER" --json mergeCommit,state   --jq 'select(.state == "MERGED") | .mergeCommit.oid')
test -n "$RELEASE_SHA"
git merge-base --is-ancestor "$RELEASE_SHA" origin/main
test -z "$(git status --porcelain)"
git switch --detach "$RELEASE_SHA"
trap 'git switch - >/dev/null || true' EXIT
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
TAG="v$VERSION"
python scripts/verify_release.py --tag "$TAG"
REMOTE_TAG_SHA="$(git ls-remote --tags origin | awk -v ref="refs/tags/$TAG" '$2 == ref { print $1 }')"
if [ -n "$REMOTE_TAG_SHA" ]; then
  echo "Release tag already exists on origin: $TAG" >&2
  exit 1
fi
git tag -a "$TAG" "$RELEASE_SHA" -m "gpt2agent $VERSION"
git push origin "refs/tags/$TAG"
trap - EXIT
git switch -
```

If a publish or downstream release job fails, use GitHub Actions' **Re-run
failed jobs** on that same workflow run so it reuses the original build
artifact. Do not re-run the whole workflow after any file reaches PyPI.

If a publish or downstream release job fails, use GitHub Actions' **Re-run
failed jobs** on that same workflow run so it reuses the original build
artifact. Do not re-run the whole workflow after any file reaches PyPI: Python
sdists are not guaranteed byte-reproducible, and the hash guard intentionally
rejects different rebuilt bytes for an existing version.

### Testing

- **Offline**: `pytest` — 513+ unit/contract tests (zero network)
- **Live verification matrix**: `scripts/agent-user-journey.sh <worktree>` — 15 cases
- **Release emulation gate**: `scripts/release-emulation-test.sh <worktree>` — 11 checks
- **Parameter contracts**: `tests/test_param_matrix.py` — 34 cases

---

## License

[MIT](./LICENSE). See [NOTICES](./NOTICES.md) for third-party attributions.

---

## Acknowledgments

- [lanqian528/chat2api](https://github.com/lanqian528/chat2api) — POW solver (MIT)
- Sentinel bridge technique studied from public reverse-engineering (2026); see
  [docs/dev/specs/sentinel-vm.md](./docs/dev/specs/sentinel-vm.md) for the
  from-scratch interpreter spec.
- [basketikun/chatgpt2api](https://github.com/basketikun/chatgpt2api) — survey of ChatGPT backend API patterns
