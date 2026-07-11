# Migrating to 0.0.12

Version 0.0.12 expands account discovery and tightens the local security and
release contracts. Existing stdio installations keep the same server command.

## Upgrade

```bash
pipx upgrade gpt2agent
gpt2agent install
gpt2agent --version
```

Restart MCP hosts that keep a long-running server process. The version command
must report `gpt2agent 0.0.12`.

## Tool and resource changes

The MCP surface now contains exactly 32 tools. The seven additions are read-only:

- `list_scheduled_tasks` reads scheduled ChatGPT automations;
- `list_plugins` and `list_installed_plugins` read the Plugin catalog and the
  account's installed Plugins;
- `list_work_models` reads the separate Work model catalog;
- `sites_access` and `list_sites` read bounded Sites metadata without returning
  content or private URLs;
- `account_capabilities` returns a live, shape-only capability inventory with
  explicit unknown states. The inventory always keeps conversation, memory,
  and custom-instruction capability records unknown; their explicit tools read
  that private content separately when intentionally invoked.

`list_tasks` still exists, but it represents generic asynchronous jobs. It is
not an alias for `list_scheduled_tasks`. Apps/connectors from `list_apps` are
also distinct from Plugins.

Two static resources are available through MCP resource discovery:

- `chatgpt://feature-coverage`
- `chatgpt://update-evidence`

Reading either resource performs no network or account request. Use
`account_capabilities` when current account reachability matters.

`chat` now accepts an optional `thinking_effort`. Leave it unset to preserve the
model default. When supplied, the value must be advertised by the selected
general ChatGPT model. Both `chat` and `agent` validate their selected general
model. A Work-only identifier from `list_work_models` is not valid on either
path unless the same exact slug also appears in `list_models`.

## HTTP is local-only

Streamable HTTP has no account-level authentication and now refuses every
non-loopback bind. The legacy `GPT2AGENT_ALLOW_REMOTE` variable cannot override
that refusal. Remove it from service files and shell profiles.

Plain `gpt2agent run` now starts the default stdio transport. Existing HTTP
service units or launch scripts that relied on plain `gpt2agent run` must add
the explicit flag and port:

```bash
gpt2agent run --http --port 9000
```

`gpt2agent install --client claude-code --transport http --http-port 9000`
registers that loopback URL with Claude Code only; it does not start or manage
the server. HTTP install requests for Codex, other supported hosts, or a mixed
auto-detected target set now fail before writing any configuration.

Use stdio for Claude Code, Codex, Cursor, Windsurf, Claude Desktop, Zed, and
other local MCP clients. If a local browser client needs streamable HTTP, bind
`127.0.0.1`, `localhost`, or a supported loopback address. Native MCP Host and
Origin validation rejects non-loopback DNS-rebinding attempts.

## Raw diagnostics were removed

The legacy `GPT2AGENT_RAW_DUMP` path is no longer available. Setting the
variable fails closed before account access and creates no file. Remove it from
current configuration.

The bundled Deep Research runner now creates only:

- `report.md`, the explicitly requested report;
- `status.txt`, shape-only START/DONE/INCOMPLETE/ERROR state and counts.

Automation that expected `events.jsonl` or `meta.json` must switch to those two
artifacts. Raw prompts, responses, resume tokens, SSE frames, and backend
metadata are never diagnostic deliverables.

## Completion visibility and receipts

`chat`, `agent`, and `gpt_chat` now end every completion with one
server-appended `Tool activity receipt`. It lists only fixed safe categories,
or `none` when no tool lifecycle was observed. Consumers must treat only the
final footer as authoritative; a matching string earlier in model-authored text
is not a receipt. Hidden dispatch and response bodies remain withheld.

Regular response text and heavy Deep Research progress are buffered until the
current message is terminal; late visibility patches can revoke provisional
text before it is published.
Deep Research emits only static `web_search` / `web` tool events internally,
never private dispatch text. An Agent polling timeout returns
`(no final assistant response)` followed by the final activity receipt.

## Image-generation route

Image generation now follows the observed private prepare/conduit flow at
`/backend-api/f/conversation/prepare`, then consumes the `/f/conversation` v1
patch stream. A result is accepted only when its visible carrier is bound to the
current stream by the observed dispatch or a same-message marker and has
image-generation provenance. These are undocumented private ChatGPT routes, not
an official or stable API; drift is a typed failure, not a compatibility promise.
The 2026-07-10 release validation confirmed website image generation but the
direct client failed closed on current required-Turnstile contract drift before
prepare. Model-catalog entitlement therefore remains separate from execution
reachability.

## Concurrency and writes

Ordinary REST/JSON backend requests share a process-wide limit of four in-flight
calls. Set `GPT2AGENT_MAX_IN_FLIGHT` to an integer from 1 through 8 only when a
smaller or larger local bound is justified. Permit acquisition fails within one
second, and a 429 activates a route-local cooldown of at most 60 seconds. Direct
SSE/Sentinel streams are outside this semaphore and use endpoint timeouts; keep
heavy Deep Research serial.

Custom-instruction partial updates are serialized inside one gpt2agent process.
That lock cannot coordinate two independently running processes. Avoid two MCP
servers writing custom instructions at the same time.

## Voice boundary

Version 0.0.12 contains no Voice tool, audio stream, WebRTC session, microphone
capture, or OpenAI API fallback. The static coverage resource lists Voice as
deferred and unprobed rather than reachable; Voice entries are non-collection
capabilities in this release.

The current official [Voice in ChatGPT](https://help.openai.com/en/articles/20001274)
guidance describes Live as a separate human-facing feature and says it does not
initially support connected apps or Plugins, Work, Codex, Custom GPTs, Temporary
Chats, or desktop. A bounded read-only voice catalog is planned for 0.0.13.
AgentRTC/WebRTC and human-to-coding-agent audio are later, separately gated work;
they are not presented as a supported MCP audio capability.

## Release operators

The account gate must run on a trusted local machine from the clean repository
root at the exact candidate commit. Follow the complete build-once operator
procedure in the README: query the successful main `ci.yml` push run with at
least 72 hours of artifact lifetime, download its attempt-specific artifact,
and pass the returned run ID, producing attempt, artifact ID, REST digest, size,
and expiry to both account-receipt commands. Give the downloaded distribution
and receipt new sibling paths outside the checkout.

The command separately checks both exact main-CI distributions, probes from the
installed wheel environment, measures the active account entitlement against
the expected Pro plan, and writes a mode-0600 canonical receipt. Its redacted
shape evidence records empty/nonempty classes without exact account collection
counts. Never upload a browser cookie, bearer token, raw response, or
unsanitized account payload to GitHub Actions. Put the exact eight lines emitted
by the create command in the annotated tag message:

Build/install children receive a positive-allowlist
runtime/locale/certificate environment; credential-bearing proxy variables are
not forwarded. The live probe gets a separate mode-restricted
credential-discovery home containing only the selected ChatGPT access token, so
normal environment and home-directory lookup cannot discover unrelated operator
GitHub, release, publish, SSH, and cloud credentials. Child `TEMP`/`TMP`/`TMPDIR`
paths are also private per-step directories rather than inherited operator
paths. This is not an OS filesystem sandbox. The reviewed installed candidate
still receives that ChatGPT bearer for its current live adapter; this is not an
account-auth isolation boundary.

```text
account-receipt-sha256: <64 lowercase hex>
account-artifact-set-sha256: <64 lowercase hex>
account-ci-run-id: <positive integer>
account-ci-run-attempt: <positive integer>
account-ci-artifact-id: <positive integer>
account-ci-artifact-digest: sha256:<64 lowercase hex>
account-ci-artifact-size: <positive integer>
account-ci-artifact-expires-at: <UTC timestamp>
```

The hosted release workflow validates the pinned run and candidate live with a
small execution headroom, downloads by immutable artifact ID, reconstructs the
account artifact-set digest, and never rebuilds. It publishes those same bytes
through PyPI trusted publishing, verifies published hashes, installs the exact
PyPI version in a clean canary environment, and creates the GitHub Release only
after the canary passes. The release evidence asset carries the full pinned
candidate identity and artifact-set digest.

Before tagging, an expired, deleted, replaced, or near-expiry candidate requires
a full main-CI rerun and a new account gate. Immediately before tag creation,
the README operator command re-fetches the complete pinned run artifact list,
requires at least one hour of retention headroom, and rejects any newer
candidate-producing attempt. After tagging, artifact loss is a hard release
blocker; do not rebuild or move the version tag. Historical receipt verification
remains valid after normal Actions retention expiry when the exact distribution
bytes and recorded metadata have been retained.

The digest is only a commitment to the local receipt bytes and a post-publish
audit link. Hosted automation cannot validate a receipt it does not receive
until after publication. Before creating the tag, restrict creation to the
policy-bound release App and enforce deletion/update/non-fast-forward through
separate no-bypass rules. Require the policy-bound independent reviewer or
protection App on the protected `pypi` environment, with self-review and
administrator bypass disabled. The reviewer must verify the exact tag, commit,
tree, version, and receipt digest. Without those live controls, do not tag or
publish.

Verify the controls through reviewed GET-only endpoints before tagging:

```bash
python scripts/audit_release_governance.py \
  --live robotlearning123/gpt2agent \
  --policy /trusted/local/release-governance-policy.json
```

The policy is the separately reviewed closed-schema identity binding documented
in `CONTRIBUTING.md`; do not infer its App IDs or reviewer identity from the
snapshot being audited. The command emits deterministic JSON and exits nonzero
if the policy is missing or invalid, any required control is absent, or the
live snapshot cannot be validated.

After the workflow is green, verify the retained mode-0600 local receipt's
SHA-256 against the annotated-tag line and keep it only in the approved local
evidence store. Do not upload the account receipt to Actions or the public
GitHub Release. The public tag and release-evidence asset carry only its digest
and exact artifact handoff; they do not expose the shape-only account probe
records.
