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
retains them under an attempt-specific, immutable artifact ID. The gate downloads
that exact candidate, separately checks both distributions, probes through the
installed wheel environment, writes one closed-schema sanitized receipt, and
binds it to the source commit/tree, full CI workflow identity, and exact artifact
bytes. It measures the authenticated account's active Pro
entitlement; `--expected-plan pro` is a fail-closed expectation, not a caller
assertion. The public receipt keeps only empty/nonempty shape classes, never
exact account collection counts. Never upload cookies, bearer tokens, raw
responses, or unsanitized account payloads to hosted CI. Use new sibling paths
outside the checkout so the exact source stays clean, then have the policy-bound
release App create only the intended annotated tag. The trusted release
environment must provide an authenticated `gh` CLI for read-only operator
checks, a short-lived installation token for the
policy-bound release App scoped to this repository with Contents write access,
the reviewed local governance policy, and the reviewed Pro account login. Never
substitute the operator's user token for the App token:

```bash
set -euo pipefail
git fetch --no-tags origin main:refs/remotes/origin/main
: "${GPT2AGENT_RELEASE_GOVERNANCE_POLICY:?set this to the reviewed policy JSON}"
: "${GPT2AGENT_RELEASE_APP_TOKEN:?set this to a short-lived release App installation token}"
python scripts/audit_release_governance.py \
  --live robotlearning123/gpt2agent \
  --policy "$GPT2AGENT_RELEASE_GOVERNANCE_POLICY"
read -r -p "Merged release PR number: " PR_NUMBER
RELEASE_SHA=$(gh pr view "$PR_NUMBER" --json mergeCommit,state \
  --jq 'select(.state == "MERGED") | .mergeCommit.oid')
test -n "$RELEASE_SHA"
git merge-base --is-ancestor "$RELEASE_SHA" origin/main
test -z "$(git status --porcelain=v1 --untracked-files=all --ignored=matching)"
git switch --detach "$RELEASE_SHA"
trap 'git switch - >/dev/null || true' EXIT
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
REPOSITORY=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
CANDIDATE_JSON=$(GH_TOKEN="$(gh auth token)" python scripts/verify_main_ci.py \
  --repository "$REPOSITORY" --commit "$RELEASE_SHA" \
  --print-candidate-json --attempts 180 --delay 10)
candidate_field() {
  python -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' \
    "$CANDIDATE_JSON" "$1"
}
CI_RUN_ID=$(candidate_field run_id)
CI_RUN_ATTEMPT=$(candidate_field run_attempt)
CI_ARTIFACT_ID=$(candidate_field artifact_id)
CI_ARTIFACT_NAME=$(candidate_field artifact_name)
CI_ARTIFACT_DIGEST=$(candidate_field artifact_digest)
CI_ARTIFACT_SIZE=$(candidate_field artifact_size)
CI_ARTIFACT_EXPIRES_AT=$(candidate_field artifact_expires_at)
ROOT=$PWD
COMMIT=$(git rev-parse HEAD)
TREE=$(git rev-parse 'HEAD^{tree}')
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
DIST="$ROOT/../gpt2agent-$TAG-$COMMIT-main-ci-candidate"
RECEIPT="$ROOT/../gpt2agent-$TAG-$COMMIT.account-receipt.json"
test ! -e "$DIST"
test ! -e "$RECEIPT"
gh run download "$CI_RUN_ID" --repo "$REPOSITORY" \
  --name "$CI_ARTIFACT_NAME" --dir "$DIST"
CREATE_OUTPUT=$(python scripts/verify_account_receipt.py create \
  --checkout "$ROOT" --dist "$DIST" --output "$RECEIPT" \
  --commit "$COMMIT" --tree "$TREE" --expected-plan pro \
  --repository "$REPOSITORY" \
  --ci-run-id "$CI_RUN_ID" --ci-run-attempt "$CI_RUN_ATTEMPT" \
  --ci-artifact-id "$CI_ARTIFACT_ID" \
  --ci-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --ci-artifact-size "$CI_ARTIFACT_SIZE" \
  --ci-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT")
RECEIPT_SHA256=$(printf '%s\n' "$CREATE_OUTPUT" | \
  sed -n 's/^account-receipt-sha256: //p')
test "${#RECEIPT_SHA256}" -eq 64
case "$RECEIPT_SHA256" in (*[!0-9a-f]*) exit 1;; esac
VERIFY_OUTPUT=$(python scripts/verify_account_receipt.py verify \
  --receipt "$RECEIPT" --checkout "$ROOT" --dist "$DIST" \
  --commit "$COMMIT" --tree "$TREE" --sha256 "$RECEIPT_SHA256" \
  --repository "$REPOSITORY" \
  --ci-run-id "$CI_RUN_ID" --ci-run-attempt "$CI_RUN_ATTEMPT" \
  --ci-artifact-id "$CI_ARTIFACT_ID" \
  --ci-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --ci-artifact-size "$CI_ARTIFACT_SIZE" \
  --ci-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT")
test "$VERIFY_OUTPUT" = "$CREATE_OUTPUT"
REMOTE_TAG_SHA="$(
  git ls-remote --tags origin |
    awk -v ref="refs/tags/$TAG" '$2 == ref { print $1 }'
)"
if [ -n "$REMOTE_TAG_SHA" ]; then
  echo "Release tag already exists on origin: $TAG" >&2
  exit 1
fi
TAG_MESSAGE=$(printf 'gpt2agent %s\n\n%s' "$VERSION" "$CREATE_OUTPUT")
TAG_OBJECT_SHA=$(
  GH_TOKEN="$GPT2AGENT_RELEASE_APP_TOKEN" gh api --method POST \
    "repos/$REPOSITORY/git/tags" \
    --raw-field tag="$TAG" \
    --raw-field message="$TAG_MESSAGE" \
    --raw-field object="$RELEASE_SHA" \
    --raw-field type=commit \
    --jq .sha
)
test -n "$TAG_OBJECT_SHA"
GH_TOKEN="$GPT2AGENT_RELEASE_APP_TOKEN" gh api --method POST \
  "repos/$REPOSITORY/git/refs" \
  --raw-field ref="refs/tags/$TAG" \
  --raw-field sha="$TAG_OBJECT_SHA" >/dev/null
unset GPT2AGENT_RELEASE_APP_TOKEN TAG_MESSAGE
git fetch --no-tags origin "refs/tags/$TAG:refs/tags/$TAG"
test "$(git cat-file -t "$TAG")" = tag
test "$(git rev-parse "$TAG")" = "$TAG_OBJECT_SHA"
test "$(git rev-parse "$TAG^{}")" = "$RELEASE_SHA"
trap - EXIT
git switch -
```

The release workflow (`.github/workflows/release.yml`) verifies every version
surface and the CHANGELOG, reads the remote annotated tag target independently
of checkout's runner-local tag ref, binds it to the event SHA, and proves that
commit is on `origin/main`. Both the local pre-tag command and the tagged
workflow require a successful `ci.yml` `push` run for that exact commit on
`main`; a green PR run or a newer branch head cannot substitute. The workflow
requires exactly one valid value for every receipt and candidate-identity field
in the annotated tag. It fetches the pinned run and immutable artifact ID live,
then creates canonical evidence binding the receipt digest and full account
handoff identity to the tag object, source commit/tree, release workflow, and
exact wheel/sdist hashes.

The workflow never rebuilds after the account gate. It downloads the exact
account-tested main-CI artifact by numeric ID, reconstructs its artifact-set
digest, runs Twine and clean-install tests, publishes those same bytes to PyPI
via OIDC trusted publishing, verifies the
published filenames and hashes, and installs that exact PyPI version in a clean
canary environment. The GitHub Release is created only after the canary passes;
its initial assets include the distributions and `release-workflow-artifacts.json`.

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

After the tagged workflow is green, verify the retained local receipt bytes
against the annotated-tag commitment. Keep that mode-0600 account evidence in
the approved local evidence store; do not upload it to Actions or the public
GitHub Release:

```bash
read -r -p "Release tag (for example v0.0.12): " TAG
ROOT=$(git rev-parse --show-toplevel)
git fetch --no-tags origin "refs/tags/$TAG:refs/tags/$TAG"
test "$(git cat-file -t "$TAG")" = tag
COMMIT=$(git rev-parse "$TAG^{}")
RECEIPT="$ROOT/../gpt2agent-$TAG-$COMMIT.account-receipt.json"
test -f "$RECEIPT"
TAG_RECEIPT_SHA256=$(
  git for-each-ref --format='%(contents)' "refs/tags/$TAG" |
    sed -n 's/^account-receipt-sha256: \([0-9a-f]\{64\}\)$/\1/p'
)
test "${#TAG_RECEIPT_SHA256}" -eq 64
case "$TAG_RECEIPT_SHA256" in (*[!0-9a-f]*) exit 1;; esac
LOCAL_RECEIPT_SHA256=$(python - "$RECEIPT" <<'PY'
import hashlib
import pathlib
import sys

print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)
test "$LOCAL_RECEIPT_SHA256" = "$TAG_RECEIPT_SHA256"
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
