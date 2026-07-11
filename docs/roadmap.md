# Roadmap

Where gpt2agent is going, and — just as important — where the hard boundaries
are. This page is a dated snapshot of intent, not a compatibility guarantee:
every capability here rides private, reverse-engineered chatgpt.com routes that
can change without notice.

## Version lanes

| Version | Theme | State |
|---|---|---|
| `0.0.11` | Recovery release; hardened release-source verification | Published to PyPI |
| `0.0.12` | Account-native feature coverage (read-only introspection breadth) | Design + cross-model review complete; implementation on a separate lane |
| `0.0.13` | Voice **catalog** (`list_voices`) | Code complete; release held until 0.0.12 lands |
| `0.0.14` | **Real live voice (GPT-Live)** — experimental TypeScript WebRTC sidecar | Investigation; evidence capture first |

Lanes ship in order. 0.0.13 does not invent or supersede 0.0.12; the two are
independent branches and merge in sequence.

## The GPT-Live boundary

Voice is an official ChatGPT product, but the routes this project touches are
private website contracts. The line between what ships and what does not:

**Supported (0.0.13):**

- Voice **catalog discovery** via `list_voices` — the account's current voice
  IDs and display metadata from `GET /backend-api/settings/voices`, projected
  to a bounded, redacted, read-only shape.

**Not supported (and why):**

- **GPT-Live realtime audio** — full-duplex low-latency speech-to-speech. MCP
  is a request/response tool protocol, not an audio transport, so GPT-Live
  cannot be a plain MCP tool. It rides browser-native WebRTC.
- **Microphone / playback transport, speech synthesis, preview-media fetch** —
  no audio ever crosses the MCP boundary today.
- **Guaranteed post-session transcript** — official docs say a transcript lands
  in chat history, but this project has not proven a stable adapter for that
  shape; it stays inventory-only and unverified.

The current official Voice documentation also excludes connected apps/plugins,
Work, Codex, custom GPTs, temporary chats, and desktop from initial Live
support — which constrains any "let Live call out to an external agent" design.

**0.0.14 direction.** A real live-voice bridge is pursued as an *optional,
disabled-by-default, experimental* lane: a small TypeScript/browser sidecar owns
the WebRTC and media APIs; Python remains the MCP control plane exposing a
control-only surface (`start`, `status`, `send_text`, `end`, `get_transcript`).
Audio stays local to the sidecar and never transits MCP. No sidecar ships until
a real captured handshake, benchmarks, and a separate safety design justify it.

## Language policy

- The MCP core stays **Python**. Network latency, server processing, and
  streaming dominate this workload; the mature `curl_cffi` client, tested
  transport, authentication, and redaction code are kept with the smallest safe
  diff.
- The GPT-Live lane may add an **optional TypeScript/browser sidecar** for
  browser-native WebRTC and media APIs. It is isolated so private media churn
  cannot destabilize the Python read server.
- **Rust** is reserved for a measured CPU, memory, or transport bottleneck that
  cannot be resolved in the current architecture — not adopted speculatively.

## Release gates

Every release must clear, in order:

1. Full offline `pytest` suite green (live/network tests auto-skip).
2. `ruff check gpt2agent tests scripts` clean.
3. `scripts/verify_release.py` — all version fields (`pyproject.toml`,
   `gpt2agent/__init__.py`, `.claude-plugin/plugin.json`, `server.json`) agree
   and `CHANGELOG.md` has a dated section for the release.
4. Wheel + sdist build, `twine check`, and a clean-environment install of each
   artifact.
5. One GET-only live-contract check (schema/shape only; no raw payload
   persisted) when the change touches a private route.
6. Independent cross-model review of the actual diff before merge.
7. Post-merge `main` CI green on the exact merged commit before an annotated tag
   is pushed.
