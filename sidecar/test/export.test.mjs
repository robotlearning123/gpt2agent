import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ModeBExport,
  ExportState,
  redactForAgent,
  buildSpeakWire,
} from "../src/export.mjs";
import {
  SERVER_EVENTS,
  CLIENT_EVENTS,
  DATA_MESSAGE,
  wrapDataMessage,
} from "../src/events.mjs";

test("ModeBExport extracts human transcript and calls agent hook", async () => {
  const turns = [];
  const plane = new ModeBExport({
    onAgentTurn: async (text) => {
      turns.push(text);
      return `echo:${text}`;
    },
  });
  plane.setState(ExportState.LIVE);

  const raw = wrapDataMessage({
    type: SERVER_EVENTS.INPUT_TRANSCRIPT_DONE,
    transcript: "hello agent",
  });
  const result = await plane.handleInbound(raw);

  assert.equal(result.humanText, "hello agent");
  assert.equal(result.agentReply, "echo:hello agent");
  assert.equal(result.speakWires.length, 1);
  assert.deepEqual(turns, ["hello agent"]);

  const wire = JSON.parse(result.speakWires[0]);
  assert.equal(wire.type, DATA_MESSAGE);
  const inner = JSON.parse(wire.data);
  assert.equal(inner.type, CLIENT_EVENTS.RESPONSE_CREATE);
  assert.equal(inner.response.instructions, "echo:hello agent");

  const txs = plane.getTranscripts();
  assert.equal(txs.length, 2);
  assert.equal(txs[0].role, "human");
  assert.equal(txs[0].text, "hello agent");
  assert.equal(txs[1].role, "agent");
  assert.equal(txs[1].text, "echo:hello agent");
});

test("ModeBExport speak path rejects empty text and queues valid speak", () => {
  const plane = new ModeBExport();
  assert.throws(() => plane.buildSpeakWire(""), TypeError);
  assert.throws(() => plane.queueSpeak("  "), TypeError);
  const wire = plane.queueSpeak("spoken reply");
  assert.equal(typeof wire, "string");
  assert.equal(plane.drainSpeakQueue().length, 1);
  assert.equal(plane.drainSpeakQueue().length, 0);
});

test("status and transcripts never expose tokens or audio fields", () => {
  const plane = new ModeBExport();
  plane.record("human", "hi");
  const st = plane.status();
  assert.equal(st.audioCrossesBoundary ?? st.boundary.audioCrossesBoundary, false);
  assert.equal(st.boundary.secretsCrossBoundary, false);
  assert.equal(st.boundary.turnstileBypass, "out-of-scope");
  assert.equal("token" in st, false);
  assert.equal("audio" in st, false);
  const json = JSON.stringify(st);
  assert.equal(/Bearer |eyJ[a-zA-Z0-9]/.test(json), false);

  const redacted = redactForAgent({
    token: "secret-tok",
    access_token: "abc",
    audio_bytes: Buffer.from("pcm").toString("base64"),
    ok: true,
    nested: { authorization: "Bearer x", text: "safe" },
  });
  assert.equal(redacted.token, "[redacted]");
  assert.equal(redacted.access_token, "[redacted]");
  assert.equal(redacted.audio_bytes, "[redacted]");
  assert.equal(redacted.nested.authorization, "[redacted]");
  assert.equal(redacted.nested.text, "safe");
  assert.equal(redacted.ok, true);
});

test("buildSpeakWire from export module matches events contract", () => {
  const wire = buildSpeakWire("agent says hi");
  const outer = JSON.parse(wire);
  assert.equal(outer.type, DATA_MESSAGE);
  assert.equal(JSON.parse(outer.data).response.instructions, "agent says hi");
});

test("agent hook errors are captured without throwing out of handleInbound", async () => {
  const plane = new ModeBExport({
    onAgentTurn: async () => {
      throw new Error("agent down");
    },
  });
  const result = await plane.handleInbound({
    type: SERVER_EVENTS.INPUT_TRANSCRIPT_DONE,
    transcript: "ping",
  });
  assert.equal(result.humanText, "ping");
  assert.equal(result.agentReply, null);
  assert.equal(plane.status().lastError, "agent down");
});

test("close moves to CLOSED and clears speak queue", () => {
  const plane = new ModeBExport();
  plane.queueSpeak("bye");
  plane.close();
  assert.equal(plane.state, ExportState.CLOSED);
  assert.equal(plane.drainSpeakQueue().length, 0);
});
