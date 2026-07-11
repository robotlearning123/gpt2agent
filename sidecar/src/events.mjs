// GPT-Live datachannel event routing + Mode B glue.
//
// SOURCE / VERIFICATION STATUS:
//   The event *type names* below are the OpenAI **Realtime API** contract
//   (public, documented, stable). ChatGPT's consumer "GPT-Live" voice is built
//   on the same Realtime infrastructure, so this is the well-founded starting
//   shape — but the consumer datachannel has NOT been captured yet (headless/no
//   authenticated-session blocker). Treat every name here as
//   (needs-consumer-verification): run sidecar/capture/gpt-live-capture.js in an
//   authenticated session to confirm the real type names, then reconcile.
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

/**
 * Extract the transcribed human utterance from an input-transcription event.
 * Kept tolerant: the consumer payload key may differ from the API's `transcript`.
 * @returns {string|null}
 */
export function extractInputTranscript(evt) {
  if (!evt || evt.type !== SERVER_EVENTS.INPUT_TRANSCRIPT_DONE) return null;
  const t = evt.transcript ?? evt.text ?? evt.content;
  return typeof t === "string" && t.trim() ? t : null;
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
    response: { modalities: ["audio", "text"], instructions: text },
  };
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
   * Returns the event's type, or null if it could not be parsed.
   */
  handle(message) {
    let evt = message;
    if (typeof message === "string") {
      try {
        evt = JSON.parse(message);
      } catch {
        return null;
      }
    }
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
