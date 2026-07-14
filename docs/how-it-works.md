# How it works

gpt2agent is a native Python MCP server that calls ChatGPT's backend directly —
no proxy process, no platform API key.

```
$CODEX_HOME/auth.json (default ~/.codex/auth.json) ← bearer, auto-refreshed by Codex
~/.gpt2agent/token.json                            ← manual fallback
        │
   gpt2agent  (stdio MCP server; token reloaded from disk on mtime change)
        │
   curl_cffi  ──TLS-impersonates Chrome──▶  chatgpt.com /backend-api/...
        │                                     ├── /conversation, /f/conversation   (SSE)
        │                                     └── /me, /models, /memories, /codex,  (REST)
        │                                         /gizmos, /files, /apps, ...
   all registered ChatGPT tools + 2 packaged MCP resources
```

The Grok Build path is separate:

```text
configured repository root
        │
   gpt2agent (bounded stdio child)
        │
   official Grok Build CLI (subscription/OAuth; CLI-owned session history)
```

## Request path

- **SSE tools** (`chat`, `agent`, `deep_research[_heavy]`, `gpt_chat`, image gen,
  code interpreter, canvas) are parsed in `gpt2agent/sse.py`. Image generation
  uses the observed private `/backend-api/f/conversation/prepare` conduit step
  followed by the `/backend-api/f/conversation` v1 patch stream; other tools use
  the applicable conversation route.
- **REST tools** (account, General/Work models, memory, instructions,
  conversations, generic jobs, scheduled automations, Codex, Apps, Plugins,
  Sites, files, and capability truth) are bounded adapters over
  `gpt2agent/backend.py`'s sync HTTP client, exposed from
  `gpt2agent/tools/*.py`. Raw private responses are not an MCP contract.
- **Static resources** (`chatgpt://feature-coverage` and
  `chatgpt://update-evidence`) read packaged JSON. Resource reads perform no
  network or account request.

These account endpoints and event shapes are reverse-engineered observations,
not official or stable integration contracts. In particular, image assets are
accepted only when a visible result carrier is bound to the current stream by
the observed dispatch or a same-message marker and carries image-generation
provenance. Contract drift fails closed instead of turning an unrelated
image-shaped message into a result.

## Visibility and completion output

Regular chat response text and heavy Deep Research progress are buffered until
the relevant backend message reaches a terminal, still-visible state. This lets
a late visibility patch revoke provisional text before it becomes public output.
Deep Research tool events use only the static `web_search` / `web` category;
private dispatch text is not an event payload.

Every `chat`, `agent`, and `gpt_chat` completion ends with a server-appended
`Tool activity receipt`, including `none`. Only the final footer is
authoritative. If Agent polling times out without a completed assistant message,
the body is `(no final assistant response)` followed by that receipt.

## The Sentinel challenge

ChatGPT's backend is protected by OpenAI's "Sentinel" anti-bot system (a
proof-of-work plus a Cloudflare Turnstile token). gpt2agent solves these with
vendored solvers (`gpt2agent/_vendored/`, MIT, attributed in
[NOTICES.md](../NOTICES.md)) and aligns its TLS fingerprint + User-Agent with a real
Chrome build so Cloudflare's bot manager doesn't 403 the request. **This is the part
that carries ToS/account-ban risk** — see the README's Security & risk section.

## Token handling

The bearer is reloaded when the auth file changes, so codex's background refresh
propagates without restarting the server. Authorization lives only in
request-local headers, never in the shared HTTP session. A multi-request
operation takes one auth snapshot so token rotation cannot mix generations
inside that operation. Multi-account setups can point at a different login via
`CODEX_HOME`. Token and header values are redacted from error messages and logs
(`gpt2agent/_log_redact.py`).

General and Work model metadata use separate 60-second caches keyed by that auth
generation. `chat` and `agent` validate their selected general model; `chat`
also validates any optional `thinking_effort`. Work-only identifiers never leak
into general chat validation.

Grok Build never receives either ChatGPT auth source. gpt2agent also strips
`XAI_API_KEY` and `GROK_CODE_XAI_API_KEY` from the CLI environment, so the Build
path uses the official CLI's separate subscription/OAuth state. Explicit
`GROK_HOME` and `GROK_AUTH_PATH` locations can be configured. No ChatGPT, Build,
or website authentication lane falls back to another.

Build command execution is constrained to configured roots and bounded by time
and output size. Empty roots disable both probes and agent sessions. Plan mode
uses the CLI's plan/read-only sandbox; apply is an explicit destructive choice
using the strict sandbox. The CLI retains history; gpt2agent exposes only a
sanitized session ID, not a transcript or resume/delete operation. See xAI's
[CLI reference](https://docs.x.ai/build/cli/reference) and [headless scripting
guide](https://docs.x.ai/build/cli/headless-scripting).

## Bounded request policy

Ordinary `BackendClient` REST/JSON requests share a process-wide semaphore, four
permits by default and a configured range of 1-8. A caller waits at most one
second for a permit. Backend 429 responses activate a normalized, route-local
cooldown; numeric and HTTP-date `Retry-After` values are capped at 60 seconds.
Response JSON is limited to 4 MiB, and failures expose only typed, bounded
metadata. Direct SSE/Sentinel streams do not hold this semaphore; endpoint
timeouts bound them, and heavy Deep Research should run serially.

## Capability truth

`account_capabilities` probes only an explicit GET allowlist with one auth
snapshot and a 90-second total budget. It returns shape-only records: entitlement
and current reachability are independent tri-state fields, while status and item
contract state distinguish live proof, public-bundle-only schemas, unverified
state, and contract drift. Voice/realtime, transcripts, conversation bodies,
writes, unknown routes, and off-host redirects are not probed. Conversation
summaries, memories, and custom instructions are also omitted from automatic
capability and release-receipt probes because those responses contain private
account content; their explicit tools still work when intentionally called.

## Transports

stdio only: the MCP host spawns a local child process. Version 0.0.12 disables
network transport because loopback TCP is not a per-user account boundary.
`gpt2agent install` always wires stdio.
