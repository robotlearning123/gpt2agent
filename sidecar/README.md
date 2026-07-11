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
| `src/events.mjs` — datachannel event router + Mode B glue | **done, unit-tested**; event model confirmed Realtime-style from the bundle |
| `src/adapter.mjs` — realtime routes + SDP exchange | **done, unit-tested**; routes VERIFIED from the shipped bundle (see evidence doc) |
| `src/session.mjs` — WebRTC lifecycle wiring | wired to the real adapter; needs a WebRTC impl (browser / `werift`) to run |
| `capture/gpt-live-capture.js` — live confirmation harness | ready to run |

The consumer handshake is **captured** — read directly from ChatGPT's public web
bundle, no session/mic/credentials (`docs/…/2026-07-11-gpt-live-handshake-evidence.md`):

| mode | SDP-exchange endpoint |
|---|---|
| standard | `https://chatgpt.com/realtime/vps?dcid=0` |
| advanced | `https://chatgpt.com/realtime/vp?dcid=0` |
| wingman | `https://chatgpt.com/realtime/wm?dcid=0` |

SDP is a single-shot `POST offer.sdp` (`Content-Type: application/sdp`,
`Authorization: Bearer <token>`) whose response body is the answer SDP;
datachannel is negotiated, `id:0`.

```bash
cd sidecar && npm test    # 21/21 green (reconnect, liveness, events, adapter)
```

## The one remaining step: a live confirmation POST

Routes and mechanism are verified from shipped code; what remains is a single
authenticated round-trip to confirm the **token source** (the SDP `Authorization`
bearer — very likely the account token gpt2agent already loads from
`~/.codex/auth.json`, which would mean the sidecar bootstraps with **no browser
at all**) and to enumerate the datachannel event names + ICE servers.

Two ways to run it:
- **Token path (no browser):** POST a throwaway SDP offer to `/realtime/vp?dcid=0`
  with the account bearer; a `200` + SDP answer confirms the token path.
- **Console harness:** paste `capture/gpt-live-capture.js` into an authenticated
  `chatgpt.com` voice session (real or `--use-fake-device-for-media-stream`),
  run ~5s, then `copy(JSON.stringify(window.__gptLiveCapture, null, 2))`. Records
  routes/shapes only — no raw audio, tokens, or transcript text.

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
