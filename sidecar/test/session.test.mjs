import { test } from "node:test";
import assert from "node:assert/strict";
import { VoiceSession, State } from "../src/session.mjs";
import { CLIENT_EVENTS, DATA_MESSAGE } from "../src/events.mjs";

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

test("speak requires LIVE state and sends envelope-wrapped response.create", () => {
  const peer = mockPeer();
  const session = new VoiceSession({
    auth: { token: "t" },
    createPeer: () => peer.pc,
    getMicTrack: async () => ({}),
    onUserSaid: () => {},
  });
  assert.throws(() => session.speak("hi"), /cannot speak while idle/);

  // Force live + attach dc as connect() would.
  session._dc = peer.dc;
  session.state = State.LIVE;
  const wire = session.speak("agent reply");
  assert.equal(peer.sends.length, 1);
  const outer = JSON.parse(peer.sends[0]);
  assert.equal(outer.type, DATA_MESSAGE);
  const inner = JSON.parse(outer.data);
  assert.equal(inner.type, CLIENT_EVENTS.RESPONSE_CREATE);
  assert.equal(inner.response.instructions, "agent reply");
  assert.equal(typeof wire, "string");
  const txs = session.getTranscripts();
  assert.equal(txs.some((t) => t.role === "agent" && t.text === "agent reply"), true);
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

test("inbound transcript routes to onUserSaid and can speak reply", async () => {
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

  const msg = {
    type: "conversation.item.input_audio_transcription.completed",
    transcript: "turn one",
  };
  const result = await session.exportPlane.handleInbound(msg);
  assert.deepEqual(heard, ["turn one"]);
  assert.equal(result.agentReply, "got:turn one");
  // Deliver speak wires as the session message handler would.
  for (const w of result.speakWires) peer.dc.send(w);
  assert.equal(peer.sends.length, 1);
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
