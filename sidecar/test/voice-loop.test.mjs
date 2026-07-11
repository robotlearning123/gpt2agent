// Full human-free voice→agent loop test (T2 integration). The whole loop runs with
// no human: a synthetic utterance → TTS → Realtime API STT → coding agent → reply,
// asserted. Opt-in (slow, costs $, needs the agent) so it stays out of `npm test`.
//
//   OPENAI_API_KEY=... RUN_FULL_LOOP=1 node --test test/voice-loop.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { transcribeText } from "../src/realtime-provider.mjs";

const KEY = process.env.OPENAI_API_KEY;
const AGENT = process.env.AGENT_CMD || "claude -p";
const RUN = process.env.RUN_FULL_LOOP;
const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

function runAgent(text) {
  return new Promise((res) => {
    const c = spawn(AGENT, { shell: true, stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    c.stdout.on("data", (d) => (out += d));
    c.on("error", () => res(null));
    c.on("close", () => res(out.trim()));
    c.stdin.write(text);
    c.stdin.end();
  });
}

test("human-free full loop: synthetic voice → STT → coding agent → reply",
  { skip: !(KEY && RUN) && "set OPENAI_API_KEY + RUN_FULL_LOOP=1 (slow, costs $, needs agent)" },
  async () => {
    const utterance = "Reply with only the digits and nothing else: what is two plus two";
    const transcript = await transcribeText(utterance);
    console.log("  transcript:", JSON.stringify(transcript));
    assert.ok(transcript && transcript.trim(), "no transcript from Realtime STT");

    const reply = await runAgent(transcript);
    console.log("  agent reply:", JSON.stringify((reply || "").slice(0, 200)));
    assert.ok(reply && reply.trim(), "no agent reply");
    assert.ok(norm(reply).includes("4"), `expected "4" in agent reply: ${reply}`);
    console.log("  ✅ full voice→agent loop completed with NO human (TTS+STT+agent)");
  });
