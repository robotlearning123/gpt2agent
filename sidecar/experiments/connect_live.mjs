// Live GPT-Live connect experiment (verification, not shipped src).
//
// werift owns the WebRTC peer; sdp_exchange.py does the authenticated SDP POST.
// Goal: complete ICE/DTLS against the real server, open the negotiated
// datachannel, send session.update, and log the real inbound event `type` names
// (the last piece of the datachannel event enum). No mic; audio is recvonly.
//
// Run:  node experiments/connect_live.mjs      (MODE=vp|vps|wm, default vp)

import { RTCPeerConnection } from "werift";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const MODE = process.env.MODE || "vp";
const PY = "/home/robot/workspace/47-chatgpt2agent/gpt2agent/.venv/bin/python";
const HELPER = fileURLToPath(new URL("./sdp_exchange.py", import.meta.url));

function exchange(offerSdp) {
  return new Promise((resolve, reject) => {
    const p = spawn(PY, [HELPER, MODE]);
    let out = "", err = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => { err += d; process.stderr.write(d); });
    p.on("close", (code) => (code === 0 ? resolve(out) : reject(new Error(`sdp_exchange exit ${code}: ${err}`))));
    p.stdin.write(offerSdp);
    p.stdin.end();
  });
}

const pc = new RTCPeerConnection({});
const events = [];
const seen = new Set();

const dc = pc.createDataChannel("", { negotiated: true, id: 0 });
dc.stateChanged.subscribe((s) => {
  console.log("[dc]", s);
  if (s === "open") {
    // Pin modalities, then drive the Mode B "speak" path directly: ask the model
    // to produce a spoken response (tests output without needing input audio).
    dc.send(JSON.stringify({ type: "session.update", session: { modalities: ["audio", "text"] } }));
    dc.send(JSON.stringify({ type: "response.create", response: { modalities: ["audio", "text"], instructions: "Say hello and count to three." } }));
    console.log("[sent] session.update + response.create");
  }
});
let msgCount = 0;
dc.onMessage.subscribe((msg) => {
  const raw = msg && msg.toString ? msg.toString() : String(msg);
  msgCount += 1;
  // Unwrap the consumer envelope: {type:"data_message", data:"<inner json>"}
  let inner = raw;
  try {
    const outer = JSON.parse(raw);
    if (outer && outer.type === "data_message" && typeof outer.data === "string") inner = outer.data;
    const ev = JSON.parse(inner);
    const t = ev && (ev.type || "(no-type)");
    if (!seen.has(t)) { seen.add(t); events.push(t); }
    console.log(`[msg ${msgCount}] inner.type=${t} :: ${inner.slice(0, 300)}`);
  } catch {
    console.log(`[msg ${msgCount}] raw :: ${raw.slice(0, 300)}`);
  }
});

pc.addTransceiver("audio", { direction: "recvonly" });
pc.connectionStateChange.subscribe((s) => console.log("[conn]", s));
pc.iceConnectionStateChange.subscribe((s) => console.log("[ice]", s));

const offer = await pc.createOffer();
await pc.setLocalDescription(offer);
// Non-trickle: wait for ICE gathering to finish (cap 6s).
await new Promise((r) => {
  if (pc.iceGatheringState === "complete") return r();
  const t = setInterval(() => { if (pc.iceGatheringState === "complete") { clearInterval(t); r(); } }, 200);
  setTimeout(() => { clearInterval(t); r(); }, 6000);
});
const offerSdp = pc.localDescription.sdp;
console.log(`[offer] mode=${MODE} gathered len=${offerSdp.length}`);

const answerSdp = await exchange(offerSdp);
console.log(`[answer] len=${answerSdp.length}`);
await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
console.log("[state] remote description set; awaiting connection…");

setTimeout(() => {
  console.log("\n=== RESULT ===");
  console.log("connectionState:", pc.connectionState);
  console.log("iceConnectionState:", pc.iceConnectionState);
  console.log("dataChannel:", dc.readyState);
  console.log("event types seen:", JSON.stringify(events));
  pc.close();
  process.exit(0);
}, 22000);
