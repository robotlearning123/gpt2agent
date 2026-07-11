# gpt2agent GPT-Live voice sidecar (experimental, v0.0.14)

**Export GPT-Live to an agent as Mode B voice I/O.**

GPT-Live is full-duplex audio over WebRTC — it **cannot** be a plain MCP tool
(MCP is request/response and carries no media). The export splits ownership:

- **Browser sidecar (this package)** owns WebRTC, mic, speaker, and datachannel.
  Audio never leaves it.
- **Agent / MCP** only receives **transcript text** and sends **reply text**
  (`send_text`). No raw audio, SDP, bearer tokens, or cookies cross that boundary.

```
  human mic ──WebRTC──▶ headed Chrome (ChatGPT Voice UI)
                             │ datachannel text
                             ▼
                    ModeBExport (src/export.mjs)
                       onAgentTurn(humanText)
                             │
                    your agent reasons + tools
                             │
  human speaker ◀──WebRTC── speak wire (response.create in data_message)
```

## Hard boundary (read this)

| Supported export path | Not supported |
|---|---|
| Human-authenticated **real headed Chrome** profile signed into ChatGPT | Headless / token-only SDP “bypass” of Cloudflare Turnstile |
| Fake WAV mic via Chrome flags, or a real mic | Shipping raw audio over MCP |
| Localhost control plane + MCP control tools (text only) | Mode A (Live natively calling external MCP tools) |

Turnstile / bot-detection circumvention is **out of scope**. Token-only sessions
abort ~1s after `listening` by design (server-side validation).

## Quick start — export path

```bash
cd sidecar && npm install

# 1) one-time: dedicated Chrome profile, sign into chatgpt.com, quit
open -na "Google Chrome" --args --user-data-dir="$PWD/.chrome-gptlive"

# 2) short mono WAV as fake mic
echo "What is two plus two?" | mb voice -o q.mp3
ffmpeg -y -i q.mp3 -ar 24000 -ac 1 q.wav

# 3) run export (control plane on :8741)
node browser/sidecar.mjs \
  --profile "$PWD/.chrome-gptlive" \
  --audio q.wav \
  --control-port 8741

# optional demo: fixed agent reply spoken back through Live
node browser/sidecar.mjs --profile "$PWD/.chrome-gptlive" --audio q.wav --reply "Four."
```

CLI help: `node browser/sidecar.mjs --help`

### Agent hook options

| Mechanism | How |
|---|---|
| Control HTTP | Agent `POST /send_text {"text":"..."}` after reading `GET /transcript` |
| `--reply TEXT` | Fixed reply for every human turn (demo) |
| `--agent-cmd 'cmd'` | Shell: stdin = human text, stdout = reply text |
| In-process | `ModeBExport({ onAgentTurn })` in `src/export.mjs` |

### Localhost control plane (text/control only)

Default: `http://127.0.0.1:8741` (override with `--control-port` / `GPT2AGENT_LIVE_CONTROL`).

| Route | Purpose |
|---|---|
| `GET /help` | Start instructions + Turnstile boundary |
| `GET /status` | Export state (redacted; no secrets/audio) |
| `GET /transcript` | Buffered human/agent text (`?clear=1` to drain) |
| `POST /send_text` | `{"text":"..."}` → speak-injection wire into Live |
| `POST /end` | Tear down |
| `GET /health` | Liveness |

### MCP tools (Python package, control only)

Registered when the gpt2agent server loads this worktree:

| Tool | Purpose |
|---|---|
| `voice_live_export_help` | Docs + boundary |
| `voice_live_status` | Proxy `GET /status` |
| `voice_live_get_transcript` | Proxy `GET /transcript` |
| `voice_live_send_text` | Proxy `POST /send_text` |
| `voice_live_end` | Proxy `POST /end` |

Start the browser sidecar first; then the agent calls these tools. Audio never
enters MCP responses.

## Shipped modules

| Module | Role |
|---|---|
| `src/export.mjs` | **Mode B export plane** — transcript → agent hook → speak queue |
| `src/events.mjs` | Envelope wrap/unwrap, transcript extract, `buildSpeakWire` |
| `src/control.mjs` | Localhost HTTP control plane |
| `src/session.mjs` | WebRTC session orchestration using export plane |
| `src/adapter.mjs` | Verified realtime routes + SDP exchange |
| `src/reconnect.mjs` / `liveness.mjs` | Reliability primitives |
| `browser/sidecar.mjs` | **Runnable** headed-Chrome export entry |

```bash
cd sidecar && npm test
```

## Speak-injection contract

Outbound client message (unit-tested):

```json
{
  "type": "data_message",
  "data": "{\"type\":\"response.create\",\"response\":{\"modalities\":[\"audio\",\"text\"],\"instructions\":\"<agent reply>\"}}"
}
```

Built by `buildSpeakWire(text)` / `ModeBExport.queueSpeak(text)`. Consumer event
names may still refine after live capture; the contract is serializable
control JSON only — never PCM/Opus bytes.

## Investigation notes

Handshake evidence and Turnstile boundary write-ups live under
`docs/superpowers/plans/2026-07-11-*.md`. Catalog-only voice discovery remains
`list_voices` (0.0.13 lane) and is separate from this Live export.
