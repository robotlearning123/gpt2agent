// The bridge layer (② in docs/superpowers/plans/2026-07-11-gpt-live-bridge-layer-spec.md).
//
// Direction is human → agent ONLY. This layer:
//   - ingests the real consumer GPT-Live datachannel (chat_message_delta) via
//     TranscriptAssembler → completed HUMAN utterances (direction:"in"),
//   - filters filler/acks (isActionable),
//   - routes each real utterance to a pluggable coding-agent hook (onAgentTurn),
//   - records the agent's reply as text for OUT-OF-BAND egress to the human
//     (overlay / side-UI) — never injected back into GPT-Live.
//
// There is NO agent→Live speak channel: the consumer datachannel silently drops
// client-injected speak/response events (verified). Audio, bearer tokens, cookies,
// and SDP never cross this layer.

import { TranscriptAssembler, isActionable } from "./transcript.mjs";

export const ExportState = Object.freeze({
  IDLE: "idle",
  LIVE: "live",
  CLOSED: "closed",
});

/**
 * Sanitize a value so it can never leak secrets/audio into agent-facing status.
 * Blocks known credential/media field names; recurses dicts, arrays, and strings
 * (redacting an embedded Bearer/JWT). Does not redact boolean flags that merely
 * mention "audio" (e.g. audioCrossesBoundary).
 */
export function redactForAgent(value, depth = 0) {
  if (depth > 6) return "[truncated]";
  if (value == null) return value;
  if (typeof value === "string") {
    // Redact a Bearer token or JWT appearing ANYWHERE in the string (e.g. embedded
    // in a lastError message), not only when the whole string is one token.
    return value
      .replace(/Bearer\s+[A-Za-z0-9._-]+/gi, "[redacted]")
      .replace(/[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g, "[redacted]");
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
    "audiobytes",
    "pcm",
    "sdp",
    "offersdp",
    "answersdp",
    "proof_token",
    "prooftoken",
    "sentinel",
  ]);
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    const lk = k.toLowerCase();
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
    out[k] = isBlocked ? "[redacted]" : redactForAgent(v, depth + 1);
  }
  return out;
}

/**
 * Bridge layer controller (kept exported as `ModeBExport` for import stability).
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
    /** @type {Array<{role:"human"|"agent", text:string, at:number}>} */
    this._transcripts = [];
    this.state = ExportState.IDLE;
    this._turns = 0;
    this._lastError = null;
    this._assembler = new TranscriptAssembler();
    /** @type {Set<(text:string)=>void>} fired on each completed human utterance */
    this._humanHooks = new Set();
  }

  /** Pluggable agent brain: human utterance → optional reply text (out-of-band). */
  setAgentTurn(fn) {
    if (fn != null && typeof fn !== "function") {
      throw new TypeError("onAgentTurn must be a function");
    }
    this._onAgentTurn = fn;
  }

  /** Subscribe to completed human utterances (e.g. liveness). Returns an unsubscribe. */
  onHumanUtterance(fn) {
    this._humanHooks.add(fn);
    return () => this._humanHooks.delete(fn);
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
    if (role !== "human" && role !== "agent") {
      throw new TypeError("role must be human|agent");
    }
    this._push(role, text);
  }

  /**
   * Ingest one inbound datachannel message (string or object) through the real
   * consumer-protocol parser. For each completed, actionable HUMAN utterance,
   * records it, fires hooks, and (if an agent hook is set) awaits a reply and
   * records it as text for out-of-band egress. Never returns audio or wires.
   *
   * @returns {Promise<{ humanText: string|null, agentReply: string|null }>}
   */
  async ingest(raw) {
    const utterances = this._assembler.feed(raw); // completed human strings (direction:"in")
    let humanText = null;
    let agentReply = null;
    for (const u of utterances) {
      const r = await this.handleUtterance(u);
      if (r.humanText) {
        humanText = r.humanText;
        agentReply = r.agentReply;
      }
    }
    return { humanText, agentReply };
  }

  /**
   * Handle one ALREADY-EXTRACTED human utterance (e.g. from the extension, which
   * parses the datachannel in-page). Applies the same filter → record → agent →
   * record-reply pipeline as ingest, so every entry path shares isActionable, the
   * capped transcript buffer, hooks, and error handling.
   *
   * @returns {Promise<{ humanText: string|null, agentReply: string|null }>}
   */
  async handleUtterance(text) {
    if (typeof text !== "string" || !isActionable(text)) {
      return { humanText: null, agentReply: null };
    }
    const humanText = text.trim();
    this._push("human", humanText);
    for (const fn of this._humanHooks) {
      try {
        fn(humanText);
      } catch {
        /* hook errors never break the pipeline */
      }
    }
    let agentReply = null;
    if (this._onAgentTurn) {
      this._turns += 1;
      try {
        const reply = await this._onAgentTurn(humanText);
        if (typeof reply === "string" && reply.trim()) {
          agentReply = reply.trim();
          this._push("agent", agentReply);
        }
      } catch (err) {
        this._lastError = err instanceof Error ? err.message : String(err);
      }
    }
    return { humanText, agentReply };
  }

  /** Buffered transcript entries (text only). */
  getTranscripts({ clear = false } = {}) {
    const copy = this._transcripts.map(({ role, text, at }) => ({ role, text, at }));
    if (clear) this._transcripts = [];
    return copy;
  }

  /**
   * Agent-safe status snapshot — no tokens, no audio, no SDP, no speak channel.
   * @returns {object}
   */
  status() {
    return redactForAgent({
      state: this.state,
      turns: this._turns,
      transcriptCount: this._transcripts.length,
      hasAgentHook: Boolean(this._onAgentTurn),
      lastError: this._lastError,
      boundary: {
        direction: "human-to-agent",
        audioCrossesBoundary: false,
        secretsCrossBoundary: false,
        speakInjection: "unsupported (server drops it)",
        mediaOwner: "browser-or-webrtc-peer",
        agentSurface: "text-and-control-only",
        turnstileBypass: "out-of-scope",
      },
    });
  }

  close() {
    this.setState(ExportState.CLOSED);
  }
}
