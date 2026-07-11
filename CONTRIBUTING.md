# Contributing to gpt2agent

## Prerequisites

- Python 3.10+
- pipx
- codex CLI (for live testing against real backend)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Tests

```bash
pytest
```

The offline suite must pass before merging (live tests are gated by `SKIP_LIVE`
and skipped by default). Also run `ruff check gpt2agent tests scripts` and
`python scripts/verify_release.py`.

CI also installs the package at the minimum supported MCP SDK (`1.26.0`) and the
latest resolvable stable v1, builds the wheel and sdist, and runs both artifacts
in clean environments. Hosted CI never receives ChatGPT account credentials.

## Code style

- Follow existing patterns in the codebase.
- No comments unless the WHY is non-obvious.
- No docstrings longer than one short line.
- Minimal changes — don't refactor surrounding code.

## PR process

- One concern per PR.
- Conventional commits: `feat(scope):`, `fix(scope):`, `test:`, `docs:`, `chore:`.
- Tests required for any new behavior.
- Squash-merge into main.

## Adding a new MCP tool

1. Create `gpt2agent/tools/<name>.py` with a `register(mcp, client: BackendClient)` function.
2. For tools that need SSE streaming (chat, image gen, etc.), accept an optional `conv` param: `register(mcp, client, conv=None)`.
3. Add all four MCP hints to `TOOL_ANNOTATION_MANIFEST` and use
   `@mcp.tool(annotations=tool_annotations("name"))`. Annotations guide clients;
   they do not replace server-side validation.
4. Add the import to `gpt2agent/tools/__init__.py` and call `register()` in `register_all()`.
5. Normalize private responses to a bounded allowlist and use typed contract
   failures. Never return a raw backend body, URL, header, or account payload.
6. Add exact manifest, annotation, adapter, and package tests in `tests/`.

Static MCP resources belong in `gpt2agent/resources/*.json` and register through
`gpt2agent/resources.py`. A resource read must be deterministic and perform no
account or network request.

## Architecture

- `BackendClient` (backend.py): synchronous HTTP via `curl_cffi`. Handles
  request-local token snapshots, Sentinel challenges, bounded REST responses,
  process-wide ordinary REST/JSON concurrency, and route-local cooldowns.
- `ConversationClient` (sse.py): async SSE streaming. Handles `/backend-api/conversation` and `/backend-api/f/conversation` for chat, DR, agent mode, image gen, code interpreter, canvas.
- `server.py`: FastMCP registration and stdio-only transport policy. Creates `BackendClient` + `ConversationClient` singletons.
- `tools/`: 15 registration modules exposing 26 of the 32 MCP tools. REST-backed handlers
  are async and offload the synchronous `BackendClient` through the shared
  tool backend helper.
- `tool_contracts.py` / `tool_manifest.py`: the exact 32-tool public contract and annotations.
- `resources.py` + `resources/*.json`: two static account-independent MCP resources.
- `model_catalog.py`: 60-second General/Work caches keyed by auth generation;
  the namespaces never merge.

The `temporary` parameter on `_build_payload()` controls `history_and_training_disabled`. Tools that use ChatGPT features (image gen, code interpreter, canvas, memory writes, agent mode) MUST pass `temporary=False`.

## Token

Loaded from `$CODEX_HOME/auth.json` (or `~/.codex/auth.json`, preferred and
auto-refreshed by Codex) or `~/.gpt2agent/token.json` (manual). Never commit
tokens or credentials.

## Private release gate

Account-backed release checks run only on a trusted local machine against the
exact candidate commit. Never place browser cookies, bearer tokens, or raw
account payloads in Actions, PRs, logs, or repository artifacts. Hosted CI sees
only the sanitized receipt's SHA-256; the receipt remains in the approved local
evidence store and is never attached to the public release. The trusted verifier
owns the bearer and live transport while candidate distributions remain inert.
The closed adapter corpus executes candidates only in credential-free main CI
or an OS-isolated environment with no account auth. See
[the 0.0.12 migration guide](docs/migration-0.0.12.md#release-operators).
Before creating a release tag, run:

```bash
: "${VERIFIER_PYTHON:?set this to the reviewed isolated CPython 3.12.13 verifier}"
GH_TOKEN="$(/usr/bin/gh auth token --hostname github.com)" \
  "$VERIFIER_PYTHON" -I -S -B \
  scripts/audit_release_governance.py \
  --live OWNER/REPO \
  --policy /trusted/local/release-governance-policy.json \
  --gh /usr/bin/gh
```

Any failed check blocks tagging and publication. `POLICY.json` is a separately
reviewed, closed-schema identity binding, not data inferred from the live
snapshot:

```json
{
  "schema_version": 2,
  "repository": "OWNER/REPO",
  "release_tag_app": {"id": 123456},
  "release_settings_app": {
    "id": 234567,
    "slug": "release-settings-reader",
    "client_id": "Iv1.1234567890abcdef"
  },
  "required_check_app": {"id": 15368},
  "pypi_gate": {"kind": "reviewer", "login": "independent-reviewer"}
}
```

The alternative PyPI gate is
`{"kind":"protection_app","id":654321,"slug":"reviewed-app"}`. A reviewer
policy passes only when the environment's required-reviewer list contains that
one independent user; adding the repository owner would create an OR path and
fails the audit. A protection App must be distinct from the release-tag,
release-settings, and required-check Apps.
Tag creation uses one or more exact `v*` rulesets; every such creation ruleset's
sole bypass must be the policy-bound release App. Deletion, update, and
non-fast-forward use separate exact-scope rulesets with no bypass actors.
`Required checks` must be bound to the App ID in the policy. Live mode fails
before making GitHub requests when `--policy` is absent or invalid, when `--gh`
is not an absolute protected executable, or when the policy path or any ancestor
is writable by another user. The policy-bound release-settings App must be
installed only on this repository with exactly Administration read, implicit
Metadata read, and no subscribed events. The nonblocking
`release-settings-read` environment must allow only `v*` tags, disable
administrator bypass, expose exactly one client-ID variable named
`GPT2AGENT_RELEASE_SETTINGS_APP_CLIENT_ID`, and expose exactly one private-key
secret named `GPT2AGENT_RELEASE_SETTINGS_APP_PRIVATE_KEY`.

Available GitHub GET endpoints validate the current settings-reader
installation, but cannot prove that the App has no other installation under a
different owner or that the private key is unused elsewhere. Preserve separately
reviewed App-owner evidence for those exclusivity claims. Likewise, GitHub and
PyPI offer no cross-registry atomic transaction. The documented GitHub REST
release update exposes no conditional precondition/CAS contract. The workflow
prepares a complete GitHub draft before PyPI and revalidates it after the PyPI
canary, but a privileged writer can still race the last validation and
publication PATCH. Public readback detects asset/tag mismatches observable
during its bounded checks, but cannot roll back the now-frozen asset/tag state
or PyPI bytes safely or automatically. Title and release notes remain editable
after publication; their exactness is point-in-time at readback and therefore
also depends on privileged-writer governance.
