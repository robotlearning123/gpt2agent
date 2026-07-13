import { test } from "node:test";
import assert from "node:assert/strict";
import { VoiceSession, State } from "../src/session.mjs";

function mockPeer() {
  const sends = [];
  const handlers = {};
  const dc = {
    readyState: "open",
    send: (data) => sends.push(data),
    close: () => {},
    addEventListener: (ev, fn) => {
      handlers[ev] = fn;
    },
  };
  return {
    sends,
    handlers,
    pc: {
      addTrack: () => {},
      createDataChannel: () => dc,
      createOffer: async () => ({ sdp: "v=0\r\noffer", type: "offer" }),
      setLocalDescription: async () => {},
      setRemoteDescription: async () => {},
      close: () => {},
    },
    dc,
  };
}

// Real consumer-protocol human utterance (chat_message_delta, direction:"in").
const env = (inner) => JSON.stringify({ type: "data_message", data: JSON.stringify(inner) });
const cmd = (delta) => env({ type: "chat_message_delta", payload: { delta } });
const humanUtterance = (id, text) => [
  cmd({ o: "add", v: { message: { id, content: { parts: [{ direction: "in", text: "" }] } } } }),
  cmd({ v: [{ o: "append", p: "/message/content/parts/0/text", v: text }] }),
  cmd({ o: "replace", p: "/message/status", v: "finished_successfully" }),
];

test("no agent→Live speak method exists (injection is unsupported)", () => {
  const peer = mockPeer();
  const session = new VoiceSession({
    auth: { token: "t" },
    createPeer: () => peer.pc,
    getMicTrack: async () => ({}),
    onUserSaid: () => {},
  });
  assert.equal(typeof session.speak, "undefined");
});

test("status is agent-safe (no auth token fields)", () => {
  const peer = mockPeer();
  const session = new VoiceSession({
    auth: { token: "super-secret-token-value" },
    createPeer: () => peer.pc,
    getMicTrack: async () => ({}),
    onUserSaid: () => {},
  });
  const st = session.status();
  assert.equal(st.sessionState, State.IDLE);
  const dumped = JSON.stringify(st);
  assert.equal(dumped.includes("super-secret-token-value"), false);
  assert.equal(st.boundary.audioCrossesBoundary, false);
});

test("inbound human transcript routes to onUserSaid; reply buffered out-of-band, not sent to Live", async () => {
  const peer = mockPeer();
  const heard = [];
  const session = new VoiceSession({
    auth: { token: "t" },
    createPeer: () => peer.pc,
    getMicTrack: async () => ({}),
    onUserSaid: async (text) => {
      heard.push(text);
      return `got:${text}`;
    },
  });
  session._dc = peer.dc;
  session.state = State.LIVE;
  session.exportPlane.setState("live");

  for (const m of humanUtterance("s1", "what is the plan")) {
    await session.exportPlane.ingest(m);
  }
  assert.deepEqual(heard, ["what is the plan"]);
  const txs = session.getTranscripts();
  assert.equal(txs.some((t) => t.role === "agent" && t.text === "got:what is the plan"), true);
  // The reply is NOT pushed back to the Live datachannel (injection is dropped).
  assert.equal(peer.sends.length, 0);
});

test("close transitions to CLOSED", () => {
  const peer = mockPeer();
  const session = new VoiceSession({
    auth: { token: "t" },
    createPeer: () => peer.pc,
    getMicTrack: async () => ({}),
    onUserSaid: () => {},
  });
  session._pc = peer.pc;
  session._dc = peer.dc;
  session.close();
  assert.equal(session.state, State.CLOSED);
});
