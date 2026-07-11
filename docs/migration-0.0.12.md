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

## HTTP is disabled

Version 0.0.12 supports stdio only. Loopback TCP is visible to other users and
processes on the same host, so a loopback bind plus Host/Origin validation is
not account authentication. The legacy HTTP launch flag and URL installer now
fail before server construction or configuration writes. Remove HTTP service
units and `GPT2AGENT_ALLOW_REMOTE` from current launch configuration, then run
`gpt2agent install` to restore a spawned stdio entry.

Use stdio for Claude Code, Codex, Cursor, Windsurf, Claude Desktop, Zed, and
other local MCP clients. A future network transport requires strong per-launch
authentication or an equivalent per-user boundary.

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
procedure in the README. First copy CPython 3.12.13 for Linux x86_64 from a
the exact Astral 20260510 install-only archive pinned in the README. Set
`GPT2AGENT_TRUSTED_PYTHON_ARCHIVE` to a protected local copy; do not provide
runtime hashes from the operator environment. The reviewed extractor pins the
archive size and SHA-256, topology, symlink map, executable SHA-256, and complete
normalized tree SHA-256, then extracts into an owner-private disposable
directory. Every runtime ancestor to `/` must be owned by root or the operator
and must not be group/world-writable or a symlink. Hosted CI retains its distinct
pinned `actions/setup-python` and isolated-runner provenance boundary. The
reviewed hash lock allows only the exact nine-distribution account-gate closure,
binary wheels, and the official PyPI index; the verifier then checks every installed
distribution, file, owner/mode, import origin, and runtime path under
`python -I -S -B`.

Query the successful main `ci.yml` push run with at least 72 hours of artifact
lifetime, download its attempt-specific artifact, and pass the returned run ID,
producing attempt, numeric artifact ID, REST digest, size, and expiry to both
account-receipt commands. Give the downloaded distribution, receipt, and trusted
runtime new paths outside the checkout. The runtime is temporary; retain the
mode-0600 receipt and inert candidate only under the reviewed evidence policy.

Main CI's credential-free package job installs both exact distributions and
runs the same closed synthetic adapter corpus against each, requiring identical
canonical results and unchanged distribution hashes. The local command treats
those wheel/sdist files as inert bytes. Its own reviewed `curl_cffi` transport
loads one owner-only bearer, measures the active account entitlement against the
expected Pro plan, performs only the exact bounded GET allowlist, and writes a
mode-0600 canonical receipt. Redacted live evidence records empty/nonempty
classes without exact account collection counts; fixed adapter counts represent
the separate main-CI corpus. Never upload a browser cookie, bearer token, raw
response, unsanitized account payload, or the receipt itself to GitHub Actions.
Do not copy verifier stdout into a tag. Compute the receipt SHA-256 independently
and pass it with the exact candidate identity to
`scripts/create_release_tag.sh`. The coordinator prompts for the short-lived,
repository-scoped release App token only after receipt creation; keeps it out of
Python, environment inheritance, and process arguments; re-fetches the pinned CI
candidate; re-verifies the receipt; and creates a new tag through the GitHub API
without any update, deletion, `git tag`, or `git push` fallback.

The trusted transport disables environment proxy discovery and automatic
redirects, uses fixed timeouts and TLS verification, and caps response bodies at
4 MiB. It never imports or invokes the candidate package. Run package smoke only
in credential-free CI or an OS-isolated no-auth container/VM; private `HOME` and
an empty environment are useful hygiene but are not an OS sandbox. Create and
verify the receipt immediately: either command rejects evidence older than
30 minutes, a live probe lasting over 10 minutes, or a completion timestamp more
than one minute in the future.

The annotated tag has one exact canonical envelope: `gpt2agent <version>`, one
blank line, and one ASCII, duplicate-key-free, closed-schema JSON object. Its
repository, tag, version, source commit/tree, receipt and artifact-set digests,
and complete candidate identity are generated and validated by
`release_tag_metadata.py`; extra text, extra keys, alternate spellings, and
noncanonical JSON fail closed. Keep the release branch and the exact full-SHA
publication-action commit pushed and resolvable through the first release so
hosted Actions can fetch that immutable action revision.

The hosted release workflow validates the pinned run and candidate live with a
small execution headroom, downloads by immutable artifact ID, reconstructs the
account artifact-set digest, and never rebuilds. Before publication it verifies
and promotes those inert bytes without importing, installing, or rerunning
package smoke; required main CI is the sole pre-publication packaged-artifact
execution gate. After PyPI publication, a credential-free canary installs the
public version and checks its CLIs, resources, and import surface. The release
evidence asset carries the full pinned candidate identity and artifact-set
digest. GitHub publication resolves or creates the exact-tag draft, captures its
numeric release ID, and targets every asset operation and publication update by
that ID. Exact metadata and assets are checked before and after publication, the
public readback must be immutable, and an already-public rerun succeeds only for
an exact immutable match.

Before tagging, an expired, deleted, replaced, or near-expiry candidate requires
a full main-CI rerun and a new account gate. Immediately before tag creation,
the README operator command re-fetches the complete pinned run artifact list,
requires at least one hour of retention headroom, and rejects any newer
candidate-producing attempt. After tagging, artifact loss is a hard release
blocker; do not rebuild or move the version tag. The full receipt verifier is a
fresh pre-tag gate, not a replay mechanism. Long-term audit verifies retained
receipt bytes against the immutable annotated-tag SHA-256 commitment.

The digest is only a commitment to the local receipt bytes and a post-publish
audit link. Hosted automation cannot validate a receipt it does not receive
until after publication. Before creating the tag, restrict creation to the
policy-bound release App and enforce deletion/update/non-fast-forward through
separate no-bypass rules. Require the policy-bound independent reviewer or
protection App on the protected `pypi` environment, with self-review and
administrator bypass disabled. The reviewer must verify the exact tag, commit,
tree, version, and receipt digest. Configure a separate policy-bound GitHub App
for immutable-release settings with exactly Administration read, implicit
Metadata read, no subscribed events, and access only to this repository. Bind
its client ID and private key exclusively to a nonblocking
`release-settings-read` environment through
`GPT2AGENT_RELEASE_SETTINGS_APP_CLIENT_ID` and
`GPT2AGENT_RELEASE_SETTINGS_APP_PRIVATE_KEY`. That environment must accept only
`v*` tags, have no reviewer, wait timer, or custom protection rule, and disable
administrator bypass. Do not reuse the reader App as the tag creator,
required-check App, or PyPI gate. Without those live controls, do not tag or
publish.

Verify the controls through reviewed GET-only endpoints before tagging:

```bash
python scripts/audit_release_governance.py \
  --live robotlearning123/gpt2agent \
  --policy /trusted/local/release-governance-policy.json \
  --gh /usr/bin/gh
```

The policy is the separately reviewed schema-v2 identity binding documented in
`CONTRIBUTING.md`; it includes distinct release-tag, release-settings, and
required-check App identities plus the PyPI gate identity. Do not infer those
IDs, the settings App client ID, or the reviewer identity from the snapshot
being audited. The command emits deterministic JSON and exits nonzero if the
policy or exact trusted `gh` path is missing or invalid, any required control is
absent, or the live snapshot cannot be validated.

After the workflow is green, run `scripts/audit_retained_receipt.sh` with the
repository, tag, protected evidence directory, pinned runtime archive, and
`/usr/bin/git`. It fetches the public annotated tag into a disposable bare
repository, executes only the tagged verifiers under the authenticated runtime,
and compares the tag's canonical `receipt_sha256` with the retained mode-0600
receipt. Keep that receipt only in the approved local evidence store. Do not
upload it to Actions or the public GitHub Release. The public tag and
release-evidence asset carry only its digest and exact artifact handoff; they do
not expose the shape-only account probe records.
