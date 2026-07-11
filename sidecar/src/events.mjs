// GPT-Live datachannel event routing + Mode B glue.
//
// SOURCE / VERIFICATION STATUS:
//   The event *type names* below are the OpenAI **Realtime API** contract. The
//   ChatGPT consumer bundle was confirmed to use the same Realtime event model
//   (observed fragments: `session.update`, `response.*`, `audio_transcription`,
//   `output_audio_buffer_depth_ms` — see the 2026-07-11 handshake evidence doc),
//   so the family is verified even though the full enum is minified. A live
//   confirmation POST will enumerate the exact names; reconcile any deltas here.
//
// Consumer wire envelope (captured 2026-07-11): both directions use
//   { type: "data_message", data: "<inner json string>" }
//
// Mode B (the goal): GPT-Live is voice I/O, our agent is the brain.
//   inbound  input-transcript event  -> hand text to the agent
//   outbound response.create(text)   -> Live speaks the agent's reply
// Audio never leaves the sidecar; only these text/control events cross to MCP.

/** Server -> client Realtime events we care about (needs-consumer-verification). */
export const SERVER_EVENTS = Object.freeze({
  SESSION_CREATED: "session.created",
  SESSION_UPDATED: "session.updated",
  SPEECH_STARTED: "input_audio_buffer.speech_started",
  SPEECH_STOPPED: "input_audio_buffer.speech_stopped",
  // The human's words, transcribed — the Mode B input signal:
  INPUT_TRANSCRIPT_DONE: "conversation.item.input_audio_transcription.completed",
  RESPONSE_CREATED: "response.created",
  RESPONSE_AUDIO_TRANSCRIPT_DELTA: "response.audio_transcript.delta",
  RESPONSE_DONE: "response.done",
  FUNCTION_CALL_ARGS_DONE: "response.function_call_arguments.done",
  ERROR: "error",
});

/** Client -> server Realtime events we emit (needs-consumer-verification). */
export const CLIENT_EVENTS = Object.freeze({
  SESSION_UPDATE: "session.update",
  CONVERSATION_ITEM_CREATE: "conversation.item.create",
  RESPONSE_CREATE: "response.create",
});

/** Consumer datachannel envelope type (both directions). */
export const DATA_MESSAGE = "data_message";

/**
 * Parse a datachannel payload into an object. Accepts objects or JSON strings.
 * @returns {object|null}
 */
export function parseMessage(message) {
  if (message == null) return null;
  if (typeof message === "object") return message;
  if (typeof message !== "string") return null;
  try {
    return JSON.parse(message);
  } catch {
    return null;
  }
}

/**
 * Unwrap the consumer `{type:"data_message", data:"..."}` envelope if present.
 * Returns the inner event object, or the original object when not enveloped.
 * @returns {object|null}
 */
export function unwrapDataMessage(message) {
  const outer = parseMessage(message);
  if (!outer || typeof outer !== "object") return null;
  if (outer.type === DATA_MESSAGE && typeof outer.data === "string") {
    return parseMessage(outer.data);
  }
  if (outer.type === DATA_MESSAGE && outer.data && typeof outer.data === "object") {
    return outer.data;
  }
  return outer;
}

/**
 * Wrap an inner client event for the consumer datachannel wire format.
 * @param {object|string} inner
 * @returns {string} JSON string ready for RTCDataChannel.send
 */
export function wrapDataMessage(inner) {
  const data = typeof inner === "string" ? inner : JSON.stringify(inner);
  return JSON.stringify({ type: DATA_MESSAGE, data });
}

/**
 * True when this event type represents a completed *human input* transcription
 * (not the assistant's spoken transcript deltas).
 */
export function isInputTranscriptType(type) {
  if (typeof type !== "string" || !type) return false;
  if (type === SERVER_EVENTS.INPUT_TRANSCRIPT_DONE) return true;
  if (type === "audio_transcription") return true;
  if (type === "transcription") return true;
  if (/input_audio_transcription\.(completed|done)$/i.test(type)) return true;
  // Explicitly not response/assistant audio transcripts:
  if (/response\.audio_transcript/i.test(type)) return false;
  return false;
}

/**
 * Extract the transcribed human utterance from an input-transcription event.
 * Kept tolerant: the consumer payload key may differ from the API's `transcript`.
 * Accepts raw or already-unwrapped events; skips assistant/response transcripts.
 * @returns {string|null}
 */
export function extractInputTranscript(evt) {
  if (!evt || typeof evt !== "object") return null;

  // Consumer often nests under payload; also accept already-flat Realtime events.
  const bodies = [evt];
  if (evt.payload && typeof evt.payload === "object") bodies.push(evt.payload);

  for (const body of bodies) {
    const type = body.type ?? evt.type;
    if (!isInputTranscriptType(type)) continue;
    // Skip assistant-side labels if present.
    if (body.role === "assistant" || body.speaker === "assistant") continue;
    const t = body.transcript ?? body.text ?? body.content ?? body.utterance;
    if (typeof t === "string" && t.trim()) return t.trim();
  }
  return null;
}

/**
 * Build the client event that makes GPT-Live speak the agent's text.
 * The Realtime way to voice provided text is a response.create carrying explicit
 * instructions; the consumer channel may instead want a conversation.item.create
 * with an assistant message — confirm from capture, then adjust here only.
 */
export function buildSpeakText(text) {
  if (typeof text !== "string" || !text.trim()) {
    throw new TypeError("buildSpeakText requires non-empty text");
  }
  return {
    type: CLIENT_EVENTS.RESPONSE_CREATE,
    response: { modalities: ["audio", "text"], instructions: text.trim() },
  };
}

/**
 * Wire-ready speak payload: envelope-wrapped JSON string for datachannel.send.
 * This is the agent → Live speak-injection contract used by Mode B export.
 * @returns {string}
 */
export function buildSpeakWire(text) {
  return wrapDataMessage(buildSpeakText(text));
}

/** Build the session.update that pins the voice/instructions after connect. */
export function buildSessionUpdate({ voice, instructions } = {}) {
  const session = {};
  if (voice !== undefined) session.voice = voice;
  if (instructions !== undefined) session.instructions = instructions;
  return { type: CLIENT_EVENTS.SESSION_UPDATE, session };
}

/**
 * Minimal, dependency-free event dispatcher for datachannel messages.
 * Register handlers by exact event type; `onInputTranscript` is the Mode B hook.
 * Automatically unwraps consumer `data_message` envelopes.
 */
export class EventRouter {
  constructor() {
    /** @type {Map<string, Set<Function>>} */
    this._handlers = new Map();
    /** @type {Set<Function>} */
    this._transcriptHooks = new Set();
    /** @type {Set<Function>} */
    this._unknownHooks = new Set();
  }

  on(type, fn) {
    if (!this._handlers.has(type)) this._handlers.set(type, new Set());
    this._handlers.get(type).add(fn);
    return () => this._handlers.get(type)?.delete(fn);
  }

  /** Fires with the transcribed human text on each completed input transcription. */
  onInputTranscript(fn) {
    this._transcriptHooks.add(fn);
    return () => this._transcriptHooks.delete(fn);
  }

  /** Fires for any event type with no registered handler — capture blind spots. */
  onUnknown(fn) {
    this._unknownHooks.add(fn);
    return () => this._unknownHooks.delete(fn);
  }

  /**
   * Dispatch one datachannel message. Accepts a parsed object or a JSON string.
   * Unwraps consumer data_message envelopes. Returns the event's type, or null.
   */
  handle(message) {
    const evt = unwrapDataMessage(message);
    if (!evt || typeof evt.type !== "string") return null;

    const transcript = extractInputTranscript(evt);
    if (transcript !== null) {
      for (const fn of this._transcriptHooks) fn(transcript, evt);
    }

    const handlers = this._handlers.get(evt.type);
    if (handlers && handlers.size) {
      for (const fn of handlers) fn(evt);
    } else if (transcript === null) {
      for (const fn of this._unknownHooks) fn(evt);
    }
    return evt.type;
  }
}
