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
        │                                         /gizmos, /files, /apps,
        │                                         /settings/voices, ...
   26 MCP tools
```

## Request path

- **SSE tools** (`chat`, `agent`, `deep_research[_heavy]`, `gpt_chat`, image gen,
  code interpreter, canvas) stream from `/backend-api/conversation` and are parsed
  incrementally in `gpt2agent/sse.py`.
- **REST tools** (account, models, Voice catalog, memory, instructions,
  conversations, codex, apps, files) are thin wrappers over
  `gpt2agent/backend.py`'s sync HTTP client, exposed from
  `gpt2agent/tools/*.py`.

## The Sentinel challenge

ChatGPT's backend is protected by OpenAI's "Sentinel" anti-bot system (a
proof-of-work plus a Cloudflare Turnstile token). gpt2agent solves these with
vendored solvers (`gpt2agent/_vendored/`, MIT, attributed in
[NOTICES.md](../NOTICES.md)) and aligns its TLS fingerprint + User-Agent with a real
Chrome build so Cloudflare's bot manager doesn't 403 the request. **This is the part
that carries ToS/account-ban risk** — see the README's Security & risk section.

## Token handling

The bearer is read from disk on each request and reloaded when the file's mtime
changes, so codex's background refresh propagates without restarting the server.
Multi-account setups can point at a different login via `CODEX_HOME`. Token and
header values are redacted from error messages and logs (`gpt2agent/_log_redact.py`).

## Transports

stdio (default; a local subprocess of your client) or streamable-HTTP (loopback by
default; unauthenticated — see Security & risk). `gpt2agent install` always wires
stdio.
