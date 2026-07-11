// Mode B export plane: GPT-Live voice I/O ↔ agent text.
//
// This is the product boundary for "export GPT-Live to an agent":
//   - inbound: datachannel events → human transcript text → pluggable agent hook
//   - outbound: agent reply text → speak-injection wire message (no audio bytes)
//
// NEVER stores raw audio, bearer tokens, cookies, or account secrets.
// Audio stays in the browser / WebRTC peer that owns the media session.

import {
  EventRouter,
  buildSpeakWire,
  buildSpeakText,
  extractInputTranscript,
  unwrapDataMessage,
  wrapDataMessage,
} from "./events.mjs";

export const ExportState = Object.freeze({
  IDLE: "idle",
  LIVE: "live",
  CLOSED: "closed",
});

/**
 * Sanitize a value so it can never leak secrets/audio into agent-facing status.
 * Blocks known credential/media *field names* only — not keys that merely
 * mention "audio" in a boolean flag (e.g. audioCrossesBoundary).
 */
export function redactForAgent(value, depth = 0) {
  if (depth > 6) return "[truncated]";
  if (value == null) return value;
  if (typeof value === "string") {
    // Never return JWT-shaped or long opaque blobs via status APIs.
    if (/^Bearer\s+/i.test(value)) return "[redacted]";
    if (/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(value)) return "[redacted]";
    return value;
  }
  if (typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((v) => redactForAgent(v, depth + 1));
  const blocked = new Set([
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "authorization",
    "cookie",
    "cookies",
    "password",
    "secret",
    "client_secret",
    "audio",
    "audio_bytes",
    "audioBytes",
    "pcm",
    "sdp",
    "offersdp",
    "answersdp",
    "proof_token",
    "prooftoken",
    "sentinel",
    "wire",
  ]);
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    const lk = k.toLowerCase();
    // Exact blocked names, or explicit secret/credential suffixes — not "audio*" flags.
    const isBlocked =
      blocked.has(lk) ||
      lk.endsWith("_token") ||
      lk.endsWith("token") ||
      lk.endsWith("_secret") ||
      lk.endsWith("password") ||
      lk.endsWith("_cookie") ||
      lk === "cookies" ||
      lk.endsWith("_sdp") ||
      lk.endsWith("sdp");
    if (isBlocked) {
      out[k] = "[redacted]";
    } else {
      out[k] = redactForAgent(v, depth + 1);
    }
  }
  return out;
}

/**
 * Mode B export controller.
 *
 * @param {{
 *   onAgentTurn?: (humanText: string) => (string|null|Promise<string|null>),
 *   maxTranscripts?: number,
 * }} [opts]
 */
export class ModeBExport {
  constructor(opts = {}) {
    this._onAgentTurn = typeof opts.onAgentTurn === "function" ? opts.onAgentTurn : null;
    this._maxTranscripts = opts.maxTranscripts ?? 200;
    /** @type {Array<{role:"human"|"agent"|"assistant", text:string, at:number}>} */
    this._transcripts = [];
    /** @type {string[]} wire-ready speak messages waiting to be sent on the dc */
    this._speakQueue = [];
    this.state = ExportState.IDLE;
    this.router = new EventRouter();
    this._turns = 0;
    this._lastError = null;

    this.router.onInputTranscript((text) => {
      // Sync path only records; async agent turn is driven from handleInbound.
      this._push("human", text);
    });
  }

  /** Pluggable agent brain: human utterance → optional reply text for Live TTS. */
  setAgentTurn(fn) {
    if (fn != null && typeof fn !== "function") {
      throw new TypeError("onAgentTurn must be a function");
    }
    this._onAgentTurn = fn;
  }

  setState(state) {
    if (!Object.values(ExportState).includes(state)) {
      throw new RangeError(`invalid export state: ${state}`);
    }
    this.state = state;
  }

  _push(role, text) {
    if (typeof text !== "string" || !text.trim()) return;
    this._transcripts.push({ role, text: text.trim(), at: Date.now() });
    if (this._transcripts.length > this._maxTranscripts) {
      this._transcripts.splice(0, this._transcripts.length - this._maxTranscripts);
    }
  }

  /** Record a text-only transcript line (human or agent). No audio. */
  record(role, text) {
    if (role !== "human" && role !== "agent" && role !== "assistant") {
      throw new TypeError("role must be human|agent|assistant");
    }
    this._push(role, text);
  }

  /**
   * Build the speak-injection wire message for an agent reply.
   * Throws on empty text (contract).
   * @returns {string} JSON string for datachannel.send — never audio bytes
   */
  buildSpeakWire(text) {
    return buildSpeakWire(text);
  }

  /**
   * Queue agent reply text to be spoken by Live. Returns the wire payload.
   * @returns {string}
   */
  queueSpeak(text) {
    const wire = this.buildSpeakWire(text);
    this._speakQueue.push(wire);
    this._push("agent", text);
    return wire;
  }

  /** Drain queued speak wires (caller sends them on the live datachannel). */
  drainSpeakQueue() {
    const q = this._speakQueue;
    this._speakQueue = [];
    return q;
  }

  /**
   * Handle one inbound datachannel message (string or object).
   * If a human transcript is extracted and an agent hook is set, awaits the
   * hook and queues a speak wire for any non-empty reply.
   *
   * @returns {Promise<{
   *   type: string|null,
   *   humanText: string|null,
   *   agentReply: string|null,
   *   speakWires: string[],
   * }>}
   */
  async handleInbound(raw) {
    const evt = unwrapDataMessage(raw);
    const type = this.router.handle(raw);
    let humanText = extractInputTranscript(evt);
    let agentReply = null;
    const speakWires = [];

    if (humanText && this._onAgentTurn) {
      this._turns += 1;
      try {
        const reply = await this._onAgentTurn(humanText);
        if (typeof reply === "string" && reply.trim()) {
          agentReply = reply.trim();
          speakWires.push(this.queueSpeak(agentReply));
        }
      } catch (err) {
        this._lastError = err instanceof Error ? err.message : String(err);
      }
    }

    // Also drain any speak wires queued outside this turn.
    for (const w of this.drainSpeakQueue()) {
      if (!speakWires.includes(w)) speakWires.push(w);
    }

    return { type, humanText, agentReply, speakWires };
  }

  /** Buffered transcript entries (text only). */
  getTranscripts({ clear = false } = {}) {
    const copy = this._transcripts.map(({ role, text, at }) => ({ role, text, at }));
    if (clear) this._transcripts = [];
    return copy;
  }

  /**
   * Agent-safe status snapshot — no tokens, no audio, no SDP.
   * @returns {object}
   */
  status() {
    return redactForAgent({
      state: this.state,
      turns: this._turns,
      transcriptCount: this._transcripts.length,
      speakQueueLength: this._speakQueue.length,
      hasAgentHook: Boolean(this._onAgentTurn),
      lastError: this._lastError,
      boundary: {
        audioCrossesBoundary: false,
        secretsCrossBoundary: false,
        mediaOwner: "browser-or-webrtc-peer",
        agentSurface: "text-and-control-only",
        turnstileBypass: "out-of-scope",
      },
    });
  }

  close() {
    this._speakQueue = [];
    this.setState(ExportState.CLOSED);
  }
}

// Re-export speak builders so agents import one module for the contract.
export {
  buildSpeakText,
  buildSpeakWire,
  extractInputTranscript,
  wrapDataMessage,
  unwrapDataMessage,
};
