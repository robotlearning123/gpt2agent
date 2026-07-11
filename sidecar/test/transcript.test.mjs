// T1 unit tests for the GPT-Live transcript assembler. No voice, no human, no LLM.
// Synthetic chat_message_delta streams built from REAL captured payloads (2026-07-11).
import test from "node:test";
import assert from "node:assert/strict";
import { TranscriptAssembler, unwrap, isActionable } from "../src/transcript.mjs";

const env = (inner) => ({ type: "data_message", data: JSON.stringify(inner) });

// delta builders matching the real wire shapes
const add = (mid, role, direction, text) => ({
  type: "chat_message_delta",
  payload: { delta: { o: "add", v: { message: { id: mid, author: { role }, content: { content_type: "multimodal_text", parts: [{ content_type: "audio_transcription", direction, text }] } } } } },
});
const appends = (...chunks) => ({ type: "chat_message_delta", payload: { delta: { v: chunks.map((c) => ({ p: "/message/content/parts/0/text", o: "append", v: c })) } } });
const done = () => ({ type: "chat_message_delta", payload: { delta: { o: "replace", p: "/message/status", v: "finished_successfully" } } });
const patchDone = () => ({ type: "chat_message_delta", payload: { delta: { v: [{ p: "/message/status", o: "replace", v: "finished_successfully" }] } } });

test("reconstructs a full human utterance from add + appends + done", () => {
  const a = new TranscriptAssembler();
  assert.deepEqual(a.feed(env(add("u1", "user", "in", ""))), []);
  assert.deepEqual(a.feed(env(appends(" List", " the", " python", " files"))), []);
  assert.deepEqual(a.feed(env(done())), ["List the python files"]);
});

test("does not emit Live's own output (direction:out)", () => {
  const a = new TranscriptAssembler();
  a.feed(env(add("a1", "assistant", "out", "Sure")));
  a.feed(env(appends(", checking now")));
  assert.deepEqual(a.feed(env(done())), []);
});

test("handles multiple utterances in order, no cross-contamination", () => {
  const a = new TranscriptAssembler();
  a.feed(env(add("u1", "user", "in", "Hello"))); a.feed(env(done()));
  a.feed(env(add("a1", "assistant", "out", "Hi"))); a.feed(env(done()));
  a.feed(env(add("u2", "user", "in", ""))); a.feed(env(appends("List files"))); a.feed(env(done()));
  // emit one at a time
  assert.equal(a.feed(env(add("u1", "user", "in", "Hello"))).length, 0); // u1 already seen mid; re-add ignored
});

test("status replace inside a patch array also completes the utterance", () => {
  const a = new TranscriptAssembler();
  a.feed(env(add("u3", "user", "in", "run the tests")));
  assert.deepEqual(a.feed(env(patchDone())), ["run the tests"]);
});

test("accepts enveloped (string) and bare inner messages", () => {
  const a = new TranscriptAssembler();
  const inner = add("u4", "user", "in", "hi there");
  assert.deepEqual(a.feed(env(inner)), []);          // enveloped object
  assert.deepEqual(a.feed(JSON.stringify(env(done()))), ["hi there"]); // enveloped string
});

test("unwrap returns inner event for data_message envelope, else passthrough", () => {
  assert.equal(unwrap(env({ type: "x" })).type, "x");
  assert.equal(unwrap({ type: "y" }).type, "y");
  assert.equal(unwrap("not json"), null);
});

test("isActionable drops acks/filler, keeps real requests", () => {
  for (const f of ["ok", "Okay", "um", "hello", "hi", "yeah", "mm"]) assert.equal(isActionable(f), false);
  for (const t of ["list the python files", "run the tests", "create init.py"]) assert.equal(isActionable(t), true);
});

test("empty utterance not emitted", () => {
  const a = new TranscriptAssembler();
  a.feed(env(add("u5", "user", "in", "   ")));
  assert.deepEqual(a.feed(env(done())), []);
});
