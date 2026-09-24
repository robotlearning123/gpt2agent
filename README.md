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

## TL;DR

```bash
pipx install gpt2agent        # 1. install
codex login                   # 2. authenticate (or: gpt2agent setup)
gpt2agent install             # 3. register with your MCP client(s), then restart it
gpt2agent doctor              # 4. verify — status table, never spends quota
```

Then ask your agent to call `chat`, `deep_research`, or `account_status`.

**One thing to know up front** — conversation tools have three lanes (below):
REST needs the **sentinel bridge** (owner-supplied, not distributed);
**browser** needs a one-time Chrome login; **manual** works with zero network.
Read-only tools work out of the box.

---

## What works right now

Last full verification: **2026-09-24** (light + heavy Deep Research on two Pro
accounts, receipts in `artifacts/verify/`). Re-check your own account any time
with **`gpt2agent doctor`**.

| State | Tools |
|---|---|
| ✅ **Working** | `chat` (gpt-6-pro, gpt-5-6), `agent`, `deep_research` (light — rides the chat model's auto-search), `deep_research_heavy` (both accounts 2026-09-23), `code_interpreter`, `generate_image`, `list_models` (23), `account_status`, `list_conversations`, `get_conversation`, `list_custom_gpts`, `memory_list`, `memory_search`, `list_apps` (107), `list_tasks`, `list_codex_envs`, `list_codex_tasks`, `custom_instructions_get`, `account_limits`, `rate_limit`, `usage_stats`, `sentinel (bridge)` |
| ⚠ **Known limitations** | `chat(<Work-only slug>)` — GPT-6 Sol/Luna are Work & Codex-only; Chat-surface requests silently resolve to `gpt-5-6` + a *Model note* (measured 2026-09-23); `gpt_chat` — 422 with `g-p-` store GPTs; `memory_create_via_chat` — model-dependent; `deep_research_heavy` — needs Settings → Connectors → Deep Research enabled |
| ❓ Unverified | `custom_instructions_set`, `codex_task_create` (plain REST writes); `get_file_info`, `get_file_download_url` (need a `file_id`) |
| 🔌 **Upstream-retired** | `canvas_execute` — returns the model's deprecation notice; use `code_interpreter` |
| 🔇 **Fallback always available** | Every conversation tool: `manual=True` (zero-network handoff) and `browser=True` (real Chrome) |

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/robotlearning123/gpt2agent/main/install.sh | bash
```

That command: installs the package via pipx, reuses your `codex login` token if
present, detects your MCP clients and writes the right config for each, and
drops the Claude Code skills (`deep-research` + `gpt2agent`) into `~/.claude/skills/`.

### Step by step

```bash
pipx install gpt2agent
gpt2agent install                          # auto-detect & register all clients
gpt2agent install --client claude-code     # or: codex, cursor, windsurf, claude-desktop, zed
# VS Code & Cline: manual snippets in docs/clients.md
gpt2agent install --transport http --http-port 9000   # HTTP instead of stdio (UNAUTHENTICATED — loopback only)
```

### As a Claude Code plugin

```text
/plugin marketplace add robotlearning123/gpt2agent
/plugin install gpt2agent@gpt2agent
```

Bundles the server registration + skills. The `gpt2agent` CLI still needs to be
on PATH (`pipx install gpt2agent`) — the plugin wires, it doesn't install Python.

### Authenticate

- **Preferred:** run `codex login` once — gpt2agent reuses
  `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`) and reloads it on
  mtime change, so refreshed tokens are picked up mid-flight.
- **No codex?** `gpt2agent setup` — paste a ChatGPT session token once
  (saved to `~/.gpt2agent/token.json`, mode `600`).

### Multiple ChatGPT accounts

One server entry per account, selected by `env.CODEX_HOME` — both run side by
side in one client with independent tokens, quotas, and rate budgets:

```json
{ "mcpServers": {
    "gpt2agent":   { "command": "gpt2agent", "args": ["run", "--stdio"] },
    "gpt2agent-b": { "command": "gpt2agent", "args": ["run", "--stdio"],
                     "env": { "CODEX_HOME": "/home/you/.codex-second" } } } }
```

Log the second account in once with `CODEX_HOME=~/.codex-second codex login`.
Details: [docs/clients.md](./docs/clients.md).

---

## The three lanes (every conversation tool)

| Lane | How | Needs | When |
|---|---|---|---|
| **REST** (default) | `chat("hi")` | **Sentinel bridge** (below) | Fastest |
| **Browser** | `chat("hi", browser=True)` | `pip install "gpt2agent[browser]"` + `[browser] enabled = true` + one-time Chrome login | Most reliable; no bridge required |
| **Manual** | `chat("hi", manual=True)` | Nothing (zero network) | Fallback — returns a paste-into-chatgpt.com JSON handoff |

Precedence: `manual=True` > `browser=True` > REST.

### The sentinel bridge (REST lane)

Upstream's Sentinel challenge demands a bytecode-VM Turnstile token the built-in
solver cannot produce. The **bridge** loads a solver from an owner-supplied
directory and mints the full header set (fingerprint `p` + PoW + VM token) in
one consistent session, with retry/backoff (3 attempts).

```bash
mkdir -p ~/.gpt2agent/sentinel-bridge
touch ~/.gpt2agent/sentinel-bridge/ENABLED   # marker enables it persistently
```

The directory must contain `wrapper/reverse/vm.py`; deps: `pip install esprima
pillow colorama`. It is **NOT part of this distribution** — the from-scratch
replacement spec is [docs/dev/specs/sentinel-vm.md](./docs/dev/specs/sentinel-vm.md).
Controls: `GPT2AGENT_SENTINEL_BRIDGE=/path` (explicit dir),
`GPT2AGENT_SENTINEL_BRIDGE_OFF=1` (force legacy gate, offline/tests).

**Without the bridge:** read-only tools work; conversation tools over REST fail
at the legacy gate (turnstile) — use `browser=True` or `manual=True`.

### Verify

```bash
gpt2agent doctor      # read-only probes: status table + one-line summary; zero quota
gpt2agent usage       # plan, per-feature quota remaining + reset times, rate window
```

---

## Tools (30)

> Tables below cover the conversation/account surfaces. The rest — `usage_stats`
> and the queue tools (`queue_submit`, `queue_status`, `queue_result`,
> `queue_cancel`) — are listed by `tools/list`. Every conversation tool also
> takes `manual` and `browser`.

### Chat & research

| Tool | Key parameters | What it does | Status |
|---|---|---|---|
| `chat` | `prompt`, `model`, `temporary` | Any model on your account — `gpt-5-6` (default), `gpt-6-pro` (410K), `gpt-5-6-thinking`, `o3-pro`, … | ✅ live-verified |
| `agent` | `prompt` | Agent Mode — 262K context, autonomous browsing + code execution | ✅ live-verified |
| `deep_research` | `query`, `auto_confirm` | Web research with inline `[N](url)` citations (~15–120 s); rides the chat model's auto-search | ✅ live-verified, both accounts |
| `deep_research_heavy` | `query`, `auto_confirm` | Long-form DR via `gpt-6-pro` + connector (5–30 min) | ⚠ connector-dependent |
| `gpt_chat` | `gizmo_id`, `prompt` | Your Custom GPTs (`g-` prefix; `g-p-` store GPTs 422) | ⚠ partial |

### Quotas (measured 2026-09-24, two Pro accounts)

- **Light DR**: 1 per completed search turn from the `deep_research` bucket
  (aborted turns cost 0). Buckets are per-account, monthly.
- **Heavy DR**: independent monthly cap (reported under a `deep_research_*`
  variant when the backend exposes it) — two full heavy reports left the light
  bucket unmoved. Authoritative exhaustion signal: the in-stream `usage_limit`
  frame.
- **Conversation posts**: ~100 per 3 h window, 15 s min interval (client-side
  limiter enforces pacing across processes).

### Image, code & files

| Tool | What it does | Status |
|---|---|---|
| `generate_image` | DALL·E via your account (download URLs + metadata) | ✅ live-verified |
| `code_interpreter` | Python in ChatGPT's sandbox (output + charts) | ✅ live-verified |
| `canvas_execute` | Canvas retired upstream — returns the deprecation notice | 🔌 retired |
| `get_file_info` / `get_file_download_url` | File metadata / ~1 h download URL (need `file_id`) | ❓ |

### Account, memory & Codex introspection

`account_status`, `list_models`, `list_conversations`, `get_conversation`,
`list_tasks`, `list_apps`, `list_custom_gpts` (all ✅) · `memory_list`,
`memory_search`, `custom_instructions_get` (✅) · `memory_create_via_chat` (⚠
model-dependent) · `custom_instructions_set` (❓) · `list_codex_envs`,
`list_codex_tasks` (✅) · `codex_task_create` (❓).

---

## Architecture

Native Python, no proxy: `curl_cffi` (TLS impersonation) → chatgpt.com
`/backend-api/*`, SSE streaming with v1-delta parsing. The sentinel bridge mints
the header set per request; a shared simulation profile keeps one persistent
browser identity (device/session ids, geo-consistent timezone/locale).

```
$CODEX_HOME/auth.json ← auto-refreshed by codex   (~/.gpt2agent/token.json fallback)
        |
   gpt2agent (stdio MCP server; token reloaded per call)
        |
   lane: REST (sentinel bridge) | browser (Playwright Chrome) | manual (handoff JSON)
        |
   30 MCP tools → chatgpt.com/backend-api/*
```

**Citations:** DR replies carry inline `[N](url)` anchors — `citations.py`
rewrites the stream's raw `citeturn…` markers via `content_references` and
appends a Sources section. Knowledge-only answers may have no Sources.

---

## Configuration

Optional, searched in order: `~/.gpt2agent/config.toml`, `./config.toml`,
`~/.config/gpt2agent/config.toml`. Full reference:
[docs/configuration.md](./docs/configuration.md).

```toml
[server]
host = "127.0.0.1"   # loopback only; the HTTP transport is UNAUTHENTICATED
port = 9000

[models]
chat     = "gpt-5-6"        # default for chat AND light deep_research
agent    = "agent-mode"
heavy_dr = "gpt-6-pro"      # override slug for deep_research_heavy

[browser]
enabled  = false             # opt-in; requires "gpt2agent[browser]" extra
headed   = true              # visible Chrome (recommended for Turnstile)
```

---

## Account safety & risk — read before running

gpt2agent talks to ChatGPT's **private** backend the way the web app does.

- **It impersonates the chatgpt.com web client** (TLS fingerprinting + sentinel
  bridge). This is very likely against the OpenAI ToS; automated traffic can get
  an account **rate-limited, challenged, suspended, or banned**. Use an account
  you can afford to lose; keep volume human-scale.
- **Client-side pacing is built in** (sliding window + min interval, shared
  across processes via `~/.gpt2agent/ratelimit-state.json`); challenge = backoff,
  never brute force. Full policy: [docs/account-safety.md](./docs/account-safety.md).
- **Temporary chats by default** (`chat` uses `temporary=True` — image gen /
  code interpreter / canvas need their dedicated tools, which set it `False`).
- **Your token stays local** — read from `$CODEX_HOME/auth.json` /
  `~/.gpt2agent/token.json`, sent only to `chatgpt.com`. PII redaction is
  limited (emails/phones/secret shapes masked, rest verbatim). The HTTP
  transport is **unauthenticated** — use stdio.
- **`GPT2AGENT_RAW_DUMP`** (debug) writes raw unredacted traffic; use mode-600
  paths and delete afterwards.

Security issues: [SECURITY.md](./SECURITY.md).

---

## For agents (machine-facing facts)

- **Status ground truth for this host+account:** `gpt2agent doctor` (read-only,
  zero quota, prints a per-tool table + `N OK, M failed, …` summary). The table
  above is the last verified date, not live state.
- **Quota before spending:** `gpt2agent usage` (or MCP `usage_stats`) —
  per-feature `remaining`/`resets`, plan, message window.
- **Lane precedence** is `manual` > `browser` > REST per tool call; on
  `UpstreamChallengeError` with browser enabled, conversation tools fall back to
  the browser lane automatically.
- **Long calls:** light DR 15–120 s, heavy DR up to 30 min (`max_wait` 1800 s).
  Claude Code defaults cover this (`MCP_TOOL_TIMEOUT`, ms, default ≈28 h; a
  `.mcp.json` per-server `timeout` overrides it — HTTP/SSE servers have a
  separate 60 s per-request cap unless raised). Source: code.claude.com/docs
  env-vars reference.
- **Environment:** `CODEX_HOME` (account selection), `GPT2AGENT_SENTINEL_BRIDGE`,
  `GPT2AGENT_SENTINEL_BRIDGE_OFF=1`, `GPT2AGENT_RAW_DUMP` (debug frames).
- **Evidence trail:** verification receipts live in `artifacts/verify/`; raw
  frame captures under `taskruns/`. Claims in this README cite dates; treat
  anything older than a week as needing a re-probe.
- **Dev loop:** `pip install -e ".[dev]"`; `bash .claude/verify.sh` (suite +
  lint, ~60 s, zero network); live gates: `scripts/agent-user-journey.sh` (15
  cases), `scripts/release-emulation-test.sh` (11 checks).

---

## Limitations

- `gpt_chat` `g-p-` store GPTs → 422 (payload reverse-engineered for `g-` only).
- Work-only slugs (`gpt-6-sol`, `gpt-6-luna`, …) silently resolve to `gpt-5-6`
  on the Chat surface + a *Model note*; deepest Chat model is `gpt-6-pro`.
- **Light DR rides the chat model** since the 2026-09-22 GPT-6 rollout retired
  the `research` lane upstream (accepted turn → in-band `Error in message
  stream`; 11 live probes found no payload fix). Occasional knowledge-only
  answers may carry no Sources.
- `deep_research_heavy` needs the Deep Research connector enabled
  (chatgpt.com → Settings → Connectors).
- Not yet supported: Sora video, Operator/CUA, voice sessions, Projects, Tasks
  (write), file upload. Requires an active Plus/Pro subscription.

---

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
bash .claude/verify.sh              # 619 tests + ruff, zero network
```

### Testing

- **Offline:** 619 unit/contract tests (live auto-skip via `SKIP_LIVE`)
- **Parameter contracts:** `tests/test_param_matrix.py`
- **Live matrix:** `scripts/agent-user-journey.sh <worktree>` — 15 cases
- **Release gate:** `scripts/release-emulation-test.sh <worktree>` — 11 checks

### Release

1. Release PR: bump version in 4 files (`pyproject.toml`,
   `gpt2agent/__init__.py`, `.claude-plugin/plugin.json`, `server.json`) +
   dated `CHANGELOG.md` entry.
2. Verify: `python scripts/verify_release.py`
3. After merge, tag the merge SHA (annotated) and push — CI publishes to PyPI
   (trusted publishing) + GitHub Release. Full runbook incl. the owner publish
   gate: [docs/release-validation.md](./docs/release-validation.md).

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
failed jobs** on that same run (artifact reuse). Do not re-run the whole
workflow after any file reaches PyPI: sdists are not byte-reproducible and the
hash guard rejects rebuilt bytes for an existing version.

---

## License

[MIT](./LICENSE). Third-party attributions: [NOTICES](./NOTICES.md).

## Acknowledgments

- [lanqian528/chat2api](https://github.com/lanqian528/chat2api) — POW solver (MIT)
- Sentinel bridge technique studied from public reverse-engineering (2026);
  clean-room spec: [docs/dev/specs/sentinel-vm.md](./docs/dev/specs/sentinel-vm.md)
- [basketikun/chatgpt2api](https://github.com/basketikun/chatgpt2api) — survey of ChatGPT backend API patterns
