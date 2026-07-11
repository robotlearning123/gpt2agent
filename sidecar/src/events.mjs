// GPT-Live datachannel event model + envelope helpers.
//
// CORRECT (use these):
//   - DATA_MESSAGE envelope + parseMessage / unwrapDataMessage / wrapDataMessage
//   - CONSUMER_EVENTS — the REAL wire vocabulary the shipped client knows
//   - TranscriptAssembler (re-exported from transcript.mjs) — turns the
//     chat_message_delta JSON-patch stream into human utterances (direction:"in")
//
// @deprecated (behavior retained for source/test compatibility, but the design is
// dead): SERVER_EVENTS / CLIENT_EVENTS / buildSpeakText / buildSpeakWire /
// buildSessionUpdate / extractInputTranscript / isInputTranscriptType /
// EventRouter. These assume the OpenAI Realtime API event names + a client→server
// speak-INJECTION the consumer channel does NOT support. Verified 2026-07-11:
// response.create / conversation.item.create / session.update are silently dropped
// (5 candidates, all dc.send→true, 0 replies). See protocol spec §5.2/§12.

export { TranscriptAssembler, unwrap, isActionable } from "./transcript.mjs";

/** Consumer datachannel envelope type (both directions). */
export const DATA_MESSAGE = "data_message";

/** REAL client event vocabulary (from the shipped client enum, exhaustive). */
export const CONSUMER_EVENTS = Object.freeze({
  CHAT_MESSAGE_DELTA: "chat_message_delta",
  FULL_CHAT_MESSAGE: "full_chat_message",
  CLIENT_METRICS: "client_metrics",
  CLIENT_METADATA_UPDATE: "client_metadata_update",
  TRACK_STATE: "track_state",
  SPAWN_UPDATE: "spawn_update",
  STATE_UPDATE: "state_update",
  STARTUP_TELEMETRY: "startup_telemetry",
  CONVERSATION_UPDATE: "conversation_update",
  CONVERSATION_FOLLOWUP: "conversation_followup",
  USAGE_UPDATE: "usage_update",
  URL_MODERATION: "url_moderation",
  URL_SEARCH: "url_search",
  MODERATION: "moderation",
  INTERRUPTION_SERVER_ERROR: "interruption_server_error",
  USER_SESSION_EXPIRED: "user_session_expired",
  ERROR: "error",
});

/** @deprecated raw Realtime API names — NOT what the consumer channel uses. */
export const SERVER_EVENTS = Object.freeze({
  SESSION_CREATED: "session.created",
  SESSION_UPDATED: "session.updated",
  SPEECH_STARTED: "input_audio_buffer.speech_started",
  SPEECH_STOPPED: "input_audio_buffer.speech_stopped",
  INPUT_TRANSCRIPT_DONE: "conversation.item.input_audio_transcription.completed",
  RESPONSE_CREATED: "response.created",
  RESPONSE_AUDIO_TRANSCRIPT_DELTA: "response.audio_transcript.delta",
  RESPONSE_DONE: "response.done",
  FUNCTION_CALL_ARGS_DONE: "response.function_call_arguments.done",
  ERROR: "error",
});
/** @deprecated raw Realtime API names — NOT honored by the consumer channel. */
export const CLIENT_EVENTS = Object.freeze({
  SESSION_UPDATE: "session.update",
  CONVERSATION_ITEM_CREATE: "conversation.item.create",
  RESPONSE_CREATE: "response.create",
});

export function parseMessage(message) {
  if (message == null) return null;
  if (typeof message === "object") return message;
  if (typeof message !== "string") return null;
  try { return JSON.parse(message); } catch { return null; }
}

export function unwrapDataMessage(message) {
  const outer = parseMessage(message);
  if (!outer || typeof outer !== "object") return null;
  if (outer.type === DATA_MESSAGE && typeof outer.data === "string") return parseMessage(outer.data);
  if (outer.type === DATA_MESSAGE && outer.data && typeof outer.data === "object") return outer.data;
  return outer;
}

export function wrapDataMessage(inner) {
  const data = typeof inner === "string" ? inner : JSON.stringify(inner);
  return JSON.stringify({ type: DATA_MESSAGE, data });
}

/** @deprecated true only for Realtime-API input-transcription types (not consumer). */
export function isInputTranscriptType(type) {
  if (typeof type !== "string" || !type) return false;
  if (type === SERVER_EVENTS.INPUT_TRANSCRIPT_DONE) return true;
  if (type === "audio_transcription") return true;
  if (type === "transcription") return true;
  if (/input_audio_transcription\.(completed|done)$/i.test(type)) return true;
  if (/response\.audio_transcript/i.test(type)) return false;
  return false;
}

/** @deprecated use TranscriptAssembler.feed on chat_message_delta (direction:"in"). */
export function extractInputTranscript(evt) {
  if (!evt || typeof evt !== "object") return null;
  const bodies = [evt];
  if (evt.payload && typeof evt.payload === "object") bodies.push(evt.payload);
  for (const body of bodies) {
    const type = body.type ?? evt.type;
    if (!isInputTranscriptType(type)) continue;
    if (body.role === "assistant" || body.speaker === "assistant") continue;
    const t = body.transcript ?? body.text ?? body.content ?? body.utterance;
    if (typeof t === "string" && t.trim()) return t.trim();
  }
  return null;
}

/** @deprecated speak-injection is not supported by the consumer channel. */
export function buildSpeakText(text) {
  if (typeof text !== "string" || !text.trim()) throw new TypeError("buildSpeakText requires non-empty text");
  return { type: CLIENT_EVENTS.RESPONSE_CREATE, response: { modalities: ["audio", "text"], instructions: text.trim() } };
}

/** @deprecated not honored by the server (kept for source compatibility). */
export function buildSpeakWire(text) {
  return wrapDataMessage(buildSpeakText(text));
}

/** @deprecated consumer channel does not accept session.update. */
export function buildSessionUpdate({ voice, instructions } = {}) {
  const session = {};
  if (voice !== undefined) session.voice = voice;
  if (instructions !== undefined) session.instructions = instructions;
  return { type: CLIENT_EVENTS.SESSION_UPDATE, session };
}

/** @deprecated prefer TranscriptAssembler (real direction:"in" parsing). */
export class EventRouter {
  constructor() {
    this._handlers = new Map();
    this._transcriptHooks = new Set();
    this._unknownHooks = new Set();
  }
  on(type, fn) {
    if (!this._handlers.has(type)) this._handlers.set(type, new Set());
    this._handlers.get(type).add(fn);
    return () => this._handlers.get(type)?.delete(fn);
  }
  onInputTranscript(fn) { this._transcriptHooks.add(fn); return () => this._transcriptHooks.delete(fn); }
  onUnknown(fn) { this._unknownHooks.add(fn); return () => this._unknownHooks.delete(fn); }
  handle(message) {
    const evt = unwrapDataMessage(message);
    if (!evt || typeof evt.type !== "string") return null;
    const transcript = extractInputTranscript(evt);
    if (transcript !== null) for (const fn of this._transcriptHooks) fn(transcript, evt);
    const handlers = this._handlers.get(evt.type);
    if (handlers && handlers.size) { for (const fn of handlers) fn(evt); }
    else if (transcript === null) { for (const fn of this._unknownHooks) fn(evt); }
    return evt.type;
  }
}
