# gpt2agent GPT-Live → coding-agent bridge (experimental, v0.0.14)

**Bridge a human's ChatGPT voice to your coding agent. Direction: human → agent.**

GPT-Live is full-duplex audio over WebRTC — it **cannot** be a plain MCP tool (MCP
is request/response and carries no media). This bridge taps the *human* side of a
live voice conversation and routes it to a coding agent; the reply reaches the human
**out-of-band** (a text overlay), because GPT-Live silently drops any client-injected
speech (verified — see the protocol docs). So there is **no agent→Live "speak" path**.

- **Browser (real Chrome)** owns WebRTC, mic, speaker, and the datachannel. Audio
  never leaves it.
- **Bridge layer + agent** receive only the **human transcript text** and return
  **reply text**. No raw audio, SDP, bearer tokens, or cookies cross that boundary.

```
  human mic ──WebRTC──▶ signed-in Chrome (ChatGPT Voice UI)
                             │ datachannel: chat_message_delta (human transcript)
                             ▼
                    bridge layer (src/export.mjs · extension/hook.js)
                       onAgentTurn(humanText)
                             │
                    your coding agent reasons + uses repo/tools
                             │
  human eyes ◀── text overlay ── agent reply   (NOT spoken by Live)
```

See the spec: `docs/superpowers/plans/2026-07-11-gpt-live-bridge-layer-spec.md`.

## Hard boundary (read this)

| Supported | Not supported |
|---|---|
| Human-authenticated **real signed-in Chrome** | Headless / token-only SDP "bypass" of Cloudflare Turnstile |
| Reading the human transcript (observe) | Making Live **speak** injected text (server drops it) |
| Localhost control plane + MCP tools (text only) | Shipping raw audio over MCP |

Turnstile / bot-detection circumvention is **out of scope**.

## Reliable path — extension + agent gateway (real human voice)

The reliable bridge uses your own signed-in Chrome (which clears Turnstile natively)
plus a small extension that reads the real transcript and an agent gateway that runs
your coding agent.

```bash
cd sidecar && npm install

# 1) run the agent gateway (the coding agent adapter + control plane). Loopback only.
AGENT_CMD='claude -p' node agent-gateway.mjs
#   or: AGENT_CMD='codex exec --skip-git-repo-check' node agent-gateway.mjs

# 2) load sidecar/extension as an unpacked extension in your signed-in Chrome
#    (chrome://extensions → Developer mode → Load unpacked → pick sidecar/extension)

# 3) open chatgpt.com, start voice, and talk.
#    Each human utterance → your coding agent; the reply appears as a text overlay.
```

The gateway runs each utterance through the same bridge as the harness (so
`isActionable` filtering + the transcript buffer apply) and serves the control plane
on `127.0.0.1:8741`, so the `voice_live_*` MCP tools observe **this** path.

**Gateway security.** Baseline protection is loopback-bind + no wildcard CORS (a
drive-by web page cannot reach it). `GPTLIVE_TOKEN` adds a header gate for **direct,
non-extension** local callers — the bundled extension does **not** send a token, so
leave it unset if you rely on the extension.

## Test harness — browser/sidecar.mjs (no human, wiring only)

`browser/sidecar.mjs` launches puppeteer Chrome with a **fake WAV mic** to exercise
the bridge without a person. Two limits, both by design:

- Synthetic audio is **not transcribed** by GPT-Live (only a real mic is), so this
  proves wiring, not live transcription.
- Puppeteer automation may be flagged by the session's anti-bot check.

```bash
node browser/sidecar.mjs --profile "$PWD/.chrome-gptlive" --audio q.wav --control-port 8741
node browser/sidecar.mjs --profile "$PWD/.chrome-gptlive" --audio q.wav --reply "Four."   # fixed-reply wiring demo
```

CLI help: `node browser/sidecar.mjs --help`

### Agent hook options

| Mechanism | How |
|---|---|
| Agent gateway | Extension POSTs each utterance to `agent-gateway.mjs` (`AGENT_CMD`) |
| `--reply TEXT` | Fixed reply for every human turn (harness demo) |
| `--agent-cmd 'cmd'` | Shell: stdin = human text, stdout = reply text |
| In-process | `ModeBExport({ onAgentTurn })` in `src/export.mjs` |

### Localhost control plane (text/control only, observe + lifecycle)

Default: `http://127.0.0.1:8741` (override with `--control-port` / `GPT2AGENT_LIVE_CONTROL`).

| Route | Purpose |
|---|---|
| `GET /help` | How the bridge works + Turnstile boundary |
| `GET /status` | Bridge state (redacted; no secrets/audio) |
| `GET /transcript` | Observed human/agent text (`?clear=1` to drain) |
| `POST /end` | Tear down |
| `GET /health` | Liveness |

There is intentionally **no `/send_text` route** — the agent cannot make Live speak.

### MCP tools (Python package, control only)

| Tool | Purpose |
|---|---|
| `voice_live_export_help` | Docs + boundary |
| `voice_live_status` | Proxy `GET /status` |
| `voice_live_get_transcript` | Proxy `GET /transcript` |
| `voice_live_end` | Proxy `POST /end` |

Audio never enters MCP responses. Note: the Node sidecar/extension ship with the
**source repo**, not the PyPI wheel — clone the repo to run them.

## Shipped modules

| Module | Role |
|---|---|
| `src/transcript.mjs` | **Consumer protocol parser** — `chat_message_delta` → human utterances |
| `src/export.mjs` | **Bridge layer** — ingest → filter → agent hook → transcript buffer |
| `src/control.mjs` | Localhost HTTP control plane (observe + lifecycle) |
| `extension/` | Chrome extension — real-Chrome TAP + agent-reply overlay |
| `agent-gateway.mjs` | Agent adapter — POST `{text}` → runs `AGENT_CMD` → `{reply}` |
| `src/session.mjs` | Experimental werift session orchestration (observe path) |
| `src/adapter.mjs` | Verified realtime routes + SDP exchange |
| `src/reconnect.mjs` / `liveness.mjs` | Reliability primitives |
| `browser/sidecar.mjs` | Diagnostic/test harness (fake mic) |

```bash
cd sidecar && npm test
```

## Investigation notes

Handshake evidence, the full protocol, and the Turnstile boundary write-ups live
under `docs/superpowers/plans/2026-07-11-*.md`. Catalog-only voice discovery remains
`list_voices` (0.0.13 lane) and is separate from this bridge.
