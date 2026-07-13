import { test } from "node:test";
import assert from "node:assert/strict";
import { ModeBExport, ExportState, redactForAgent } from "../src/export.mjs";

// --- helpers: build REAL consumer-protocol messages (chat_message_delta) ---
const env = (inner) => JSON.stringify({ type: "data_message", data: JSON.stringify(inner) });
const cmd = (delta) => env({ type: "chat_message_delta", payload: { delta } });
const addMsg = (id, direction) =>
  cmd({
    o: "add",
    v: {
      message: {
        id,
        author: { role: direction === "in" ? "user" : "assistant" },
        content: { parts: [{ content_type: "audio_transcription", direction, text: "" }] },
      },
    },
  });
const appendText = (chunk) => cmd({ v: [{ o: "append", p: "/message/content/parts/0/text", v: chunk }] });
const finish = () => cmd({ o: "replace", p: "/message/status", v: "finished_successfully" });

async function feedUtterance(plane, id, direction, chunks) {
  await plane.ingest(addMsg(id, direction));
  for (const c of chunks) await plane.ingest(appendText(c));
  return plane.ingest(finish());
}

test("ingest extracts a completed HUMAN utterance and calls the agent hook", async () => {
  const turns = [];
  const plane = new ModeBExport({
    onAgentTurn: async (t) => {
      turns.push(t);
      return `echo:${t}`;
    },
  });
  plane.setState(ExportState.LIVE);

  const result = await feedUtterance(plane, "m1", "in", ["hello ", "agent"]);

  assert.equal(result.humanText, "hello agent");
  assert.equal(result.agentReply, "echo:hello agent");
  assert.deepEqual(turns, ["hello agent"]);

  const txs = plane.getTranscripts();
  assert.equal(txs.length, 2);
  assert.equal(txs[0].role, "human");
  assert.equal(txs[0].text, "hello agent");
  assert.equal(txs[1].role, "agent");
  assert.equal(txs[1].text, "echo:hello agent");
});

test("model speech (direction:out) is NOT emitted as a human turn", async () => {
  const turns = [];
  const plane = new ModeBExport({
    onAgentTurn: async (t) => {
      turns.push(t);
      return "x";
    },
  });
  const result = await feedUtterance(plane, "m2", "out", ["I am the ", "assistant"]);
  assert.equal(result.humanText, null);
  assert.equal(turns.length, 0);
  assert.equal(plane.getTranscripts().length, 0);
});

test("filler / acks are dropped (isActionable)", async () => {
  const turns = [];
  const plane = new ModeBExport({
    onAgentTurn: async (t) => {
      turns.push(t);
      return "r";
    },
  });
  const result = await feedUtterance(plane, "m3", "in", ["ok"]);
  assert.equal(result.humanText, null);
  assert.equal(turns.length, 0);
});

test("handleUtterance (extension path) shares the filter/buffer/agent pipeline", async () => {
  const turns = [];
  const plane = new ModeBExport({
    onAgentTurn: async (t) => {
      turns.push(t);
      return `r:${t}`;
    },
  });
  // Filler is dropped by the SAME isActionable used on the datachannel path.
  const dropped = await plane.handleUtterance("ok");
  assert.equal(dropped.humanText, null);
  assert.equal(turns.length, 0);
  assert.equal(plane.getTranscripts().length, 0);

  // A real utterance routes to the agent and is buffered (observable via control plane).
  const r = await plane.handleUtterance("  what files changed  ");
  assert.equal(r.humanText, "what files changed");
  assert.equal(r.agentReply, "r:what files changed");
  const txs = plane.getTranscripts();
  assert.equal(txs.length, 2);
  assert.equal(txs[0].role, "human");
  assert.equal(txs[1].role, "agent");
});

test("no agent→Live speak/inject API exists (write channel removed)", () => {
  const plane = new ModeBExport();
  for (const m of ["buildSpeakWire", "queueSpeak", "drainSpeakQueue", "enqueueWire", "removeSpeakWire"]) {
    assert.equal(typeof plane[m], "undefined", `${m} must not exist`);
  }
  const st = plane.status();
  assert.equal(st.boundary.speakInjection, "unsupported (server drops it)");
  assert.equal("speakQueueLength" in st, false);
});

test("status and redaction never expose tokens or audio", () => {
  const plane = new ModeBExport();
  plane.record("human", "hi there");
  const st = plane.status();
  assert.equal(st.boundary.audioCrossesBoundary, false);
  assert.equal(st.boundary.direction, "human-to-agent");
  assert.equal(st.boundary.turnstileBypass, "out-of-scope");

  const redacted = redactForAgent({
    token: "secret-tok",
    items: [{ access_token: "abc" }, { note: "keep" }],
    nested: { authorization: "Bearer x", text: "safe" },
    lastError: "auth failed: Bearer aaaaaaaa.bbbbbbbb.cccccccc",
  });
  assert.equal(redacted.token, "[redacted]");
  assert.equal(redacted.items[0].access_token, "[redacted]");
  assert.equal(redacted.items[1].note, "keep");
  assert.equal(redacted.nested.authorization, "[redacted]");
  assert.equal(redacted.nested.text, "safe");
  assert.match(redacted.lastError, /\[redacted\]/);
  assert.equal(/[a-z]{8}\.[a-z]{8}\.[a-z]{8}/.test(JSON.stringify(redacted)), false);
});

test("agent hook errors are captured, not thrown out of ingest", async () => {
  const plane = new ModeBExport({
    onAgentTurn: async () => {
      throw new Error("agent down");
    },
  });
  const result = await feedUtterance(plane, "m4", "in", ["please ", "help me"]);
  assert.equal(result.humanText, "please help me");
  assert.equal(result.agentReply, null);
  assert.equal(plane.status().lastError, "agent down");
});

test("close moves to CLOSED", () => {
  const plane = new ModeBExport();
  plane.close();
  assert.equal(plane.state, ExportState.CLOSED);
});
