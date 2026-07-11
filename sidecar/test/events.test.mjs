import { test } from "node:test";
import assert from "node:assert/strict";
import {
  EventRouter,
  SERVER_EVENTS,
  CLIENT_EVENTS,
  extractInputTranscript,
  buildSpeakText,
  buildSessionUpdate,
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

test("buildSpeakText produces a response.create carrying the text", () => {
  const msg = buildSpeakText("the weather is fine");
  assert.equal(msg.type, CLIENT_EVENTS.RESPONSE_CREATE);
  assert.deepEqual(msg.response.modalities, ["audio", "text"]);
  assert.equal(msg.response.instructions, "the weather is fine");
  assert.throws(() => buildSpeakText(""), TypeError);
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
  assert.equal(router.handle(JSON.stringify({ type: SERVER_EVENTS.RESPONSE_DONE })), SERVER_EVENTS.RESPONSE_DONE);
  assert.deepEqual(seen, [SERVER_EVENTS.RESPONSE_DONE]);
  assert.equal(router.handle("not json"), null);
  assert.equal(router.handle({ notype: true }), null);
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
