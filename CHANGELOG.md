# Changelog

All notable changes to this project will be documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [0.0.12] - 2026-07-11

Account-native discovery and release-safety update. The public surface grows
from 25 to 32 MCP tools and adds two deterministic MCP resources without adding
Voice, audio, browser-cookie export, or an OpenAI API fallback.

### Added

- Seven read-only discovery tools: `list_scheduled_tasks`, `list_plugins`,
  `list_installed_plugins`, `list_work_models`, `sites_access`, `list_sites`, and
  `account_capabilities`. They use bounded allowlists and typed contract errors
  rather than returning private backend payloads.
- Two packaged, account-independent MCP resources:
  `chatgpt://feature-coverage` records the release contract and
  `chatgpt://update-evidence` records the public-surface evidence snapshot.
- Every one of the 32 tools now declares all four MCP tool annotations:
  read-only, destructive, idempotent, and open-world hints.
- `chat` accepts an optional model-aware `thinking_effort`. A supplied value is
  checked against the selected general ChatGPT model's live catalog. Both
  `chat` and `agent` validate their selected general-model slug; Work-only model
  identifiers stay isolated from those paths.
- Safe machine-readable backend failures distinguish unavailable, unsupported,
  changed-contract, temporary, indeterminate-access, invalid-input,
  login-required, and unverified outcomes without retaining response bodies,
  URLs, headers, or account content.

### Changed

- `list_tasks` is now documented and validated as the generic asynchronous-job
  surface. Scheduled automations are a separate contract exposed by
  `list_scheduled_tasks`; neither tool claims to enumerate the other's jobs.
- Apps/connectors and Plugins are separate surfaces. `list_apps` accepts the
  observed string and object variants, while the two Plugin tools normalize the
  catalog and installed-plugin envelopes independently. Every projected Plugin
  string is secret/PII-redacted, while local pagination fingerprints bind the
  validated pre-redaction identities so catalog changes still fail closed.
- `list_work_models` reports Work metadata without merging Work-only identifiers
  into `list_models` or suggesting them for `chat`.
- The bundled Deep Research runner persists only the requested `report.md` and a
  shape-only `status.txt`. It no longer writes raw event or server-metadata
  artifacts, and its citation list now uses the same bounded, secret-filtering
  Markdown projection as the MCP Deep Research tools.
- The Python MCP dependency is bounded to the stable v1 line
  (`mcp>=1.26,<2`). CI verifies both 1.26.0 and the latest resolvable v1 release.
- Collection adapters now reject a blank 2xx body as a changed contract instead
  of presenting it as an honestly empty account collection.
- File metadata, account status, scheduled automations, conversation detail,
  and Codex task creation now return bounded allowlisted projections rather
  than opaque private-backend objects. Malformed backend-generated file IDs are
  classified as changed contracts before any URL is built.
- Plain `gpt2agent run` now defaults to stdio. Version 0.0.12 disables HTTP;
  the legacy launch flag fails before constructing the account server.
- The shell installer drives registration through the exact pipx-installed app
  path, so a successful upgrade is not rolled back merely because the pipx app
  directory is not yet visible in the current shell's `PATH`.
- Optional image-asset enrichment preserves typed changed-contract and
  indeterminate-access results while continuing to suppress exception details.

### Security

- Authorization is request-local. A multi-request operation captures one bearer
  snapshot so token rotation cannot mix authentication generations inside the
  same operation, and the shared HTTP session no longer stores Authorization.
- Account requests never follow redirects, preventing account headers or prompt
  bodies from being forwarded through an upstream redirect. The `curl_cffi`
  floor is now 0.15.0, excluding CVE-2026-33752.
- Private-backend JSON is capped at 4 MiB before retention, and account SSE
  streams are capped at 64 MiB with a 4 MiB pending-line limit. A process-wide
  request policy permits
  four ordinary REST/JSON requests in flight by default, accepts a bounded 1-8
  override, fails fast when saturated, and applies route-local 429 cooldowns
  with Retry-After capped at 60 seconds. Direct SSE/Sentinel streams remain
  separately bounded by endpoint timeouts and serial heavy-DR guidance.
- Network transport is disabled because loopback TCP cannot isolate the account
  from other local users or processes. Legacy HTTP launch and URL-install paths
  fail closed before server construction or configuration writes.
- The former raw SSE/poll dump is gone. Setting its legacy environment variable
  fails before account access and writes no file.
- Capability probes share one auth snapshot, a 90-second budget enforced by
  each request's remaining timeout, strict known GET routes, and shape-only
  results. Automatic capability and release-receipt probes do not request
  conversation summaries, memories, or custom instructions; those private
  surfaces remain available only through their explicitly invoked MCP tools.
  Voice, realtime, transcript, conversation body, write, unknown, and off-host
  routes are outside the probe contract.
- Visually hidden ChatGPT messages are excluded from streaming, async polling,
  conversation detail, tool-call, and Deep Research transcript output. Chat,
  Agent, and Custom GPT replies always end with one authoritative server receipt,
  using a bounded category or `none`; only the final footer is trustworthy.
  Private dispatch payloads remain hidden. Regular response text and heavy-DR
  progress are held until late visibility metadata can no longer revoke them.
  Unsupported late message mutations now revoke the buffered candidate,
  metadata JSON-pointer patches are bounded before allocation, malformed poll
  lifecycles fail closed, and a completed Deep Research widget is accepted only
  when its carrier remains the newest lifecycle.
- File-download projections accept only absolute public HTTPS destinations,
  while preserving valid signed queries. Common provider secrets,
  credential-bearing database URLs, label-aware assignments, and PEM private
  keys are redacted before private text reaches MCP clients.
- Image generation follows the observed prepare/conduit `/f` v1 stream and
  accepts an asset only when its visible carrier is bound to the current stream
  by the observed dispatch or a same-message marker and has image-generation
  provenance. These private routes remain undocumented and may change.
- Live validation on 2026-07-10 confirmed image generation in the authenticated
  website, while the direct client failed closed before prepare on changed
  required-Turnstile instructions. Catalog entitlement is not reported as live
  image execution reachability.
- Sentinel tokens and challenges have protocol-specific type, format, and size
  bounds before solver dispatch; malformed challenges and solver failures become
  static contract errors without retaining exception content.
- `custom_instructions_set` remains serialized within one server process. Two
  independently running gpt2agent processes can still race a read-modify-write;
  operators should avoid concurrent writers.

### CI / Release

- The stable `Required checks` gate now includes the full OS/Python suite,
  Ruff, ShellCheck, both MCP v1 compatibility lanes, and a wheel/sdist dry-run
  installed into clean environments.
- The locked package lane builds twice from cleaned state, normalizes bounded
  sdist container metadata with a dependency-free fail-closed rewriter, and
  requires byte-identical wheel and sdist pairs before retaining the first set.
- CI audits resolved application dependencies, the minimum supported
  `curl_cffi` release, and the hash-locked account-gate runtime for known
  vulnerabilities. A credential-free CI lane also bootstraps that runtime from
  scratch and verifies its exact distribution, file, ownership, mode, import,
  and isolated-CPython closure.
- A separate daily no-secret public-surface radar checks bounded official/public
  sources, emits only redacted summaries, retains evidence for 30 days, and does
  not pretend to verify a private account adapter.
- The private account gate runs only on a trusted local machine against the
  exact candidate commit. Its verifier-owned bounded transport reads the bearer
  directly and measures the active Pro entitlement instead of trusting a caller
  label; candidate wheel/sdist bytes are never installed, imported, or executed
  on that credential host. Main CI runs one closed synthetic adapter corpus
  against both package formats and requires identical output. The sanitized
  receipt keeps live shape classes separate from fixed offline adapter counts,
  is freshness-bounded before tagging, and remains local. The live transport is
  loaded only from a new owner-private, hash-locked CPython 3.12 environment
  under `-I -S -B`; the short-lived release App token never reaches Python or a
  process argument.
- The read-only governance audit binds the release-tag App, Required-checks App,
  and independent PyPI gate to an explicit reviewed policy. Tag creation and
  no-bypass tag immutability are audited as separate rules, and main-branch
  bypasses, stale approvals, unresolved threads, or non-strict checks fail the
  release gate.
- The release coordinator independently revalidates the pinned candidate and
  receipt before creating a new annotated tag through the policy-bound App. Its
  tag message is one exact canonical, duplicate-key-free, closed-schema JSON
  envelope binding repository, tag, version, source commit/tree, receipt and
  artifact-set digests, and complete candidate identity. Release evidence binds
  that metadata to the tag object, workflow identity, and exact wheel/sdist
  hashes. The receipt is never attached; its digest is the public commitment.
- GitHub publication uses the exact numeric draft-release ID for every asset and
  metadata operation, validates the complete release before and after making it
  public, requires the immutable public readback, and permits an already-public
  rerun only for an exact match. The publication action is pinned by its full
  commit SHA. Independent restricted tag creation and protected-environment
  approval remain required release controls.
- GitHub Release notes are extracted by the same exact-version CHANGELOG parser
  used by release metadata verification, so regex-like version near-matches
  cannot select another section.

### Migration

- Remove `GPT2AGENT_ALLOW_REMOTE` and `GPT2AGENT_RAW_DUMP` from current launch
  scripts. Both legacy variables now fail closed; the first cannot enable a
  non-loopback bind and the second cannot create diagnostic files.
- Use stdio for local MCP clients. Remove existing HTTP service units and rerun
  `gpt2agent install` to restore a spawned stdio entry. Network support requires
  request authentication or an equivalent per-user boundary.
- Deep Research automation should consume `report.md` plus `status.txt`, not
  `events.jsonl` or `meta.json`.
- Voice remains outside 0.0.12. A bounded read-only voice catalog is planned for
  0.0.13; GPT-Live audio is not exposed as a supported MCP/account capability,
  and later AgentRTC/WebRTC work is a separate transport project.
- See [`docs/migration-0.0.12.md`](docs/migration-0.0.12.md) for the upgrade
  checklist and compatibility notes.

## [0.0.11] - 2026-07-10

Recovery release carrying forward every change in the
[`0.0.10` changelog section](https://github.com/robotlearning123/gpt2agent/blob/v0.0.11/CHANGELOG.md#0010---2026-07-10).
Version `0.0.10` was tagged but never published to PyPI or as a GitHub Release
because its source gate inspected a runner-local tag ref that
`actions/checkout` had rewritten to the peeled commit.

### Fixed

- Release source verification now reads the annotated tag's peeled target from
  the remote, binds it to the workflow event SHA, and proves that exact commit
  is on `origin/main`. This remains correct even when checkout rewrites its
  runner-local tag ref, while still rejecting lightweight, mismatched, and
  off-main tags.

## [0.0.10] - 2026-07-10

Patch release from the 2026-07-09 end-to-end security, correctness,
distribution, and release-pipeline audit.

### Security

- File and conversation IDs are validated as single URL-safe path segments
  before any backend request, closing path traversal and percent-escape routes.
- Memory search now compares only redacted text, and custom-instruction updates
  return a minimal acknowledgement instead of a secret-bearing backend echo.
- Raw SSE diagnostic files are created or tightened to mode `0600` on POSIX;
  widget reports are accepted only from Deep Research-specific backend
  envelopes, so arbitrary assistant or tool content with the same JSON shape
  is ignored. The distributed parser fixture now contains only synthetic data.
- Manual token entry uses hidden input and strict JWT-shape validation;
  noninteractive token lookup never launches browser automation.

### Fixed

- REST-backed MCP handlers offload synchronous backend calls, keeping the async
  server responsive while preserving the existing 25-tool surface.
- Chat and tool streams reject truncated EOF unless an explicit terminal signal
  or independently verified polling result proves completion. Heavy Deep
  Research ignores connector-dispatch envelopes, propagates connector failures,
  and the bundled runner records timeouts and abnormal endings as `INCOMPLETE`.
- Light Deep Research discards clarification rounds before selecting a final
  report, and newer partial/tool activity supersedes any stale completed
  candidate. Empty or unresolved follow-ups now terminate as `INCOMPLETE`.
- Required Sentinel challenges now fail closed and run their solvers off the
  event loop. Conversation caps apply after filtering visible messages, and
  large date-heavy redactions no longer recurse. Turnstile timing state is
  local to each solve, avoiding races between concurrent challenges.
- Token refresh re-evaluates source priority safely, browser helper discovery is
  portable, and account setup reports only backend-verified plan state. The
  bundled Deep Research wrappers accept either supported token source, and
  quota checks fail visibly when the account value cannot be verified.
- Custom-instruction partial updates serialize their full read-modify-write
  window so concurrent requests cannot silently lose an unrelated field.
- The shell installer distinguishes explicit local sources from the default
  PyPI install, fails closed instead of substituting mutable repository code,
  passes its selected compatible Python to pipx, installs skills transactionally
  without bytecode, honors `CODEX_HOME`, and supports `python -m gpt2agent`.
  Replacing an existing pipx environment also removes its injected packages.
- Bundled Deep Research runs use collision-resistant private output directories
  and private files, reject symlinked artifacts where the platform supports
  no-follow opens, and accept approved multi-line queries over stdin without a
  shared temporary file.
- Source distributions include a self-contained parser test and its fixtures so
  the built sdist is exercised before publication.

### CI / Release

- One verifier now requires all package, plugin, server, tag, and changelog
  versions to agree. Supported SemVer prereleases are normalized at Python
  distribution and PyPI boundaries. CI exposes a stable aggregate required
  check, adds Windows package/import smoke coverage, and pins every third-party
  action to an immutable commit.
- Release tags must be annotated and originate on `origin/main`; clean environments install
  and test both built artifacts before trusted publishing. Existing and newly
  published PyPI filenames and SHA-256 hashes must match the original build,
  and a rebuilt workflow may publish only while the version is wholly absent,
  allowing failed jobs in the same workflow run to retry safely while full
  workflow reruns after publication fail closed. Missing release notes fail the workflow.
  GitHub release tags are immutable, and the PyPI environment is configured for
  `v`-prefixed tag deployments.

## [0.0.9] - 2026-07-02

Patch release: 2026-07-02 audit round — PII-redaction correctness, robustness
against empty backend responses, Codex TOML config-editing fixes, and honest
failure reporting (truncated Deep Research reports, invalid pasted tokens).
Source-only test coverage baseline recorded at 59% (168 tests).

### Security

- `get_conversation` now redacts message `text`/`code` bodies (emails, phones,
  JWT / bearer / API-key / GitHub-token shapes), matching the already-redacted
  `title` and the skill-doc claim. Redaction runs before truncation so a secret
  straddling the 2000-char cut cannot leak as a partial token.

### Fixed

- REST tools no longer crash with a raw `AttributeError`/`TypeError` when the
  backend returns an empty 2xx body (`backend.get()` → `None`): read tools
  (memory, account, models, custom GPTs, apps, codex lists, instructions-get)
  degrade to empty results, and `custom_instructions_set` now refuses to
  overwrite when the current state is unreadable instead of silently clearing
  the field the caller did not supply.
- `load_config` raises a clean, actionable `ValueError` for a top-level scalar
  key in `config.toml` (e.g. `port = 9001` missing its `[server]` header)
  instead of `TypeError: 'bool' object is not iterable`.
- Codex TOML section remove/replace now treats dotted child subtables
  (`[mcp_servers.x.env]`) as part of the section. The legacy
  `[mcp_servers.openai]` cleanup no longer leaves an orphaned command-less
  half-entry Codex would spawn-and-fail on, and re-install no longer keeps a
  stale `.env` subtable from the replaced block.
- Codex TOML section remove/replace now recognizes the target header when it
  carries a trailing comment (`[mcp_servers.gpt2agent] # managed`). Previously
  re-install appended a DUPLICATE table — making the whole
  `~/.codex/config.toml` invalid TOML ("Cannot declare ... twice") — and the
  legacy `[mcp_servers.openai] # comment` cleanup printed "removed" while
  removing nothing.
- Legacy Deep Research no longer misclassifies a long real report whose prose
  contains an ordinary hint phrase ("to make sure", "would you like") as a
  clarification request — that auto-proceed round overwrote the real report
  and burned DR quota. Clarification detection now applies only to short
  (≤ 1200 char) done-texts.
- PII redaction no longer corrupts calendar dates: "2026-05-26" (and
  `DD-MM-YYYY` / datetime / date-range forms) satisfied the phone-number
  pattern and came back as `<PHONE>` in memories, tasks, and conversation
  titles. Phone masking still applies to actual phone shapes — including a
  phone that follows a date inside the same greedy match
  ("2026-05-26 617-555-0123" keeps the date, masks the phone).
- `deep_research` / `deep_research_heavy` now append an explicit "⚠ Report
  may be incomplete" note when the SSE stream ended without the server
  marking the response finished (`terminated_abnormally`) or when completion
  polling timed out — previously a truncated report was indistinguishable
  from a complete one.
- `gpt2agent install`: writing a symlinked agent config (dotfile-repo
  setups) now writes through to the symlink target instead of replacing the
  link with a plain file and stranding the real config.
- `gpt2agent setup`: `~/.gpt2agent/config.toml` is backed up
  (`.bak-gpt2agent`) before being overwritten and is written atomically;
  a rewrite with identical content is a no-op.
- Removed the `browser-use` session-cookie fallback that saved a NextAuth
  session cookie as `access_token` — the saved "token" 401'd on every API
  call. Extraction now fails visibly so the user can paste a real token.
  The manual paste prompt likewise no longer suggests the
  `__Secure-next-auth.session-token` cookie and refuses to save values that
  are not 3-segment JWTs (a pasted session cookie only produced 401s).

## [0.0.8] - 2026-06-27

Patch release: follow-up hardening from the 2026-06-26 cross-model audit of 0.0.7.

### Security

- **Secret-bearing config and token writes are tighter.** Installer backups preserve
  source file modes, atomic temp files are created at `0600`, and setup/auth token
  saves tighten existing `~/.gpt2agent/token.json` files that were previously wider.
- **More user-returned secrets are redacted.** Tool output redaction now masks common
  JWT, bearer, API-key, and GitHub-token shapes in conversation, memory, task, and
  instruction surfaces, including fine-grained `github_pat_` tokens. SSE in-band
  error frames are now raised with redacted text.

### Fixed

- `backend.get()` now handles empty/non-JSON 2xx responses like `post()`, returning
  `None` for empty bodies and raising a clean redacted error for HTML/gateway pages.
- Heavy Deep Research captures top-level `conversation_id` frames before polling,
  supports array-index metadata patches for citations, and can finish when a real
  report replaces a connector-dispatch placeholder in the same message. Dispatch
  JSON delivered through content patches is no longer emitted as progress.
- `chat` no longer waits through the agent-mode async poll window on a real empty
  response; only `agent` opts into async polling.
- Stream parsers tolerate `{"message": null}` patch frames and surface common
  in-band SSE error frames instead of silently ignoring them.
- `gpt2agent setup` now recognizes every saved token shape accepted by the runtime
  backend (`access_token`, `token`, or nested `tokens.access_token`).
- `auth.get_token()` now prefers Codex auth before saved `~/.gpt2agent/token.json`,
  matching the backend/setup/docs and avoiding stale-token or wrong-account drift.
- Codex TOML install replacement preserves following `[[array-of-table]]` sections.
- `get_conversation` follows the active `current_node` branch instead of raw mapping
  order, keeps the newest turns when `max_messages` is capped, and `list_apps`
  preserves explicit `is_connected: false`.
- Codex TOML section editing now treats `[section] # comment` and
  `[[array]] # comment` as section boundaries, preserving following sections.
- Explicit missing `--config` paths now fail loudly instead of falling back to defaults.

### Tests

- Added focused regressions for the audit fixes and parser edge cases. Offline suite:
  129 passed, 9 skipped. `ruff check gpt2agent tests` is clean.

## [0.0.7] - 2026-06-18

Patch release: fixes surfaced by a large-scale cx/cx2/ccz parallel verification of 0.0.6.

### Security

- **Broader secret redaction.** `redact_error` now masks base64 bearer tokens
  (`Bearer …+ab/cd==`, previously the tail leaked) and more token query-param names
  (`accessToken`, `session_token`, …). A `{16,}` length floor avoids mangling ordinary
  prose after the word "Bearer".

### Fixed

- **`.claude-plugin/marketplace.json`** plugin `source` `"."` → `"./"` to satisfy the
  canonical claude-code-marketplace JSON schema (the lenient CLI had accepted `"."`).
- **Skill docs corrected to match shipped behavior** (the skills install to
  `~/.claude/skills/` and ship in the wheel): the deep-research SKILL.md 429/poll-timeout
  recovery section, and `tools-reference.md`'s `deep_research_heavy` "Known limitation" +
  Gotcha #6 now describe the connector widget-state recovery (not a "citation bug");
  `gpt_chat` documents passing the `short_url` from `list_custom_gpts` (not a `g-p-*` id).

### Tests

- +3 redaction regression tests (base64 bearer, camelCase query token, prose preservation).
  Suite: 102 passed, 9 skipped.

## [0.0.6] - 2026-06-18

### Packaging / distribution

- **Claude Code plugin**: `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`
  bundle the MCP server + both skills as one `/plugin install gpt2agent@gpt2agent`
  (after `/plugin marketplace add robotlearning123/gpt2agent`). Validated with
  `claude plugin validate --strict`. Schemas verified against the official Claude
  Code plugin docs.
- **MCP registry descriptor**: `server.json` declares the PyPI package `gpt2agent`
  as a stdio server (with `run --stdio` packageArguments) for the official MCP
  registry. Validates against the live `2025-12-11/server.schema.json`. The README
  carries the `mcp-name` ownership marker. (Registry is in preview; publishing is an
  owner step via `mcp-publisher`.)

### Added

- **More MCP hosts auto-install.** `gpt2agent install` now registers Cursor,
  Windsurf, Claude Desktop, and Zed (in addition to Claude Code and Codex) via a
  shared idempotent JSON registrar; `detect_clients()` finds them for `--client all`.
- **`gpt2agent --version`.**
- **User docs** under `docs/` (quickstart, clients, configuration, troubleshooting,
  FAQ, how-it-works) and a CI badge in the README.
- **CODE_OF_CONDUCT.md**; ruff added to the `dev` extra with a `[tool.ruff]` config;
  richer pyproject classifiers/authors.

### Changed

- **`gpt2agent setup` no longer starts a background HTTP daemon / macOS LaunchAgent
  or registers the legacy `mcpServers.openai` HTTP entry.** It now pastes the token
  and registers clients over **stdio**, matching `gpt2agent install`.
- Tool docstrings clarified for agents: `gpt_chat` takes the `short_url` from
  `list_custom_gpts` (not a `g-p-*` id); `list_models` documents `slug` as the
  `chat(model=)` input; `account_status`/`deep_research_heavy` document returns +
  the citation caveat. README no longer claims a non-existent `account_status` "MFA".

### Fixed

- `install.sh` no longer aborts under `set -e` on PEP-668 Linux when bootstrapping
  pipx (and prints distro install commands); `-h` no longer leaks shell lines.
- Bundled `gpt2agent` skill config example: `host` `0.0.0.0` → `127.0.0.1`.
- Removed the dead `gpt2agent login --browser` hint; `CONTRIBUTING.md` stale test count.

### Tests

- New `tests/test_tools.py` (27 hermetic tests) covering the entire MCP tool layer +
  server SSE handlers + the PII-redaction and `temporary=False` invariants (was zero
  coverage). Plus install tests for the new hosts. Suite: 96 passed, 9 skipped.

## [0.0.5] - 2026-06-18

### Security

- **Default bind is now `127.0.0.1` (was `0.0.0.0`).** The HTTP transport is
  unauthenticated and proxies a full ChatGPT account; the server now refuses to
  start HTTP on a non-loopback host unless `GPT2AGENT_ALLOW_REMOTE=1` is set, and
  prints a loud warning when it does. `config.example.toml` ships loopback. Use
  the stdio transport (the `install` default) for local clients.
- **Broader secret redaction.** `redact_error` now also strips bare `Bearer`
  tokens, named JSON token fields (`access_token`/`session_token`/…), auth/session
  cookies, and token query params. `backend.py` and the streaming SSE error paths
  now route bodies through it (previously raw `r.text`/`body[:500]`).
- Removed internal agent-orchestration notes (`docs/goals/*.md`) from the repo.

### Fixed

- **Heavy DR widget parser hardening.** `_dr_report_from_widget_state` now only
  trusts `tool`/`assistant` nodes, requires the widget prefix to *start* the part
  (not appear anywhere), and emits only a *finished* report — closing a
  DR-report-spoofing vector (a user message could fake the final report) and a
  premature-`done` on in-progress drafts.
- `agent`/`chat`/`gpt_chat`/`memory_create_via_chat` return `"(no response)"`
  instead of an empty string on timeout. `stream()` is typed `AsyncIterator[str | dict]`.
- `CODEX_HOME` is now honored in `auth.py`/`setup.py` (was already in `backend.py`).
- `install.sh` falls back to a `git+https` install when PyPI returns 404, so the
  one-line installer works before the package is published.

### Docs / project

- README: new **Security & risk** section (ToS/account-ban, unauthenticated HTTP,
  redaction limits); honest "(emails/phones redacted)" wording; reconciled the
  PyPI auto-publish claim with the required one-time Trusted Publisher setup.
- Added `SECURITY.md`, issue templates, and a PR template. Lint clean; test suite
  60 passed / 9 skipped (+14 hardening tests).

## [0.0.4] - 2026-06-11

### Fixed

- **Heavy Deep Research returns the real report (connector-widget architecture).**
  ChatGPT moved heavy DR to the "Deep Research App" connector
  (`connectors://connector_openai_deep_research`), which renders the report in an
  embedded widget and **never** writes it as an assistant text node — so the old
  `_poll_dr_completion` (which only scanned assistant text) timed out at 1800s
  with an empty report even though the research finished server-side. The report
  actually lives in the hidden widget state (`widget_state.report_message`).
  `_poll_dr_completion` now fetches the conversation with
  `?include_visually_hidden_messages=true&include_widget_state=true` and recovers
  the report (text + `content_references`) from either widget-state carrier (a
  `"The latest state of the widget is: {…}"` tool node, or
  `message.metadata.chatgpt_sdk.widget_state`) via the new
  `_dr_report_from_widget_state` helper. Verified by recovering three real
  completed reports headlessly (45.6K / 52.4K / 51.5K chars, with citations).

### Notes

- Light `deep_research` (`model=research`, SearchGPT backend) is a separate
  mechanism and is **not** changed here; its longest-`done` mitigation from 0.0.3
  remains. A dedicated light-mode fix is tracked as a follow-up.

## [0.0.3] - 2026-06-03

### Fixed

- **Light Deep Research report extraction.** The research model often emits a
  short tool-dispatch JSON as the FIRST `done` event (e.g.
  `{"queries":[...],"source_filter":[...]}`); the bundled runner took that first
  done as the report and silently discarded the real, later report. It now picks
  the longest `done` and falls back to all streamed progress text.

### Added

- **Multi-account auth via `CODEX_HOME`.** `BackendClient` reads
  `$CODEX_HOME/auth.json` (falling back to `~/.codex/auth.json`), so a second
  account (e.g. `CODEX_HOME=~/.codex-cx2`) can be used without touching the
  default login. Adds 2 tests.
- **`GPT2AGENT_RAW_DUMP` heavy-DR diagnostics.** When set, `deep_research_heavy`
  writes every raw SSE object (Phase 1) and conversation-detail poll (Phase 2)
  as JSONL — used to reverse-engineer the real heavy-DR shape.
- Heavy-DR poll backs off ≥300s on HTTP 429 and polls every 120s (was 15s) to
  avoid rate-limiting the conversation endpoint.
- Best-effort heavy-DR citation extraction: `done` pulls
  `content_references` / `search_result_groups` from nested
  `/message/metadata/...` patches or a same-turn conversation-detail node, if
  the backend provides them.

### Known limitations (verified 2026-06-03)

- **Heavy DR does not return a programmatically-fetchable report.** Verified on a
  clean Pro account: `deep_research_heavy` dispatches the
  `connector_openai_deep_research` connector, which renders an "embedded UI
  experience" widget; after a full 30-minute run the conversation's report node
  stayed empty (`content_references: []`, 0-char text) and no `done` event was
  emitted. The report exists only inside the ChatGPT web UI's deep-research
  experience, not in `/backend-api/conversation/{id}`. **Use light mode
  (`deep_research`) for programmatic, cited research.** The best-effort citation
  extraction above is dormant until/unless the backend exposes refs on that path.
- Run heavy/long DR **serially** — concurrent DR + codex jobs on one account
  cause HTTP 429 on the conversation poll. See the deep-research skill's
  "Account limits & concurrency" note.

## [0.0.2] - 2026-05-26

### Renamed — `openai-mcp` → `gpt2agent`

The package, CLI, Python module, default MCP server-name key, and bundled
Claude Code skill are all renamed:

| Was | Now |
|---|---|
| PyPI `openai-mcp` | PyPI `gpt2agent` |
| `pip install openai-mcp` | `pip install gpt2agent` |
| `openai-mcp run --stdio` | `gpt2agent run --stdio` |
| `openai-mcp install` | `gpt2agent install` |
| `from openai_mcp.…` | `from gpt2agent.…` |
| Claude Code key `mcpServers.openai` | `mcpServers.gpt2agent` |
| Codex key `[mcp_servers.openai]` | `[mcp_servers.gpt2agent]` |
| Skill `~/.claude/skills/openai-mcp/` | `~/.claude/skills/gpt2agent/` |
| Config dir `~/.openai-mcp/` | `~/.gpt2agent/` |

**Why:** "openai" is a registered OpenAI® trademark; PyPI may take the
package down under their trademark policy and there's no implied
affiliation. The new name continues the `chatgpt2agent` repo naming
pattern (drop "chat") and reads as "GPT to agent" — i.e. the package
makes any GPT account addressable as an agent.

**Migration for users on 0.0.1 (`openai-mcp`):**
```bash
pipx uninstall openai-mcp
pipx install gpt2agent
gpt2agent install              # registers under new key in client configs
# Optional cleanup:
mv ~/.openai-mcp ~/.gpt2agent  # if you have a config dir from 0.0.1
# Manually remove the stale "openai" entry from ~/.claude.json
# mcpServers (the old binary no longer exists).
```

### Added — one-command install for popular MCP clients

- `gpt2agent install` subcommand — registers gpt2agent with one or more
  MCP-capable agent clients. Targets in 0.0.2:
  - **Claude Code** — writes `~/.claude.json` `mcpServers.gpt2agent` entry
    (preserves all other top-level keys, backs up to `.bak-gpt2agent`).
  - **Codex CLI** — writes `~/.codex/config.toml` `[mcp_servers.gpt2agent]`
    section, preserving all other sections (agents, model config, …).
  - `--client all` (default) auto-detects which clients are installed and
    registers with each.
  - Idempotent: re-running yields the same final config; no duplicate
    sections or entries.
  - `--dry-run` prints what would change without touching files.
  - `--transport http --http-port N` switches to an HTTP entry.
- **Bundled Claude Code skill** at `gpt2agent/skills/deep-research/` —
  ships in the wheel; `gpt2agent install` (or `install --client claude-code`)
  copies it to `~/.claude/skills/deep-research/`. The skill calls
  ConversationClient directly, so it works even before Claude Code restarts.
- **One-line installer** (`install.sh`) — cross-platform replacement of the
  prior macOS-only LaunchAgent script. Pipes through pipx install + codex
  login check + `gpt2agent install`. Curl-pipe-bash friendly.
- **GitHub Actions release workflow** (`.github/workflows/release.yml`) —
  triggers on `v*` tags, verifies the tag matches `pyproject.toml`, runs
  pytest across {ubuntu, macos} × {Python 3.11, 3.13}, builds wheel + sdist,
  publishes to PyPI via OIDC trusted publishing (no token in secrets), and
  creates a GitHub Release with the matching CHANGELOG section as body.
  Auto-marks pre-releases when tag contains `-rc` / `-alpha` / `-beta`.
- **CI workflow** (`.github/workflows/ci.yml`) — runs tests on push/PR to
  main across {ubuntu, macos} × {3.10–3.13}, plus shellcheck on `install.sh`.

### Added — full ChatGPT account surface

- `agent` tool — ChatGPT Agent Mode (262K context, autonomous browsing + code
  execution + tool use). Streams via the same SSE path as `chat` with
  `model=agent-mode`.
- `gpt_chat(gizmo_id, prompt)` tool — chat through one of your private Custom
  GPTs (`g-p-*`). Routes via `gizmo_id` + `conversation_origin` payload fields.
  *Experimental* — field shape reverse-engineered from chatgpt.com web bundle.
- `memory_create_via_chat(content)` tool — workaround for `POST /backend-api/memories`
  returning 405. Asks the model to commit content to memory and relies on
  ChatGPT's model-initiated memory feature.
- `[models].heavy_dr` config key — override the slug used for
  `deep_research_heavy` (default still `gpt-5-5-pro`).
- `[models].agent` config key — override the slug used for `agent` (default
  `agent-mode`).
- `deep_research` / `deep_research_heavy` gained `auto_confirm: bool = True`
  parameter — prefixes an imperative directive so the model starts the
  research without asking "Do you want me to proceed?".
- Token auto-reload: `BackendClient._reload_token_if_stale()` watches the
  mtime of `~/.codex/auth.json` and re-reads on change, so codex's background
  refresh propagates without needing to restart the MCP server. Called before
  every `get()` / `post()`.
- **Multi-turn DR clarification handling** in `ConversationClient.deep_research`.
  ChatGPT's `research` model often opens with a clarifying question instead of
  starting research immediately ("Could you confirm…?"). The wrapper now
  detects clarification-shaped `done` events (short text + question mark, or
  matching phrase list), captures the conversation_id + assistant message id,
  and auto-replies "Proceed with your best interpretation. Do not ask further
  clarifying questions." in the same conversation thread — then continues
  streaming until the real report. Capped at 2 rounds; surface
  `{"type": "clarification_auto_reply", "round": N, "question": text}` events
  so callers can see what happened. Without this, single-turn DR calls
  terminated on the clarification question and never saw real research.

### Fixed

- **heavy DR returned dispatch JSON instead of the real report** — `_emit_done`
  fired on the connector-dispatch envelope (`{"path": ".../connector_openai_deep_research/start", ...}`)
  before the actual report started streaming. Gated with `is_connector_dispatch`
  so the dispatch envelope's `finished_successfully` is suppressed. Real
  report now streams correctly (test: `tests/test_heavy_dr_parser.py`).
- **`Research is not currently supported in temporary chats`** — both DR payload
  builders were forcing `history_and_training_disabled = True`, which marks the
  conversation as Temporary Chat. ChatGPT server then rejected DR. Both DR
  paths now set this field to `False` (regular `chat` keeps `True` for privacy).
- **Quota probe blocked the asyncio event loop** — `init_data = self._backend.post(...)`
  is a synchronous `curl_cffi` call; wrapped in `asyncio.to_thread`.
- **Backend tool registration failures hid the cause** — `server.py` was
  catching `Exception` with `logging.warning` (no traceback). Switched to
  `logging.exception` so import / auth / wiring errors are diagnosable.

### Changed

- README repositioned around the `codex login` story — full account access in
  any MCP client with zero extra credentials.
- Tool table reorganized by category (chat, account, memory, codex).
- Removed dead `image_gen` placeholder from `server.py` (kept as a tracked
  future PR in CHANGELOG instead of in source).
- **25 MCP tools** (up from 19). New tools: `generate_image`, `code_interpreter`,
  `canvas_execute`, `get_conversation`, `get_file_info`, `get_file_download_url`.
- `list_models` returns full metadata: capabilities, enabled_tools, thinking_efforts, tags.
- `list_tasks` returns full metadata: prompt, conversation_id, image_gen_message, interruptions_disabled.
- `conv` singleton passed to tool modules — eliminates redundant sentinel token fetches.
- Image poll loop has exponential backoff (max 5 consecutive errors before raising).
- `temporary` param added to `_build_payload`, `stream`, `complete` — tools that need
  persistent conversations (image gen, code interpreter, canvas, agent, memory) pass `temporary=False`.

### Fixed (post-review)

- `history_and_training_disabled=True` blocked image gen, code interpreter, and canvas —
  new `temporary` param lets tools opt into persistent conversations.
- `tool_call` crashed on SSE frames with `content: null` — null-safety fix (`or {}`).
- `agent()` used `temporary=True`, silently disabling browsing and code execution — now `False`.
- `memory_create_via_chat` used `temporary=True`, preventing memory persistence — now `False`.
- `generate_image` sync HTTP calls blocked the async event loop — wrapped in `asyncio.to_thread`.
- `image_gen` SSE timeout was 120s (too short for complex prompts) — increased to 300s.
- `sediment://` stripping used global `replace` instead of `removeprefix`.
- `tool_call` returned empty text when stream lacked `finished_successfully` —
  added `text_parts` fallback: `last_text or "".join(text_parts)`.
- `conversations.py` syntax error (missing `return`) crashed all tool registration.
- `list_conversations` and `list_tasks` crash on API returning JSON `null` — added `or {}` safety.

## [0.0.1] - 2026-04-24

### Added

- Native Python `/backend-api/conversation` SSE client (no proxy dependency)
- Vendored SHA3-512 proof-of-work + Turnstile solvers (MIT from lanqian528/chat2api)
- 15 MCP tools: `chat`, `deep_research`, `deep_research_heavy`, `account_status`,
  `list_models`, `memory_list`, `memory_search`, `custom_instructions_get`,
  `list_codex_envs`, `list_codex_tasks`, `list_custom_gpts`, `list_conversations`,
  `list_tasks`, `list_apps` (image_gen stub hidden pending implementation)
- Deep Research heavy variant via `/backend-api/f/conversation` +
  `connector:connector_openai_deep_research`
- `curl_cffi` TLS impersonation for Cloudflare + Sentinel bypass
- Codex auth token reuse (`~/.codex/auth.json` → `tokens.access_token`)
- Setup wizard fallback to `~/.openai-mcp/token.json`

### Changed

- Dropped chatgpt2api proxy dependency (was HTTP localhost:9000)
- Dropped openai SDK runtime dependency
- Server is now stdio-first (`openai-mcp run --stdio` for MCP clients)

### Security

- User-Agent / TLS impersonation standardized on Chrome 131
- Session tokens redacted from exception messages
- `history_and_training_disabled=True` by default on all conversation requests
