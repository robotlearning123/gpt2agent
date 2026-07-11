import { test } from "node:test";
import assert from "node:assert/strict";
import {
  EventRouter,
  SERVER_EVENTS,
  CLIENT_EVENTS,
  DATA_MESSAGE,
  extractInputTranscript,
  buildSpeakText,
  buildSpeakWire,
  buildSessionUpdate,
  wrapDataMessage,
  unwrapDataMessage,
  isInputTranscriptType,
} from "../src/events.mjs";

test("extractInputTranscript reads the human utterance, tolerant of key", () => {
  assert.equal(
    extractInputTranscript({ type: SERVER_EVENTS.INPUT_TRANSCRIPT_DONE, transcript: "hello" }),
    "hello",
  );
  assert.equal(
    extractInputTranscript({ type: SERVER_EVENTS.INPUT_TRANSCRIPT_DONE, text: "hi" }),
    "hi",
  );
  assert.equal(extractInputTranscript({ type: SERVER_EVENTS.RESPONSE_DONE }), null);
  assert.equal(
    extractInputTranscript({ type: SERVER_EVENTS.INPUT_TRANSCRIPT_DONE, transcript: "  " }),
    null,
  );
});

test("extractInputTranscript accepts consumer audio_transcription + payload nest", () => {
  assert.equal(
    extractInputTranscript({ type: "audio_transcription", transcript: "weather please" }),
    "weather please",
  );
  assert.equal(
    extractInputTranscript({
      type: "state_update",
      payload: { type: SERVER_EVENTS.INPUT_TRANSCRIPT_DONE, text: "nested hi" },
    }),
    "nested hi",
  );
  assert.equal(
    extractInputTranscript({
      type: SERVER_EVENTS.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
      transcript: "assistant should not count",
    }),
    null,
  );
});

test("isInputTranscriptType distinguishes input vs response", () => {
  assert.equal(isInputTranscriptType(SERVER_EVENTS.INPUT_TRANSCRIPT_DONE), true);
  assert.equal(isInputTranscriptType("audio_transcription"), true);
  assert.equal(isInputTranscriptType(SERVER_EVENTS.RESPONSE_AUDIO_TRANSCRIPT_DELTA), false);
});

test("buildSpeakText produces a response.create carrying the text", () => {
  const msg = buildSpeakText("the weather is fine");
  assert.equal(msg.type, CLIENT_EVENTS.RESPONSE_CREATE);
  assert.deepEqual(msg.response.modalities, ["audio", "text"]);
  assert.equal(msg.response.instructions, "the weather is fine");
  assert.throws(() => buildSpeakText(""), TypeError);
  assert.throws(() => buildSpeakText("   "), TypeError);
});

test("buildSpeakWire is serializable data_message envelope for client→server", () => {
  const wire = buildSpeakWire("say hello");
  assert.equal(typeof wire, "string");
  const outer = JSON.parse(wire);
  assert.equal(outer.type, DATA_MESSAGE);
  assert.equal(typeof outer.data, "string");
  const inner = JSON.parse(outer.data);
  assert.equal(inner.type, CLIENT_EVENTS.RESPONSE_CREATE);
  assert.equal(inner.response.instructions, "say hello");
  // Must be control JSON only — never binary audio.
  assert.equal(wire.includes("\0"), false);
  assert.throws(() => buildSpeakWire(""), TypeError);
});

test("wrap/unwrap data_message round-trips", () => {
  const inner = { type: "track_state", payload: { state: "live" } };
  const wire = wrapDataMessage(inner);
  assert.deepEqual(unwrapDataMessage(wire), inner);
  assert.deepEqual(unwrapDataMessage(inner), inner);
  assert.equal(unwrapDataMessage("not-json"), null);
});

test("buildSessionUpdate pins only the fields provided", () => {
  assert.deepEqual(buildSessionUpdate({ voice: "cove" }), {
    type: CLIENT_EVENTS.SESSION_UPDATE,
    session: { voice: "cove" },
  });
  assert.deepEqual(buildSessionUpdate(), {
    type: CLIENT_EVENTS.SESSION_UPDATE,
    session: {},
  });
});

test("EventRouter dispatches by exact type and parses JSON strings", () => {
  const router = new EventRouter();
  const seen = [];
  router.on(SERVER_EVENTS.RESPONSE_DONE, (e) => seen.push(e.type));
  assert.equal(
    router.handle(JSON.stringify({ type: SERVER_EVENTS.RESPONSE_DONE })),
    SERVER_EVENTS.RESPONSE_DONE,
  );
  assert.deepEqual(seen, [SERVER_EVENTS.RESPONSE_DONE]);
  assert.equal(router.handle("not json"), null);
  assert.equal(router.handle({ notype: true }), null);
});

test("EventRouter unwraps consumer data_message for Mode B transcripts", () => {
  const router = new EventRouter();
  const heard = [];
  router.onInputTranscript((text) => heard.push(text));
  const wire = wrapDataMessage({
    type: SERVER_EVENTS.INPUT_TRANSCRIPT_DONE,
    transcript: "what's the weather",
  });
  assert.equal(router.handle(wire), SERVER_EVENTS.INPUT_TRANSCRIPT_DONE);
  assert.deepEqual(heard, ["what's the weather"]);
});

test("onInputTranscript is the Mode B hook", () => {
  const router = new EventRouter();
  const heard = [];
  router.onInputTranscript((text) => heard.push(text));
  router.handle({ type: SERVER_EVENTS.INPUT_TRANSCRIPT_DONE, transcript: "what's the weather" });
  assert.deepEqual(heard, ["what's the weather"]);
});

test("onUnknown surfaces unhandled event types (capture blind spots)", () => {
  const router = new EventRouter();
  const unknown = [];
  router.onUnknown((e) => unknown.push(e.type));
  router.handle({ type: "some.future.event" });
  assert.deepEqual(unknown, ["some.future.event"]);
});
