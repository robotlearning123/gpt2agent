# gpt2agent GPT-Live voice sidecar (experimental, v0.0.14)

A human-to-agent voice bridge over ChatGPT **GPT-Live**. GPT-Live is full-duplex
audio over WebRTC — it **cannot** be an MCP tool (MCP is request/response and
carries no media). So the bridge splits in two:

- **This sidecar (TypeScript/Node)** owns the WebRTC peer, the microphone, and
  the datachannel. Audio never leaves it.
- **The gpt2agent Python server** exposes a small **control plane** over MCP.

```
  human mic ──WebRTC──▶ sidecar ──datachannel──▶ input transcript
                                                     │
                                     your agent (Claude / gpt2agent) reasons,
                                     calls tools/search over MCP as usual
                                                     │
  human speaker ◀──WebRTC── sidecar ◀── speak(replyText) ── agent's reply
```

This is **Mode B**: GPT-Live is voice I/O, your agent is the brain — which is why
"use tools/search from GPT-Live" works: the agent owns the tool loop, and its
spoken answer is voiced by Live. (Mode A — Live natively calling our tools — is
likely disabled in the consumer product today; see the investigation doc.)

## Status — honest

| Part | State |
|---|---|
| `src/reconnect.mjs` — backoff/attempt ceiling | **done, unit-tested** |
| `src/liveness.mjs` — half-open detection | **done, unit-tested** |
| `src/events.mjs` — datachannel event router + Mode B glue | **done, unit-tested** (event *names* from the public Realtime API; consumer names need capture) |
| `src/session.mjs` — WebRTC lifecycle wiring | skeleton; needs a WebRTC impl + the captured adapter |
| `src/adapter.mjs` — consumer bootstrap + SDP routes | **stub — un-captured**; throws `NotYetCapturedError` |
| `capture/gpt-live-capture.js` — handshake capture harness | ready to run |

Nothing here claims a working end-to-end bridge yet. The single blocker is the
un-captured consumer handshake in `adapter.mjs`.

```bash
cd sidecar && npm test    # 16/16 green (reconnect, liveness, events)
```

## The one remaining step: capture the handshake

The consumer GPT-Live session-bootstrap route, SDP-exchange endpoint, ICE
servers, and datachannel event **type names** have not been captured — a
mic-equipped, signed-in session is required (a headless browser with no audio
device aborts before negotiating). To capture:

1. Open an authenticated `chatgpt.com` tab. Ensure a real or fake audio input
   device exists (headless: launch Chrome with
   `--use-fake-device-for-media-stream --use-fake-ui-for-media-stream`).
2. Paste `capture/gpt-live-capture.js` into the DevTools console.
3. Open a voice session for ~5 seconds, then run
   `copy(JSON.stringify(window.__gptLiveCapture, null, 2))`.
4. Fill the two routes in `src/adapter.mjs` and reconcile the event names in
   `src/events.mjs` with `eventTypes` from the capture.

The capture records **routes and shapes only** — no raw audio, tokens, or
transcript text.

## MCP control plane (to add in the Python server once the adapter is captured)

Audio never crosses MCP; these are control/text only:

| Tool | Purpose |
|---|---|
| `voice_start(voice_mode?, voice?)` | start a session (catalog via `list_voices`) |
| `voice_status()` | state, connection health, reconnect count |
| `voice_send_text(text)` | make Live speak text (also the agent-reply path) |
| `voice_get_transcript()` | drain buffered input/answer transcript text |
| `voice_end()` | tear down |

## Reliability (the "stable & reliable" part)

Implemented and tested here: exponential backoff with jitter + attempt ceiling
(`reconnect.mjs`), and half-open datachannel detection (`liveness.mjs`). Still to
wire in `session.mjs` once the adapter is captured: ICE restart on drop, TURN
fallback from the session's ICE servers, token refresh mid-session, and the
Sentinel challenge on the bootstrap call.
