# gpt2agent

<!-- mcp-name: io.github.robotlearning123/gpt2agent -->

> **MCP server for your ChatGPT account: `codex login` → ChatGPT Plus/Pro inside any MCP client.**

An **MCP server** that puts your **ChatGPT Plus or Pro** subscription — every model
and the account-tier features below — inside Claude Code, Codex, Cursor, Windsurf,
Zed, and any MCP client.

[![PyPI version](https://img.shields.io/pypi/v/gpt2agent)](https://pypi.org/project/gpt2agent/)
[![CI](https://github.com/robotlearning123/gpt2agent/actions/workflows/ci.yml/badge.svg)](https://github.com/robotlearning123/gpt2agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/gpt2agent/)

📖 **[Quickstart](./docs/quickstart.md)** · **[Client setup](./docs/clients.md)** · **[Troubleshooting](./docs/troubleshooting.md)** · **[FAQ](./docs/faq.md)** · **[Roadmap](./docs/roadmap.md)** · **[Docs index](./docs/README.md)**

---

## What it does

gpt2agent exposes **26 MCP tools** that forward requests directly to ChatGPT's backend API.
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

### Per-client config

The `install` subcommand writes the right thing for each:

| Client | File | Section |
|---|---|---|
| **Claude Code** | `~/.claude.json` | `mcpServers.gpt2agent` (stdio: `gpt2agent run --stdio`) |
| **Codex CLI** | `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`) | `[mcp_servers.gpt2agent]` |

Both are idempotent and back up the prior file as `<name>.bak-gpt2agent`.

After running `install`, restart Claude Code so it re-spawns the subprocess.
Codex picks up the new server on its next invocation automatically.

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

## Tools (26)

### Chat & reasoning

| Tool | What it does |
|---|---|
| `chat` | Talk to any model on your account (`gpt-5-3` default, override via `model=`). Pass `gpt-5-5-pro`, `o3-pro`, `gpt-5-4-thinking`, … |
| `agent` | **Agent Mode** — 262K context with autonomous browsing, code execution, tool use |
| `deep_research` | Web-augmented research with citations (~30–120 s). Auto-confirms by default |
| `deep_research_heavy` | Long-form DR via `gpt-5-5-pro` + connector (5–30 min, monthly quota). Configurable via `[models].heavy_dr` |
| `gpt_chat` | Talk through one of your private Custom GPTs (`g-p-*`) — *experimental* |

### Image & file management

| Tool | What it does |
|---|---|
| `generate_image` | Generate images via ChatGPT's built-in DALL-E. Returns download URLs + metadata |
| `get_file_info` | Metadata for any ChatGPT file (images, uploads) |
| `get_file_download_url` | Temporary download URL for a ChatGPT file (~1h expiry) |

### Code execution

| Tool | What it does |
|---|---|
| `code_interpreter` | Run Python in ChatGPT's sandbox. Returns output + any generated charts/images |
| `canvas_execute` | Execute code via ChatGPT's Canvas feature (live editing environment) |

### Account introspection

| Tool | What it does |
|---|---|
| `account_status` | Plan, country, groups, feature count, subscription expiry |
| `list_models` | All models on your account (slug, max_tokens, reasoning_type, capabilities, enabled_tools) |
| `list_voices` | Available Voice catalog (backend ID, display metadata, selected state, preview availability) |
| `list_conversations` | Recent ChatGPT conversations (titles: emails/phones redacted) |
| `get_conversation` | Full message history for a specific conversation (multimodal, code, images) |
| `list_tasks` | Scheduled / completed ChatGPT tasks |
| `list_apps` | Connected apps + connectors |
| `list_custom_gpts` | Your private `g-p-*` GPTs |

### Memory & instructions

| Tool | What it does |
|---|---|
| `memory_list` | List all ChatGPT memory entries (emails/phones redacted) |
| `memory_search` | Keyword filter over memories |
| `memory_create_via_chat` | Add a memory (model-initiated workaround — POST `/memories` is 405) |
| `custom_instructions_get` | Read your current `about_user` / `about_model` |
| `custom_instructions_set` | Update them (read-modify-write, preserves unspecified fields) |

### Codex (cloud agent)

| Tool | What it does |
|---|---|
| `list_codex_envs` | Codex environments (label, repos, network policy) |
| `list_codex_tasks` | Recent Codex tasks + status |
| `codex_task_create` | Kick off a new Codex task (resolves env from `repo_label`) |

---

## Architecture

Native Python implementation — no proxy. The server calls
`/backend-api/conversation` (SSE) directly using `curl_cffi` for TLS
impersonation. Vendored POW and Turnstile solvers handle the OpenAI Sentinel
challenge. Token is reloaded from disk on each request, so codex's background
refresh propagates transparently. See [NOTICES](./NOTICES.md) for attribution.

```
$CODEX_HOME/auth.json (default ~/.codex/auth.json) ← auto-refreshed by Codex
~/.gpt2agent/token.json                            ← manual fallback
        |
   gpt2agent  (stdio MCP server, token reloaded on each call)
        |
   curl_cffi  →  chatgpt.com /backend-api/{conversation,f/conversation,me,
                                          models, memories, settings/voices,
                                          codex, gizmos, ...}
        |
   26 MCP tools  (chat, agent, DR ×2, GPT chat, image gen,
                  code interpreter, canvas, memory r/w,
                  instructions r/w, codex r/w, Voice catalog,
                  account introspect)
```

---

## Configuration

Optional, searched in order: `~/.gpt2agent/config.toml`, `./config.toml`,
`~/.config/gpt2agent/config.toml`. Full reference: [docs/configuration.md](./docs/configuration.md).

```toml
[server]
host = "127.0.0.1"   # loopback only; the HTTP transport is UNAUTHENTICATED
port = 9000

[models]
chat     = "gpt-5-3"        # default for chat tool
agent    = "agent-mode"     # default for agent tool
heavy_dr = "gpt-5-5-pro"    # override slug for deep_research_heavy
```

---

## Limitations

- **Deep Research quota:** limits and reset timing are account-reported and can
  change. Run the bundled `deep-research/bin/quota.sh` before heavy work and run
  heavy Deep Research serially.
- **Voice scope:** `list_voices` reads the account's current Voice catalog, but
  it does not start a Voice session or fetch its preview media. GPT-Live
  realtime audio, microphone/playback transport, speech synthesis, and a
  guaranteed post-session transcript adapter are not supported.
- **Other account-tier features not yet supported:** Sora video and
  Operator/CUA.
- **`gpt_chat`** is experimental — `gizmo_id` payload field verified against
  web traffic but not load-tested across all g-p-* types.
- Requires an active ChatGPT Plus or Pro subscription.

---

## Security & risk — read before you run this

gpt2agent talks to ChatGPT's **private** backend the way the web app does. That
has real consequences; please understand them before pointing it at your account.

- **It impersonates the chatgpt.com web client.** It uses `curl_cffi` TLS
  fingerprint impersonation and vendored Proof-of-Work + Cloudflare Turnstile
  solvers to pass the OpenAI Sentinel challenge. This is **very likely against
  the OpenAI Terms of Service**, and automated/abnormal traffic can get your
  account **rate-limited, challenged, suspended, or banned**. Use an account you
  can afford to lose, keep volume human-scale, and don't rely on it for anything
  critical. This is a reverse-engineering / research tool, not an official API.
- **The HTTP transport is UNAUTHENTICATED.** It proxies your *entire* account —
  read all conversations, spend Deep Research quota, overwrite custom
  instructions, launch Codex cloud tasks. Anyone who can reach the port controls
  your account. Therefore:
  - **Use stdio** (the default for `gpt2agent install`) for local clients like
    Claude Code and Codex. It is not network-exposed.
  - The server **binds `127.0.0.1` by default** and **refuses** to start the HTTP
    transport on a non-loopback host unless you explicitly set
    `GPT2AGENT_ALLOW_REMOTE=1`. Only do that behind your own auth proxy / firewall.
- **Your token stays local.** It is read from `$CODEX_HOME/auth.json` (or
  `~/.codex/auth.json` by default), with `~/.gpt2agent/token.json` as the manual
  fallback. Codex manages its own auth file; gpt2agent creates or tightens the
  manual fallback to mode `600` where POSIX supports it. The token is sent only
  to `chatgpt.com`.
  gpt2agent never transmits it anywhere else. Token/secret values are redacted
  from error messages and logs (best-effort).
- **PII redaction is limited.** Tools that return conversation/memory data mask
  **emails, phone numbers, and common secret shapes** (JWTs, bearer tokens,
  `sk-`-style API keys, GitHub tokens) from text — including `get_conversation`
  message bodies — but names, addresses, IDs, and everything else are returned
  verbatim. Don't treat the output as anonymized.
- **`GPT2AGENT_RAW_DUMP`** (debug) writes raw, unredacted SSE/poll traffic —
  including prompts, responses, and resume tokens — to the path you give it.
  The file is created/tightened to mode `600` on POSIX systems, but its content
  remains sensitive. Use an ignored name such as `gpt2agent-raw-dump.jsonl`,
  then delete it after debugging.

Found a security issue? See [SECURITY.md](./SECURITY.md).

---

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

### Release

Tagged releases are configured to publish to PyPI and create a GitHub Release
with the matching CHANGELOG section:

Prepare and merge a reviewed release PR that updates the same version in
`pyproject.toml`, `gpt2agent/__init__.py`, `.claude-plugin/plugin.json`, and
both version fields in `server.json`, plus a non-empty dated `CHANGELOG.md`
section. Verify that candidate before merging:

```bash
python scripts/verify_release.py
```

Stable versions use `X.Y.Z`. Supported prereleases use `X.Y.Z-alphaN`,
`X.Y.Z-betaN`, or `X.Y.Z-rcN` in tags, changelog headings, and project
manifests. Python package metadata and PyPI use the corresponding canonical
PEP 440 spelling (`X.Y.ZaN`, `X.Y.ZbN`, or `X.Y.ZrcN`).

After the release PR is merged, read its exact merge SHA, prove that commit is
on `origin/main`, check out that reviewed tree, then create and push only the
intended annotated tag. This avoids silently including a later unrelated PR:

```bash
set -euo pipefail
git fetch --no-tags origin main:refs/remotes/origin/main
read -r -p "Merged release PR number: " PR_NUMBER
RELEASE_SHA=$(gh pr view "$PR_NUMBER" --json mergeCommit,state \
  --jq 'select(.state == "MERGED") | .mergeCommit.oid')
test -n "$RELEASE_SHA"
git merge-base --is-ancestor "$RELEASE_SHA" origin/main
test -z "$(git status --porcelain)"
git switch --detach "$RELEASE_SHA"
trap 'git switch - >/dev/null || true' EXIT
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
VERSION=$(python - <<'PY'
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open("pyproject.toml", "rb") as stream:
    print(tomllib.load(stream)["project"]["version"])
PY
)
TAG="v$VERSION"
python scripts/verify_release.py --tag "$TAG"
REMOTE_TAG_SHA="$(
  git ls-remote --tags origin |
    awk -v ref="refs/tags/$TAG" '$2 == ref { print $1 }'
)"
if [ -n "$REMOTE_TAG_SHA" ]; then
  echo "Release tag already exists on origin: $TAG" >&2
  exit 1
fi
git tag -a "$TAG" "$RELEASE_SHA" -m "gpt2agent $VERSION"
git push origin "refs/tags/$TAG"
trap - EXIT
git switch -
```

The release workflow (`.github/workflows/release.yml`) verifies every version
surface and the CHANGELOG, reads the remote annotated tag target independently
of checkout's runner-local tag ref, binds it to the event SHA, and proves that
commit is on `origin/main`. It then runs the test matrix, installs and tests the
built wheel and sdist in clean environments, publishes to PyPI via OIDC trusted
publishing, and posts a GitHub Release with that version's CHANGELOG body.

If a publish or downstream release job fails, use GitHub Actions' **Re-run
failed jobs** on that same workflow run so it reuses the original build
artifact. Do not re-run the whole workflow after any file reaches PyPI: Python
sdists are not guaranteed byte-reproducible, and the hash guard intentionally
rejects different rebuilt bytes for an existing version.

If the source gate itself exposes a workflow defect before anything is
published, fix the workflow on `main` and prepare the next version. Never
delete, move, or reuse the failed protected release tag: reruns still execute
the workflow stored at that immutable tagged commit.

> PyPI publishing requires a Trusted Publisher for this repository, workflow
> `release.yml`, and environment `pypi`. Keep that environment restricted to
> protected release tags; the installer fails closed if the published package
> cannot be installed rather than substituting unreleased repository code.

---

## License

[MIT](./LICENSE). See [NOTICES](./NOTICES.md) for third-party attributions.

---

## Acknowledgments

- [lanqian528/chat2api](https://github.com/lanqian528/chat2api) — POW and Turnstile solver code (MIT)
- [basketikun/chatgpt2api](https://github.com/basketikun/chatgpt2api) — survey of ChatGPT backend API patterns
- [7836246/cursor2api](https://github.com/7836246/cursor2api) — survey of Cursor API patterns
