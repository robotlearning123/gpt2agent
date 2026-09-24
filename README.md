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

📖 **[Quickstart](./docs/quickstart.md)** · **[Client setup](./docs/clients.md)** · **[How it works](./docs/how-it-works.md)** · **[Troubleshooting](./docs/troubleshooting.md)** · **[FAQ](./docs/faq.md)** · **[Docs index](./docs/README.md)** · **[Account safety](./docs/account-safety.md)**

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
accounts; receipts in the repo under `artifacts/verify/`). Re-check your own
account any time with **`gpt2agent doctor`** — the table below is the last
verified date, not live state.

| State | Tools |
|---|---|
| ✅ **Working** | `chat` (gpt-6-pro, gpt-5-6), `agent`, `deep_research` (light — rides the chat model's auto-search), `deep_research_heavy` (both accounts 2026-09-23), `code_interpreter`, `generate_image`, `list_models`, `account_status`, `list_conversations`, `get_conversation`, `list_custom_gpts`, `memory_list`, `memory_search`, `list_apps`, `list_tasks`, `list_codex_envs`, `list_codex_tasks`, `custom_instructions_get`, `account_limits`, `rate_limit`, `usage_stats`, `sentinel (bridge)` |
| ⚠ **Known limitations** | `chat(<Work-only slug>)` — GPT-6 Sol/Luna are Work & Codex-only; Chat requests silently resolve to `gpt-5-6` + a *Model note*; `gpt_chat` — 422 with `g-p-` store GPTs; `memory_create_via_chat` — model-dependent; `deep_research_heavy` — needs the Deep Research connector enabled |
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

Prefer steps one at a time, a single client, the Claude Code plugin, HTTP
transport, or manual config snippets? → **[Quickstart](./docs/quickstart.md)**
and **[Client setup](./docs/clients.md)**.

### Authenticate

`codex login` (token reused and reloaded on refresh), or `gpt2agent setup` to
paste a session token once — details in **[Quickstart §3](./docs/quickstart.md)**.

### Multiple ChatGPT accounts

One server entry per account, selected by `env.CODEX_HOME`; both run side by
side in one client with independent tokens, quotas, and rate budgets →
**[Client setup § Multiple accounts](./docs/clients.md#multiple-chatgpt-accounts-in-one-client)**.

---

## The three lanes (every conversation tool)

| Lane | How | Needs | When |
|---|---|---|---|
| **REST** (default) | `chat("hi")` | **Sentinel bridge** (below) | Fastest |
| **Browser** | `chat("hi", browser=True)` | `pip install "gpt2agent[browser]"` + `[browser] enabled = true` + one-time Chrome login | Most reliable; no bridge required |
| **Manual** | `chat("hi", manual=True)` | Nothing (zero network) | Fallback — returns a paste-into-chatgpt.com JSON handoff |

Precedence: `manual=True` > `browser=True` > REST. On an upstream challenge,
conversation tools fall back to the browser lane automatically when it is enabled.

### The sentinel bridge (REST lane)

Upstream's Sentinel challenge demands a bytecode-VM Turnstile token the built-in
solver cannot produce. The **bridge** — an owner-supplied solver directory at
`~/.gpt2agent/sentinel-bridge/` with an `ENABLED` marker — mints the full header
set per request. It is **not part of this distribution**; setup steps, deps, and
env controls (`GPT2AGENT_SENTINEL_BRIDGE`, `…_OFF=1`) are documented in
**[How it works](./docs/how-it-works.md#the-sentinel-challenge)** and
**[Configuration](./docs/configuration.md#environment-variables)**.
**Without the bridge:** read-only tools work; conversation tools over REST fail
at the legacy gate — use `browser=True` or `manual=True`.

### Verify

```bash
gpt2agent doctor      # read-only probes: status table + one-line summary; zero quota
gpt2agent usage       # plan, per-feature quota remaining + reset times, rate window
```

---

## Tools

> `tools/list` enumerates everything, including `usage_stats` and the queue
> tools. Every conversation tool also takes `manual` and `browser`.

### Chat & research

| Tool | Key parameters | What it does | Status |
|---|---|---|---|
| `chat` | `prompt`, `model`, `temporary` | Any model on your account — `gpt-5-6` (default), `gpt-6-pro`, `gpt-5-6-thinking`, `o3-pro`, … (`list_models` shows all) | ✅ live-verified |
| `agent` | `prompt` | Agent Mode — autonomous browsing + code execution | ✅ live-verified |
| `deep_research` | `query`, `auto_confirm` | Web research with inline `[N](url)` citations; rides the chat model's auto-search | ✅ live-verified, both accounts |
| `deep_research_heavy` | `query`, `auto_confirm` | Long-form DR via `gpt-6-pro` + connector (minutes-scale) | ⚠ connector-dependent |
| `gpt_chat` | `gizmo_id`, `prompt` | Your Custom GPTs (`g-` prefix; `g-p-` store GPTs 422) | ⚠ partial |

**Quotas:** light DR bills 1 per completed search turn from the account's
monthly `deep_research` bucket (aborted turns cost 0); heavy DR draws an
independent monthly cap; conversation posts are paced client-side (no fixed
upstream window is reported for Pro). Live numbers:
`gpt2agent usage`. Full measured model → **[FAQ](./docs/faq.md#how-much-deep-research-can-i-run)**.

### Image, code & files

| Tool | What it does | Status |
|---|---|---|
| `generate_image` | DALL·E via your account (download URLs + metadata) | ✅ live-verified |
| `code_interpreter` | Python in ChatGPT's sandbox (output + charts) | ✅ live-verified |
| `canvas_execute` | Canvas retired upstream — returns the deprecation notice | 🔌 retired |
| `get_file_info` / `get_file_download_url` | File metadata / short-lived download URL (need `file_id`) | ❓ |

### Account, memory & Codex introspection

`account_status`, `list_models`, `list_conversations`, `get_conversation`,
`list_tasks`, `list_apps`, `list_custom_gpts` (all ✅) · `memory_list`,
`memory_search`, `custom_instructions_get` (✅) · `memory_create_via_chat` (⚠
model-dependent) · `custom_instructions_set` (❓) · `list_codex_envs`,
`list_codex_tasks` (✅) · `codex_task_create` (❓).

---

## Architecture

Native Python, no proxy: `curl_cffi` (TLS impersonation) streams chatgpt.com
`/backend-api/*` SSE with v1-delta parsing; one persistent simulated browser
identity; per-call lane selection (bridge / Playwright Chrome / handoff).
Details and diagram: **[How it works](./docs/how-it-works.md)**.

**Citations:** DR replies carry inline `[N](url)` anchors — `citations.py`
rewrites the stream's `citeturn…` markers via `content_references` and appends
a Sources section. Knowledge-only answers may have no Sources.

---

## Configuration

Optional TOML, searched in order: `~/.gpt2agent/config.toml`, `./config.toml`,
`~/.config/gpt2agent/config.toml` — keys for `[server]`, `[models]`
(chat / agent / heavy_dr defaults), and `[browser]`, plus environment
variables (`CODEX_HOME`, `GPT2AGENT_SENTINEL_BRIDGE*`, `GPT2AGENT_RAW_DUMP`, …)
→ **[Configuration reference](./docs/configuration.md)**.

---

## Account safety & risk — read before running

gpt2agent talks to ChatGPT's **private** backend the way the web app does —
very likely against the OpenAI ToS; automated traffic can get an account
**rate-limited, challenged, suspended, or banned**. Use an account you can
afford to lose; keep volume human-scale; client-side pacing and challenge
backoff are built in. Token stays local (read from `$CODEX_HOME/auth.json` /
`~/.gpt2agent/token.json`, sent only to chatgpt.com); PII redaction is limited.
The HTTP transport is **unauthenticated** — use stdio. Full policy:
**[Account safety](./docs/account-safety.md)**; issues → [SECURITY.md](./SECURITY.md).

---

## For agents (machine-facing facts)

- **Status ground truth for this host+account:** `gpt2agent doctor` (read-only,
  zero quota, per-tool table + `N OK, M failed, …` summary). The table above is
  the last verified date, not live state.
- **Quota before spending:** `gpt2agent usage` (or MCP `usage_stats`).
- **Lane precedence** is `manual` > `browser` > REST per tool call; on
  `UpstreamChallengeError` with browser enabled, conversation tools fall back
  to the browser lane automatically.
- **Long calls:** light DR tens of seconds, heavy DR up to 30 min (`max_wait`
  1800 s) — set your client's tool timeout accordingly (per-client notes:
  [Client setup](./docs/clients.md#timeouts)).
- **Environment:** `CODEX_HOME` (account selection), `GPT2AGENT_SENTINEL_BRIDGE`,
  `GPT2AGENT_SENTINEL_BRIDGE_OFF=1`, `GPT2AGENT_RAW_DUMP` (debug frames) —
  full table in [Configuration](./docs/configuration.md#environment-variables).
- Claims in this README cite dates; treat anything older than a week as
  needing a re-probe. Evidence-trail paths live in the repo's CLAUDE.md.

---

## Limitations

One line each; details and dates in the **[FAQ](./docs/faq.md)**:

- `gpt_chat` `g-p-` store GPTs → 422 (payload reverse-engineered for `g-` only).
- Work-only slugs (`gpt-6-sol`, `gpt-6-luna`, …) silently resolve to `gpt-5-6`
  on the Chat surface; deepest Chat model is `gpt-6-pro`.
- Light DR rides the chat model since the 2026-09-22 GPT-6 rollout retired the
  `research` lane upstream; occasional knowledge-only answers may lack Sources.
- `deep_research_heavy` needs the Deep Research connector (Settings → Connectors).
- Not yet supported: Sora video, Operator/CUA, voice sessions, Projects, Tasks
  (write), file upload. Requires an active Plus/Pro subscription.

---

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
bash .claude/verify.sh              # full offline suite + ruff, zero network
```

### Testing

- **Offline:** the full unit/contract suite (live auto-skip via `SKIP_LIVE`)
- **Parameter contracts:** `tests/test_param_matrix.py`
- **Live matrix:** `scripts/agent-user-journey.sh <worktree>`
- **Release gate:** `scripts/release-emulation-test.sh <worktree>`

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
