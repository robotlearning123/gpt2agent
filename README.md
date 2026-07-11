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
Version 0.0.12 supports stdio only; URL registration fails before changing a
client configuration.

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
| `list_installed_plugins` | Installed Plugins with bounded allowlisted fields and no Plugin content or secrets |
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
host = "127.0.0.1"   # retained for compatibility; stdio does not bind it
port = 9000          # retained for compatibility; stdio does not bind it

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
- **Version 0.0.12 disables HTTP.** Loopback TCP is reachable by other users and
  processes on the same host, so Host/Origin checks are not account
  authentication. `--http` and URL installation fail closed before the account
  server starts or client configuration changes. **Use stdio**, which lets the
  MCP host own a private child process. A future network transport must add
  strong per-launch authentication before it can be supported.
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
machine against its exact merge SHA. Main CI independently builds two
wheel/sdist sets, deterministically normalizes each sdist, requires both formats
to be byte-identical across builds, and retains only the first set under an
attempt-specific, immutable artifact ID. In its credential-free package job,
main CI installs both retained formats, runs the same closed synthetic adapter
corpus against each, and requires byte-identical corpus results. The local
account gate downloads that exact candidate but treats both files as inert
bytes: it never installs, imports, builds, or executes them.

Bootstrap the verifier from `requirements-account-gate.txt` into a new
owner-private CPython 3.12.13 for Linux x86_64 environment outside the checkout.
Every ancestor from that runtime to `/` must be root- or operator-owned and must
not be group/world-writable or a symlink. The operator accepts only the pinned
archive; its reviewed executable and full-tree digests are constants rather than
caller-provided trust assertions.

There are two explicit external provenance boundaries. Hosted CI trusts the
pinned `actions/setup-python` action on its isolated runner, immediately copies
the complete 3.12.13 runtime out of the writable tool cache into an owner-private
directory below the canonical runner home, and binds the copy to the source
executable digest. For the local operator, v0.0.12 pins Astral's attested
[`python-build-standalone` 20260510 release](https://github.com/astral-sh/python-build-standalone/releases/tag/20260510),
asset `cpython-3.12.13+20260510-x86_64-unknown-linux-gnu-install_only.tar.gz`.
The archive SHA-256 is
`e7332b4b4bb85006deb48d251c786a04c14de104c9b3a006b33457a4a604b8bc`;
the normalized executable SHA-256 is
`f7014f68e3c8f180811740735cf1dd5c28be6cff84db11d0ced2a8cd039670a0`;
and the normalized full-tree SHA-256 is
`74e93975be819af02939878b97bafb7aa7961adfa31ef7c47845d25e2b88fc07`.
This boundary trusts Astral's published build workflow and attested runner in
addition to the upstream CPython source; it does not claim a PSF binary build.
Set only `GPT2AGENT_TRUSTED_PYTHON_ARCHIVE` to a protected local copy of that
exact asset. The reviewed extractor authenticates the bytes, topology, symlink
map, executable, and complete normalized tree before use.
The bootstrap then accepts only the reviewed nine-distribution closure, exact
hashes, binary wheels, and the official PyPI index, and verifies every installed
file and import origin. It emits only the trusted `site-packages` path on stdout.
The live gate loads `curl_cffi` from that explicit path under
`python -I -S -B`, keeps the reviewed local bearer in process, disables
environment proxy discovery and automatic redirects, and accepts only the exact
reviewed `chatgpt.com` GET routes with bounded metadata, 4 MiB response bodies,
fixed timeouts, and TLS verification.

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

The trusted release machine must provide the separately reviewed governance
policy, an authenticated `/usr/bin/gh`, the reviewed Pro account login, and a
local copy of the exact pinned Astral 20260510 CPython 3.12.13 Linux x86_64
install-only archive. The reviewed extractor pins its size, SHA-256, member
topology, symlink map, executable SHA-256, and normalized
`hash_runtime_tree.sh` v1 full-tree SHA-256. It extracts only into an
owner-private disposable directory; callers cannot substitute trust hashes.

Create a private evidence directory, then delegate the complete account gate and
tag handoff to the reviewed operator. It re-executes under `env -i` and Bash
privileged mode before parsing arguments, fetches the merged commit into a
disposable detached worktree, rejects hidden Git index state, and executes only
root-owned `/usr/bin/gh`, `/usr/bin/git`, and `/usr/bin/python3.12`. The release
App token is requested only after every read-only gate passes and is never passed
to Python or placed in process arguments.

```bash
install -d -m 700 "$HOME/gpt2agent-release-evidence"

scripts/run_account_release.sh \
  --repository robotlearning123/gpt2agent \
  --pr "$PR_NUMBER" \
  --operator-home "$(realpath -e "$HOME")" \
  --codex-home "$(realpath -e "${CODEX_HOME:-$HOME/.codex}")" \
  --evidence-directory "$(realpath -e "$HOME/gpt2agent-release-evidence")" \
  --trusted-python-archive "$(realpath -e "$GPT2AGENT_TRUSTED_PYTHON_ARCHIVE")" \
  --governance-policy "$(realpath -e "$GPT2AGENT_RELEASE_GOVERNANCE_POLICY")" \
  --gh /usr/bin/gh \
  --git /usr/bin/git
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
and `main` has no bypass actor. A distinct policy-bound GitHub App must read
immutable-release settings with exactly Administration read plus GitHub's
implicit Metadata read and no subscribed events. Install it only on this
repository. The `release-settings-read` environment must accept only `v*` tags,
have no reviewer, wait timer, or custom deployment gate, disable administrator
bypass, and contain exactly the policy-bound
`GPT2AGENT_RELEASE_SETTINGS_APP_CLIENT_ID` variable and
`GPT2AGENT_RELEASE_SETTINGS_APP_PRIVATE_KEY` secret. Do not reuse this reader as
the tag creator, required-check App, or PyPI protection actor. The workflow's
built-in token remains the only release read/write credential; the App token is
accepted only by the immutable-settings GET allowlist. The approver must verify
the exact tag, commit, tree, version, and receipt digest before allowing
publication. Run `scripts/audit_release_governance.py` with
`--live OWNER/REPO`, `--policy POLICY.json`, and `--gh /usr/bin/gh` to perform
these reviewed, read-only GitHub checks. It exits nonzero when the closed
identity policy or any required live control is absent.

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
scripts/audit_retained_receipt.sh \
  --repository robotlearning123/gpt2agent \
  --tag v0.0.12 \
  --operator-home "$(realpath -e "$HOME")" \
  --evidence-directory "$(realpath -e "$HOME/gpt2agent-release-evidence")" \
  --trusted-python-archive "$(realpath -e "$GPT2AGENT_TRUSTED_PYTHON_ARCHIVE")" \
  --git /usr/bin/git
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
