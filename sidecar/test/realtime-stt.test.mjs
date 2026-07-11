// Human-free voice STT test (T2). Proves the voice→text leg runs with NO human:
// a synthetic utterance is spoken by TTS, transcribed by the Realtime API, and the
// transcript is asserted. This is the test double for consumer GPT-Live (which
// rejects synthetic audio and so can't be driven human-free at the STT layer).
//
// Requires OPENAI_API_KEY (uses Realtime API + TTS). Network + small cost. Slow.
import test from "node:test";
import assert from "node:assert/strict";
import { transcribeText } from "../src/realtime-provider.mjs";

const KEY = process.env.OPENAI_API_KEY;
const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9 ]/g, "").replace(/\s+/g, " ").trim();

test("human-free STT: TTS → Realtime API → transcript (no mic, no human)", { skip: !KEY && "set OPENAI_API_KEY" }, async () => {
  const phrase = "List the Python files in this project";
  const transcript = await transcribeText(phrase);
  console.log("  phrase    :", phrase);
  console.log("  transcript:", transcript);
  assert.ok(transcript && transcript.trim(), "no transcript returned");
  assert.ok(norm(transcript).includes("list the python files"), `transcript mismatch: ${transcript}`);
});

test("human-free STT handles code-shaped terms", { skip: !KEY && "set OPENAI_API_KEY" }, async () => {
  const phrase = "refactor init dot py to be async";
  const transcript = await transcribeText(phrase);
  console.log("  transcript:", transcript);
  assert.ok(norm(transcript).includes("init"), `expected 'init' in: ${transcript}`);
});
