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

The consumer handshake is **captured from the bundle AND confirmed live
end-to-end** — a `POST` of an Opus-audio SDP offer to `/realtime/vp?dcid=0` with
the account bearer (`~/.codex/auth.json`) returns **HTTP 201 + a full SDP answer
with server ICE candidates**. No browser, mic, ephemeral token, or sentinel
needed (`docs/…/2026-07-11-gpt-live-handshake-evidence.md`).

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

## Runnable browser sidecar (recommended path) — `browser/sidecar.mjs`

The Node/werift path completes the handshake but its audio SRTP egress never
reaches the OpenAI server (confirmed: even continuous silence closes ~1s after
`listening`), while ChatGPT's own web client works. So the recommended sidecar
drives a real Chrome — reusing the app's working media stack — and reads the
transcription + response off the datachannel. It feeds a **WAV file as the mic**
(`--use-file-for-fake-audio-capture`), so no real microphone is used.

```bash
cd sidecar && npm install
# one-time: create a Chrome profile logged into ChatGPT
open -na "Google Chrome" --args --user-data-dir="$PWD/.chrome-gptlive"   # sign in, then quit
# make a question WAV
echo "What is two plus two?" | mb voice -o q.mp3 && ffmpeg -y -i q.mp3 -ar 24000 -ac 1 q.wav
# run
node browser/sidecar.mjs --profile "$PWD/.chrome-gptlive" --audio q.wav
```

It injects the datachannel hook via `evaluateOnNewDocument` (before the app's
scripts, which fixes the hook-too-late problem seen with post-load injection),
opens voice, and prints the event stream — you should see the human utterance
transcribed and GPT-Live's response. The `onAgentTurn(humanText)` hook is where
you route the transcript to your agent; voicing the agent's reply back (full Mode
B) still needs the one "speak provided text" outbound command captured (the
normal client only sends `track_state` + `client_metrics`, since the model
auto-responds to audio).

## What remains: wire the WebRTC media (control plane is done)

The token/route/SDP-exchange control plane is verified end-to-end. To get a
working spoken bridge, the remaining work is media transport:

1. **Node WebRTC peer** — add `werift` (pure-JS WebRTC) or `wrtc`, build the real
   offer (Opus audio + negotiated datachannel id 0), run `exchangeSdp` (already
   implemented + tested), then complete ICE/DTLS with the answer's candidates.
2. **Mic + speaker** at the human's machine (the sidecar runs where the human is).
3. **Datachannel event enum** — `session.update` to pin voice/instructions, then
   route `response.*` / input-transcription events through `EventRouter` into the
   Mode B loop (`onUserSaid` → agent → `speak`).
4. **MCP control tools** in the Python server (below).

To enumerate the exact datachannel event names quickly, either connect a real
peer (step 1) or paste `capture/gpt-live-capture.js` into an authenticated
`chatgpt.com` voice session and read `window.__gptLiveCapture.eventTypes`.

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
