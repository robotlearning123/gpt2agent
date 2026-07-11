// Tests for the corrected consumer GPT-Live event model (events.mjs).
// Covers the REAL protocol: data_message envelope, the client event vocabulary,
// and delegation to TranscriptAssembler. The deprecated Realtime-API injection
// symbols are asserted only to exist (they are intentionally dead).
import test from "node:test";
import assert from "node:assert/strict";
import {
  DATA_MESSAGE, CONSUMER_EVENTS,
  parseMessage, unwrapDataMessage, wrapDataMessage,
  TranscriptAssembler, buildSpeakWire,
} from "../src/events.mjs";

test("DATA_MESSAGE envelope wraps an inner event as a JSON string", () => {
  const w = wrapDataMessage({ type: "track_state", payload: { state: "live" } });
  const o = JSON.parse(w);
  assert.equal(o.type, "data_message");
  assert.deepEqual(JSON.parse(o.data), { type: "track_state", payload: { state: "live" } });
});

test("unwrapDataMessage reverses the envelope and passes through bare objects", () => {
  const inner = unwrapDataMessage(wrapDataMessage({ type: "x", v: 1 }));
  assert.deepEqual(inner, { type: "x", v: 1 });
  assert.deepEqual(unwrapDataMessage({ type: "y" }), { type: "y" });
  assert.equal(unwrapDataMessage("not json"), null);
});

test("parseMessage accepts objects, JSON strings, and rejects garbage", () => {
  assert.deepEqual(parseMessage({ a: 1 }), { a: 1 });
  assert.deepEqual(parseMessage('{"a":1}'), { a: 1 });
  assert.equal(parseMessage("nope"), null);
  assert.equal(parseMessage(null), null);
});

test("CONSUMER_EVENTS holds the real client wire vocabulary", () => {
  assert.equal(CONSUMER_EVENTS.CHAT_MESSAGE_DELTA, "chat_message_delta");
  assert.equal(CONSUMER_EVENTS.TRACK_STATE, "track_state");
  assert.equal(CONSUMER_EVENTS.CLIENT_METRICS, "client_metrics");
  assert.equal(CONSUMER_EVENTS.SPAWN_UPDATE, "spawn_update");
  // the raw Realtime-API names the old code used are NOT in the real vocabulary:
  assert.equal(CONSUMER_EVENTS.RESPONSE_CREATE, undefined);
});

test("TranscriptAssembler is re-exported and reconstructs a human utterance", () => {
  const a = new TranscriptAssembler();
  const env = (i) => ({ type: "data_message", data: JSON.stringify(i) });
  a.feed(env({ type: "chat_message_delta", payload: { delta: { o: "add", v: { message: { id: "u1", author: { role: "user" }, content: { parts: [{ direction: "in", text: "" }] } } } } } }));
  a.feed(env({ type: "chat_message_delta", payload: { delta: { v: [{ p: "/message/content/parts/0/text", o: "append", v: "hi there" }] } } }));
  const got = a.feed(env({ type: "chat_message_delta", payload: { delta: { o: "replace", p: "/message/status", v: "finished_successfully" } } }));
  assert.deepEqual(got, ["hi there"]);
});

test("DEPRECATED buildSpeakWire still serializes (documented dead — not honored by server)", () => {
  const w = buildSpeakWire("anything");
  assert.equal(JSON.parse(w).type, "data_message");
});
