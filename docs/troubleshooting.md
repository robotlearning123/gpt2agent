# Troubleshooting

### "No ChatGPT token found"

gpt2agent reads, in order, `$CODEX_HOME/auth.json` (or `~/.codex/auth.json`) then
`~/.gpt2agent/token.json`. Fix:

```bash
codex login           # preferred — auto-refreshing token
# or
gpt2agent setup       # paste a ChatGPT token once
```

### MCP tools don't appear in my client

The client only spawns the server on (re)start. **Restart Claude Code / Cursor /
Windsurf / Zed** after `gpt2agent install`. Codex picks it up on its next run.
Confirm the binary works first: `gpt2agent run --stdio` (should start and wait).

### `401 Unauthorized — run codex login`

The bearer token expired. Run `codex login` again (gpt2agent reloads it on the next
call). If you used `gpt2agent setup`, re-run it.

### `403 Forbidden`

A 403 from `/backend-api/conversation` is **not always** an expired token. It can be:
- a Cloudflare / Sentinel bot-check (transient — retry), or
- a region/feature gate on your account.
Try once more; if it persists, re-`codex login`, and check the feature is available
in the ChatGPT web app for your plan.

### `429` / rate limited

You're being throttled. For ordinary REST/JSON requests, gpt2agent applies a
cooldown only to the normalized route that returned 429, honors valid
`Retry-After` up to 60 seconds, and fails fast while that route is cooling down.
Wait for the reported interval; keep request volume human-scale. Deep Research
also has a monthly quota (see [faq.md](./faq.md)).

Ordinary REST/JSON account calls also share a process-wide in-flight limit
(default 4). If the client reports a temporary failure after a one-second permit
wait, reduce its parallelism or set `GPT2AGENT_MAX_IN_FLIGHT` to a justified
integer from 1 to 8. Direct SSE/Sentinel streams are outside this semaphore and
use endpoint timeouts; run heavy Deep Research serially. The limit does not
coordinate multiple gpt2agent processes.

### `contract_changed` / `access_indeterminate` / `unverified`

These are typed, content-free adapter outcomes, not raw backend errors.
`contract_changed` means a private response no longer matches the minimum known
schema. `access_indeterminate` means the route did not prove entitlement either
way. `unverified` means the release intentionally made no live claim, including
Voice in 0.0.12. Update gpt2agent and inspect the public radar or sanitized local
account receipt before assuming the feature is absent.

### `thinking_effort ... is not valid` / unsupported model

Call `list_models` and use one of the selected general model's advertised
`thinking_efforts`. gpt2agent refreshes the 60-second model cache once after a
rejected effort. A Work-only slug from `list_work_models` remains invalid for
`chat` or the configured `agent` model unless the exact slug also appears in the
general catalog. Leave `thinking_effort` unset to use the model default.

### HTTP transport is disabled

This is intentional in 0.0.12. Loopback TCP cannot isolate the account from
other users and processes on the same host. Use `gpt2agent run --stdio` or rerun
`gpt2agent install` to restore the spawned stdio configuration.

### image gen / code interpreter / canvas returns plain text

`chat` defaults to `temporary=True`, which disables tool features. Use the dedicated
tools — `generate_image`, `code_interpreter`, `canvas_execute` — which set
`temporary=False` for you. (Calling `chat` and asking it to make an image won't work.)

`generate_image` uses an observed, undocumented private prepare/conduit + `/f`
v1 flow. A `contract_changed` result means that flow or its relational result
provenance no longer matches the known shape; it is not a stable public API.
As of the 2026-07-10 release validation, the authenticated website path worked
but the direct client failed before prepare because the required Turnstile
challenge no longer matched the vendored solver. Do not export a browser token
or infer direct execution reachability from the model catalog.

### `deep_research_heavy` has no grouped Sources list

The report is recovered from the connector's widget state; grouped source URLs are
usually present but not guaranteed. If absent, the model often cited sources inline
in the body. Use light `deep_research` for reliably grouped citations.

The bundled runner writes only `report.md` and shape-only `status.txt`. Raw SSE
events and server metadata are intentionally unavailable as diagnostic files.

### Installer aborts / `externally-managed-environment`

On PEP-668 systems (Ubuntu/Debian/Fedora) `pip install --user pipx` is blocked.
Install pipx via your distro and re-run the one-liner:

```bash
sudo apt install pipx && pipx ensurepath     # Debian/Ubuntu
sudo dnf install pipx && pipx ensurepath     # Fedora
brew install pipx && pipx ensurepath         # macOS
```

### PyPI install fails (package not found)

The default installer fails closed if the published package cannot be installed;
it never substitutes mutable repository code. Check network/PyPI status and retry
the one-line installer. For development from a checkout you deliberately trust,
use `./install.sh --source /path/to/gpt2agent`.

The installer recreates an existing `gpt2agent` pipx environment when upgrading
so the requested source and a compatible Python are both honored. Re-add any
packages you had deliberately injected into that environment afterward.
