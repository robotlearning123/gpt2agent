# gpt2agent

<!-- mcp-name: io.github.robotlearning123/gpt2agent -->

> **MCP server for your ChatGPT account: `codex login` → ChatGPT Plus/Pro inside any MCP client.**

An **MCP server** that puts your **ChatGPT Plus or Pro** subscription — account-visible
model catalogs and the supported account features below — inside Claude Code,
Codex, Cursor, Windsurf, Zed, and any MCP client.

[![PyPI version](https://img.shields.io/pypi/v/gpt2agent)](https://pypi.org/project/gpt2agent/)
[![CI](https://github.com/robotlearning123/gpt2agent/actions/workflows/ci.yml/badge.svg)](https://github.com/robotlearning123/gpt2agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/gpt2agent/)

📖 **[Quickstart](./docs/quickstart.md)** · **[Client setup](./docs/clients.md)** · **[0.0.12 migration](./docs/migration-0.0.12.md)** · **[Troubleshooting](./docs/troubleshooting.md)** · **[FAQ](./docs/faq.md)** · **[Docs index](./docs/README.md)**

---

## What it does

gpt2agent exposes **32 MCP tools and 2 static MCP resources** backed by ChatGPT's
private account surface. No proxy process. No separate account. No platform API
key. Your `codex login`, your token, your quota.

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

# Claude Code URL registration instead of stdio?
gpt2agent install --client claude-code --transport http --http-port 9000
# The installer registers the URL; run and supervise the server separately:
gpt2agent run --http --port 9000
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

After a default stdio `install`, restart Claude Code so it re-spawns the
subprocess. Codex picks up the new server on its next invocation automatically.
An HTTP Claude Code registration does not start or supervise a server; keep
`gpt2agent run --http --port 9000` running separately.

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

## Tools (32)

### Chat & reasoning

| Tool | What it does |
|---|---|
| `chat` | Talk to a general ChatGPT model (`gpt-5-3` default). Optional `thinking_effort` is validated against that model's live catalog. |
| `agent` | **Agent Mode** — 262K context with autonomous browsing, code execution, and tool use; its configured model is validated against the general catalog |
| `deep_research` | Web-augmented research with citations (~30–120 s). Auto-confirms by default |
| `deep_research_heavy` | Long-form DR via `gpt-5-5-pro` + connector (5–30 min, monthly quota). Configurable via `[models].heavy_dr` |
| `gpt_chat` | Talk through one of your private Custom GPTs (`g-p-*`) — *experimental* |

### Image & file management

| Tool | What it does |
|---|---|
| `generate_image` | Generate images through the observed private prepare/conduit + `/f` v1 flow. Returns relationally validated, allowlisted asset fields + download URLs |
| `get_file_info` | Metadata for any ChatGPT file (images, uploads) |
| `get_file_download_url` | Validated public HTTPS download URL for a ChatGPT file (~1h expiry; signed query preserved) |

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
| `list_work_models` | Separate account-visible Work model catalog; Work-only slugs are not merged into general chat models |
| `account_capabilities` | Live, shape-only capability truth with explicit entitlement/reachability/contract states |
| `list_conversations` | Recent ChatGPT conversations (titles: emails/phones redacted) |
| `get_conversation` | Full message history for a specific conversation (multimodal, code, images) |
| `list_tasks` | Generic background/asynchronous ChatGPT jobs; not scheduled automations |
| `list_scheduled_tasks` | One page of scheduled ChatGPT automations from the dedicated automation surface |
| `list_apps` | Connected Apps and connectors (a separate concept from Plugins) |
| `list_plugins` | Bounded page of the account Plugin catalog |
| `list_installed_plugins` | Installed Plugins with bounded non-identifying fields |
| `sites_access` | Sites access booleans without workspace identity or content |
| `list_sites` | Bounded Sites metadata without page content or private URLs |
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
                                          models, memories, codex, gizmos, ...}
        |
   32 MCP tools + 2 static resources
        (chat, agent, DR ×2, GPT chat, image gen, code interpreter,
         canvas, memory/instructions/codex, Apps, Plugins, Work,
         automations, Sites, capability truth, release evidence)
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

Ordinary REST/JSON account requests share a process-wide limit of four in-flight
calls. `GPT2AGENT_MAX_IN_FLIGHT` may be set to an integer from 1 through 8.
Saturated callers fail within one second; backend 429 responses activate a
route-local cooldown capped at 60 seconds. Direct SSE/Sentinel streams are not
held by this semaphore; endpoint timeouts bound them, and heavy Deep Research
should run serially.

### MCP contract

All 32 tools declare the four standard MCP tool annotations (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, and `openWorldHint`). These are client hints,
not an authorization boundary. The package stays on the stable MCP Python v1
line with `mcp>=1.26,<2` and tests the minimum and latest resolvable v1 versions.

The two resources are read-only, deterministic packaged JSON and never contact
the account or network when read:

- `chatgpt://feature-coverage` describes the 0.0.12 feature/tool contract;
- `chatgpt://update-evidence` records the public-surface evidence snapshot.

Use `account_capabilities` for current account truth. Its `entitled` and
`reachable_now` fields are tri-state (`true`, `false`, or unknown), and its
status distinguishes unavailability, contract drift, temporary failure,
indeterminate access, and unverified features. To avoid incidental private-data
reads, the inventory leaves conversations, memory, and custom instructions
unverified; call their explicit tools only when that content is wanted.
Apps/connectors, Plugins, and Skills remain separate concepts: tools act,
resources provide stable context, Skills guide clients, and a Claude Code
Plugin bundles distribution.

---

## Limitations

- **Deep Research quota:** limits and reset timing are account-reported and can
  change. Run the bundled `deep-research/bin/quota.sh` before heavy work and run
  heavy Deep Research serially.
- **Account-tier features not yet supported:** Sora video, Operator/CUA,
  Projects, and Voice sessions. The Projects candidate route is not an
  established adapter. Version 0.0.12 exposes no Voice tool, audio, microphone,
  WebRTC session, or OpenAI API fallback. A bounded read-only voice catalog is
  planned for 0.0.13; later AgentRTC work is a separate transport project.
- **GPT-Live is not a supported MCP audio capability.** Current official
  [Voice guidance](https://help.openai.com/en/articles/20001274) describes Live
  as a separate human feature and says it does not initially support connected
  apps or Plugins, Work, Codex, Custom GPTs, Temporary Chats, or desktop.
- **`gpt_chat`** is experimental — `gizmo_id` payload field verified against
  web traffic but not load-tested across all g-p-* types.
- **Private routes are unstable.** The no-secret public radar can detect public
  documentation/bundle drift but cannot prove a private account adapter. Release
  candidates therefore need a sanitized exact-commit account receipt from a
  trusted local machine; `contract_changed` remains a real runtime outcome.
- **Current image execution is not verified.** On 2026-07-10 the authenticated
  website generated an image and the account catalog advertised image-capable
  models, but the direct client failed closed on a changed required Turnstile
  challenge before the prepare request. Catalog entitlement is not execution
  reachability; do not treat `generate_image` as currently verified.
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
  - **Use stdio** (the default for plain `gpt2agent run` and `gpt2agent
    install`) for local clients like Claude Code and Codex. It is not
    network-exposed.
  - The server **binds `127.0.0.1` by default** and **always refuses** a
    non-loopback HTTP bind. There is no remote override. The MCP SDK's native
    Host and Origin checks also reject non-loopback DNS-rebinding attempts.
- **Your token stays local.** It is read from `$CODEX_HOME/auth.json` (or
  `~/.codex/auth.json` by default), with `~/.gpt2agent/token.json` as the manual
  fallback. Codex manages its own auth file; gpt2agent creates or tightens the
  manual fallback to mode `600` where POSIX supports it. The token is sent only
  to `chatgpt.com`.
  gpt2agent never transmits it anywhere else. Token/secret values are redacted
  from error messages and logs (best-effort).
- **PII redaction is limited.** Tools that return conversation/memory data mask
  **emails, phone numbers, and common secret shapes** (including structured API
  tokens, label-aware credential assignments, credential-bearing database URLs,
  and PEM private keys) from text — including `get_conversation` message bodies —
  but names, addresses, IDs, and everything else are returned verbatim. Don't
  treat the output as anonymized.
- **Hidden tool payloads stay hidden, but execution is disclosed.** Every
  `chat`, `agent`, and `gpt_chat` completion ends with one server-appended
  `Tool activity receipt`: fixed categories such as `web`, `code_execution`,
  or `connector`, or `none` when no activity was observed. Only the final
  footer is authoritative; an earlier lookalike in model text is not a receipt.
  Private dispatch and response bodies are never echoed.
- **Raw backend dumps are disabled.** The bundled Deep Research runner writes
  only the explicitly requested `report.md` plus shape-only `status.txt`; it
  does not persist raw events, server metadata, prompts, responses, or resume
  tokens.
- **Write serialization is process-local.** `custom_instructions_set` protects
  its read-modify-write inside one server, but two independently running MCP
  server processes can still race. Avoid concurrent custom-instruction writers.

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

After the release PR is merged, run the private account gate on a trusted local
machine against its exact merge SHA. Main CI builds the wheel and sdist once and
retains them under an attempt-specific, immutable artifact ID. In its
credential-free package job, main CI installs both formats, runs the same closed
synthetic adapter corpus against each, requires byte-identical corpus results,
and verifies that the distribution hashes do not change. The local account gate
downloads that exact candidate but treats both files as inert bytes: it never
installs, imports, builds, or executes them.

Bootstrap the verifier from `requirements-account-gate.txt` into a new
owner-private CPython 3.12 environment outside the checkout. The bootstrap
accepts only the reviewed nine-distribution closure, exact hashes, binary wheels,
and the official PyPI index, then verifies every installed file and import
origin. It emits only the trusted `site-packages` path on stdout. The live gate
loads `curl_cffi` from that explicit path under `python -I -S -B`, keeps the
reviewed local bearer in process, disables environment proxy discovery and
automatic redirects, and accepts only the exact reviewed `chatgpt.com` GET
routes with bounded metadata, 4 MiB response bodies, fixed timeouts, and TLS
verification.

The gate writes one mode-0600, closed-schema sanitized receipt bound to the
source commit/tree, full CI workflow identity, and exact artifact bytes. It
measures the authenticated account's active Pro entitlement;
`--expected-plan pro` is a fail-closed expectation, not a caller assertion. The
receipt keeps only empty/nonempty shape classes, never exact account collection
counts. Its fixed `11/11/11/0` adapter counts are independent evidence from the
required main-CI corpus, not self-attestation by the live probe. Receipt creation
and pre-tag verification reject a probe older than 30 minutes, a probe lasting
more than 10 minutes, or a completion time more than one minute in the future.

Never upload cookies, bearer tokens, raw responses, unsanitized account payloads,
or the receipt to hosted CI. Candidate package smoke belongs only in
credential-free CI or an OS-isolated container/VM with no account auth or private
mounts. A scrubbed environment and private `HOME` improve hygiene but are not an
OS sandbox. Keep the release branch and the exact full-SHA publication-action
commit pushed and resolvable until the first release is fully verified; hosted
Actions must be able to fetch that immutable action revision.

The trusted release machine must provide an authenticated `gh` CLI for read-only
operator checks, the separately reviewed governance policy, and the reviewed Pro
account login. The coordinator prompts for the short-lived, repository-scoped
release App installation token only after receipt creation. It never passes that
token to Python or places it in process arguments: it immediately revalidates the
pinned CI candidate with the operator's read token, independently re-verifies the
receipt, builds canonical tag JSON, and lets the App create only the new annotated
tag and ref. Never substitute the operator's user token for the App token.

```bash
set -euo pipefail
set +x
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
git fetch --no-tags origin +main:refs/remotes/origin/main
: "${GPT2AGENT_RELEASE_GOVERNANCE_POLICY:?set this to the reviewed policy JSON}"
unset GH_TOKEN GITHUB_TOKEN GPT2AGENT_RELEASE_APP_TOKEN
read -r -p "Merged release PR number: " PR_NUMBER
RELEASE_SHA=$(gh pr view "$PR_NUMBER" --json mergeCommit,state \
  --jq 'select(.state == "MERGED") | .mergeCommit.oid')
case "$RELEASE_SHA" in (*[!0-9a-f]*|'') exit 1;; esac
test "${#RELEASE_SHA}" -eq 40
git merge-base --is-ancestor "$RELEASE_SHA" origin/main
test -z "$(git status --porcelain=v1 --untracked-files=all \
  --ignored=matching --ignore-submodules=none)"

START_BRANCH=$(git symbolic-ref --quiet --short HEAD || true)
START_COMMIT=$(git rev-parse HEAD)
RUNTIME_ROOT=
cleanup_release_runtime() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ -n "$START_BRANCH" ]; then
    git switch -- "$START_BRANCH" >/dev/null || status=1
  else
    git switch --detach "$START_COMMIT" >/dev/null || status=1
  fi
  if [ -n "$RUNTIME_ROOT" ]; then
    rm -rf -- "$RUNTIME_ROOT" || status=1
  fi
  return "$status"
}
trap cleanup_release_runtime EXIT
trap 'exit 130' HUP INT TERM

git switch --detach "$RELEASE_SHA"
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git status --porcelain=v1 --untracked-files=all \
  --ignored=matching --ignore-submodules=none)"

RUNTIME_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/gpt2agent-account-gate.XXXXXXXX")
chmod 700 "$RUNTIME_ROOT"
VENV="$RUNTIME_ROOT/venv"
SITE_PACKAGES=$(scripts/bootstrap_account_gate.sh \
  --python /usr/bin/python3.12 --venv "$VENV")
VERIFIER_PYTHON="$VENV/bin/python"
test -x "$VERIFIER_PYTHON"

REPOSITORY=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
"$VERIFIER_PYTHON" -I -S -B scripts/audit_release_governance.py \
  --live "$REPOSITORY" \
  --policy "$GPT2AGENT_RELEASE_GOVERNANCE_POLICY"

CANDIDATE_JSON="$RUNTIME_ROOT/candidate.json"
GH_TOKEN="$(gh auth token)" "$VERIFIER_PYTHON" -I -S -B \
  scripts/verify_main_ci.py \
  --repository "$REPOSITORY" --commit "$RELEASE_SHA" \
  --print-candidate-json --attempts 180 --delay 10 >"$CANDIDATE_JSON"
candidate_field() {
  "$VERIFIER_PYTHON" -I -S -B -c \
    'import json,pathlib,sys; print(json.loads(pathlib.Path(sys.argv[1]).read_bytes())[sys.argv[2]])' \
    "$CANDIDATE_JSON" "$1"
}
CI_RUN_ID=$(candidate_field run_id)
CI_RUN_ATTEMPT=$(candidate_field run_attempt)
CI_ARTIFACT_ID=$(candidate_field artifact_id)
CI_ARTIFACT_NAME=$(candidate_field artifact_name)
CI_ARTIFACT_DIGEST=$(candidate_field artifact_digest)
CI_ARTIFACT_SIZE=$(candidate_field artifact_size)
CI_ARTIFACT_EXPIRES_AT=$(candidate_field artifact_expires_at)

COMMIT=$(git rev-parse HEAD)
TREE=$(git rev-parse 'HEAD^{tree}')
VERSION=$("$VERIFIER_PYTHON" -I -S -B -c \
  'import pathlib,sys,tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["project"]["version"])' \
  "$ROOT/pyproject.toml")
TAG="v$VERSION"
"$VERIFIER_PYTHON" -I -S -B scripts/verify_release.py --tag "$TAG"
DIST="$ROOT/../gpt2agent-$TAG-$COMMIT-main-ci-candidate"
RECEIPT="$ROOT/../gpt2agent-$TAG-$COMMIT.account-receipt.json"
test ! -e "$DIST" && test ! -L "$DIST"
test ! -e "$RECEIPT" && test ! -L "$RECEIPT"
GH_TOKEN="$(gh auth token)" gh run download "$CI_RUN_ID" \
  --repo "$REPOSITORY" --name "$CI_ARTIFACT_NAME" --dir "$DIST"

CREATE_SUMMARY="$RUNTIME_ROOT/create-summary.txt"
"$VERIFIER_PYTHON" -I -S -B scripts/verify_account_receipt.py create \
  --checkout "$ROOT" --dist "$DIST" --output "$RECEIPT" \
  --commit "$COMMIT" --tree "$TREE" --expected-plan pro \
  --repository "$REPOSITORY" \
  --ci-run-id "$CI_RUN_ID" --ci-run-attempt "$CI_RUN_ATTEMPT" \
  --ci-artifact-id "$CI_ARTIFACT_ID" \
  --ci-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --ci-artifact-size "$CI_ARTIFACT_SIZE" \
  --ci-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT" \
  --trusted-site-packages "$SITE_PACKAGES" >"$CREATE_SUMMARY"
RECEIPT_SHA256=$("$VERIFIER_PYTHON" -I -S -B -c \
  'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
  "$RECEIPT")
case "$RECEIPT_SHA256" in (*[!0-9a-f]*|'') exit 1;; esac
test "${#RECEIPT_SHA256}" -eq 64
VERIFY_SUMMARY="$RUNTIME_ROOT/verify-summary.txt"
"$VERIFIER_PYTHON" -I -S -B scripts/verify_account_receipt.py verify \
  --receipt "$RECEIPT" --checkout "$ROOT" --dist "$DIST" \
  --commit "$COMMIT" --tree "$TREE" --sha256 "$RECEIPT_SHA256" \
  --repository "$REPOSITORY" \
  --ci-run-id "$CI_RUN_ID" --ci-run-attempt "$CI_RUN_ATTEMPT" \
  --ci-artifact-id "$CI_ARTIFACT_ID" \
  --ci-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --ci-artifact-size "$CI_ARTIFACT_SIZE" \
  --ci-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT" >"$VERIFY_SUMMARY"
cmp --silent "$CREATE_SUMMARY" "$VERIFY_SUMMARY"

scripts/create_release_tag.sh \
  --python "$VERIFIER_PYTHON" --checkout "$ROOT" --dist "$DIST" \
  --receipt "$RECEIPT" --receipt-sha256 "$RECEIPT_SHA256" \
  --repository "$REPOSITORY" --tag "$TAG" \
  --commit "$COMMIT" --tree "$TREE" \
  --ci-run-id "$CI_RUN_ID" --ci-run-attempt "$CI_RUN_ATTEMPT" \
  --ci-artifact-id "$CI_ARTIFACT_ID" \
  --ci-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --ci-artifact-size "$CI_ARTIFACT_SIZE" \
  --ci-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT"

trap - EXIT HUP INT TERM
cleanup_release_runtime
```

The release workflow (`.github/workflows/release.yml`) verifies every version
surface and the CHANGELOG, reads the remote annotated tag target independently
of checkout's runner-local tag ref, binds it to the event SHA, and proves that
commit is on `origin/main`. Both the local pre-tag command and the tagged
workflow require a successful `ci.yml` `push` run for that exact commit on
`main`; a green PR run or a newer branch head cannot substitute. The workflow
accepts only the exact canonical tag-message envelope generated by
`release_tag_metadata.py`: one title and one ASCII, duplicate-key-free,
closed-schema JSON object binding repository, tag, version, source commit/tree,
receipt and artifact-set digests, and every candidate identity field. Extra
text, alternate spellings, and noncanonical JSON fail closed. It fetches the
pinned run and immutable artifact ID live, then creates canonical evidence
binding that handoff to the tag object, source commit/tree, release workflow,
and exact wheel/sdist hashes.

The workflow never rebuilds after the account gate. It downloads the exact
account-bound main-CI artifact by numeric ID, reconstructs its artifact-set
digest, and publishes those same bytes to PyPI via OIDC trusted publishing. No
pre-publication release job installs or imports the candidate or reruns
`package_smoke.sh`; the required credential-free main-CI package job is the sole
pre-publication packaged-artifact execution gate. After PyPI publication, a
credential-free canary installs the public version and checks its CLIs,
resources, and import surface. The workflow also verifies published filenames
and hashes before creating the GitHub Release, whose initial assets include the
distributions and `release-workflow-artifacts.json`.

GitHub publication is pinned to the reviewed local action by its full 40-byte
commit SHA. That action resolves or creates only the exact-tag draft, captures
its numeric release ID, and targets every asset read, upload, deletion, and
publication update by that immutable ID. It validates exact tag, target commit,
draft/prerelease flags, name, notes, and asset names/hashes both before and after
the public transition, then requires GitHub's immutable-release setting on the
readback. A rerun against an already-public release succeeds only when that
release is already an exact immutable match; it cannot silently rewrite it.

The annotated-tag SHA-256 is a commitment to the local receipt bytes. Hosted
automation validates the tag-bound artifact identity and set digest, but it
does not receive the account receipt or its shape-only probe records. Treat
publication as blocked unless independent live
controls are also configured and verified: a policy-bound release App is the
only actor that can create `v*` tags; separate no-bypass rules make existing
tags immutable; the `pypi` environment requires the policy-bound independent
reviewer or protection App; self-review and administrator bypass are disabled;
and `main` has no bypass actor. The approver must verify the exact tag, commit,
tree, version, and receipt digest before allowing publication.
`scripts/audit_release_governance.py --live OWNER/REPO --policy POLICY.json`
performs these reviewed, read-only GitHub checks and exits nonzero when the
closed identity policy or any required live control is absent.

Immediately before the release App creates the immutable tag, the operator
command re-fetches the complete pinned run artifact list, requires at least one
hour of remaining retention, and revalidates every recorded artifact identity
field. A newer candidate-producing attempt invalidates the account gate; a
newer failed-only run attempt without a newer candidate artifact does not.

After the tagged workflow is green, verify the retained local receipt bytes
against the annotated-tag commitment. Keep that mode-0600 account evidence in
the approved local evidence store; do not upload it to Actions or the public
GitHub Release:

```bash
set -euo pipefail
umask 077
read -r -p "Release tag (for example v0.0.12): " TAG
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
case "$TAG" in (v[0-9]*.[0-9]*.[0-9]*) ;; (*) exit 1;; esac
AUDIT_REF=refs/release-verification/retained-receipt
if git show-ref --verify --quiet "$AUDIT_REF"; then exit 1; fi
AUDIT_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/gpt2agent-receipt-audit.XXXXXXXX")
chmod 700 "$AUDIT_ROOT"
cleanup_receipt_audit() {
  status=$?
  trap - EXIT HUP INT TERM
  git update-ref -d "$AUDIT_REF" >/dev/null 2>&1 || status=1
  rm -rf -- "$AUDIT_ROOT" || status=1
  return "$status"
}
trap cleanup_receipt_audit EXIT
trap 'exit 130' HUP INT TERM

git fetch --force --no-tags origin "refs/tags/$TAG:$AUDIT_REF"
test "$(git cat-file -t "$AUDIT_REF")" = tag
COMMIT=$(git rev-parse "$AUDIT_REF^{}")
TREE=$(git rev-parse "$AUDIT_REF^{tree}")
RECEIPT="$ROOT/../gpt2agent-$TAG-$COMMIT.account-receipt.json"
test -f "$RECEIPT" && test ! -L "$RECEIPT"
test "$(stat -c '%a' "$RECEIPT")" = 600

TAG_OBJECT="$AUDIT_ROOT/tag-object"
TAG_OUTPUT="$AUDIT_ROOT/tag-output"
TAG_VERIFIER="$AUDIT_ROOT/release_tag_metadata.py"
git cat-file tag "$AUDIT_REF" >"$TAG_OBJECT"
git show "$AUDIT_REF:scripts/release_tag_metadata.py" >"$TAG_VERIFIER"
REPOSITORY=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
GITHUB_OUTPUT="$TAG_OUTPUT" /usr/bin/python3.12 -I -S -B "$TAG_VERIFIER" \
  verify-tag-object --tag-object-file "$TAG_OBJECT" \
  --repository "$REPOSITORY" --tag "$TAG" \
  --commit "$COMMIT" --tree "$TREE"
test "$(grep -c '^receipt_sha256=' "$TAG_OUTPUT")" -eq 1
TAG_RECEIPT_SHA256=$(sed -n 's/^receipt_sha256=//p' "$TAG_OUTPUT")
test "${#TAG_RECEIPT_SHA256}" -eq 64
case "$TAG_RECEIPT_SHA256" in (*[!0-9a-f]*|'') exit 1;; esac
LOCAL_RECEIPT_SHA256=$(/usr/bin/python3.12 -I -S -B -c \
  'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
  "$RECEIPT")
test "$LOCAL_RECEIPT_SHA256" = "$TAG_RECEIPT_SHA256"

trap - EXIT HUP INT TERM
cleanup_receipt_audit
```

Retain or dispose of the owned local receipt and candidate directory only under
the reviewed local release-evidence policy. The public tag and
`release-workflow-artifacts.json` retain the non-account provenance commitments.

If a publish or downstream release job fails, use GitHub Actions' **Re-run
failed jobs** on that same workflow run so it reuses the pinned main-CI artifact.
Do not re-run the whole workflow after any file reaches PyPI. Before tagging, a
deleted, expired, replaced, or near-expiry candidate requires a full main-CI
rerun and a new account gate. A failed-only CI rerun may reuse an earlier package
attempt only when its exact producing attempt and artifact ID remain pinned.
After tagging, loss or mismatch of the pinned artifact blocks that release:
never rebuild, move, delete, or recreate the immutable version tag.

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
