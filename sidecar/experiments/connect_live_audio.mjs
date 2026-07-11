// Full audio round-trip experiment: send a spoken utterance to live GPT-Live and
// capture its transcription + response over the datachannel. (Verification, not
// shipped src.) werift owns WebRTC; ffmpeg encodes the mp3 to Opus RTP into a
// werift rtpSource UDP port; sdp_exchange.py does the authenticated SDP POST.
//
// Run:  node experiments/connect_live_audio.mjs /path/to/utter.mp3

import { RTCPeerConnection, MediaStreamTrackFactory } from "werift";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const MP3 = process.argv[2] || "/tmp/utter.mp3";
const MODE = process.env.MODE || "vp";
const PORT = 5006;
const PY = "/home/robot/workspace/47-chatgpt2agent/gpt2agent/.venv/bin/python";
const HELPER = fileURLToPath(new URL("./sdp_exchange.py", import.meta.url));

function exchange(offerSdp) {
  return new Promise((resolve, reject) => {
    const p = spawn(PY, [HELPER, MODE]);
    let out = "", err = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => { err += d; process.stderr.write(d); });
    p.on("close", (c) => (c === 0 ? resolve(out) : reject(new Error(`sdp exit ${c}: ${err}`))));
    p.stdin.write(offerSdp); p.stdin.end();
  });
}

// werift track fed by RTP arriving on a UDP port (ffmpeg -> here -> WebRTC).
// rtpSource returns [track, port, dispose] — an ARRAY, not an object.
let rtpCount = 0;
const [track] = await MediaStreamTrackFactory.rtpSource({
  kind: "audio",
  port: PORT,
  cb: (msg) => { rtpCount += 1; return msg; },
});

const pc = new RTCPeerConnection({});
const seen = new Set();
let audioStarted = false;
function startAudio(OPUS_PT) {
  if (audioStarted) return;
  audioStarted = true;
  // No -ssrc: werift's RTCRtpSender re-stamps SSRC to its negotiated value, and
  // werift's SSRC often exceeds ffmpeg's signed-int32 -ssrc range anyway.
  const ff = spawn("ffmpeg", [
    "-hide_banner", "-loglevel", "error", "-re", "-i", MP3,
    "-c:a", "libopus", "-ar", "48000", "-ac", "1", "-b:a", "24k",
    "-payload_type", OPUS_PT, "-f", "rtp", `rtp://127.0.0.1:${PORT}`,
  ]);
  console.log(`[audio] ffmpeg PT=${OPUS_PT}`);
  ff.stderr.on("data", (d) => process.stderr.write("[ffmpeg] " + d));
  ff.on("close", (c) => console.log("[ffmpeg] done", c));
  console.log("[audio] streaming utterance NOW (on dc open)…");
}

const dc = pc.createDataChannel("", { negotiated: true, id: 0 });
// Outbound protocol captured from the real authenticated web client (2026-07-11,
// CDP observation): messages are wrapped as {type:"data_message", data:"<inner>"},
// and to HOLD the session the client sends a `track_state` (microphone live) on
// open, then periodic `client_metrics` keepalives. My Node client sent neither,
// which is why the server closed it ~1s after "listening".
function sendWrapped(inner) {
  try { dc.send(JSON.stringify({ type: "data_message", data: JSON.stringify(inner) })); }
  catch (e) { console.log("[send-err]", e.message); }
}
let keepalive = null;
dc.stateChanged.subscribe((s) => {
  console.log("[dc]", s);
  if (s === "open") {
    // 1) declare the mic track live (the init that holds the session)
    sendWrapped({ type: "track_state", payload: { type: "track_state", track_id: "microphone", media_type: "audio", media_source: "microphone", state: "live" } });
    console.log("[sent] track_state microphone=live");
    // 2) client_metrics: the real client sends these frequently (~5-7/sec) and
    //    the session dropped before our old 1s interval even fired. Fire now + fast.
    const metric = () => sendWrapped({ type: "client_metrics", payload: { type: "client_metrics", service_rtt_ms: 20, output_audio_first_chunk_received_ts: null, output_audio_playout_start_ts: null, output_audio_buffer_depth_ms: null, output_audio_bytes_received: 0, output_audio_packets_received: 0, output_audio_packets_lost: 0, output_audio_silent_gap_ms: null } });
    metric();
    keepalive = setInterval(metric, 250);
    startAudio(globalThis.__OPUS_PT || "111");
  }
  if (s === "closed" && keepalive) { clearInterval(keepalive); keepalive = null; }
});
dc.onMessage.subscribe((msg) => {
  const raw = msg && msg.toString ? msg.toString() : String(msg);
  let inner = raw;
  try {
    const outer = JSON.parse(raw);
    if (outer && outer.type === "data_message" && typeof outer.data === "string") inner = outer.data;
    const ev = JSON.parse(inner);
    const t = ev.type || ev?.payload?.type || "(no-type)";
    if (!seen.has(t)) { seen.add(t); console.log("[event-type]", t); }
    // Surface transcription / response text as it arrives.
    const s = JSON.stringify(ev);
    if (/transcript|response|text|delta|new_state/i.test(s)) console.log("[msg]", s.slice(0, 260));
  } catch { console.log("[dc-raw]", raw.slice(0, 200)); }
});

// Pass the TRACK as the first arg (werift: addTransceiver(trackOrKind, opts)) so
// it is actually wired to the sender — a kind string here would send nothing.
pc.addTransceiver(track, { direction: "sendrecv" });
pc.connectionStateChange.subscribe((s) => console.log("[conn]", s));

const offer = await pc.createOffer();
await pc.setLocalDescription(offer);
await new Promise((r) => { const t = setInterval(() => { if (pc.iceGatheringState === "complete") { clearInterval(t); r(); } }, 200); setTimeout(() => { clearInterval(t); r(); }, 6000); });
const offerSdp = pc.localDescription.sdp;
const ptMatch = offerSdp.match(/a=rtpmap:(\d+)\s+opus/i);
const OPUS_PT = ptMatch ? ptMatch[1] : "111";
globalThis.__OPUS_PT = OPUS_PT;
const ssrcMatch = offerSdp.match(/a=ssrc:(\d+)/);
globalThis.__SSRC = ssrcMatch ? ssrcMatch[1] : "1";
console.log(`[offer] opus PT=${OPUS_PT} ssrc=${globalThis.__SSRC} len=${offerSdp.length}`);

const answerSdp = await exchange(offerSdp);
const aPt = answerSdp.match(/a=rtpmap:(\d+)\s+opus/i);
console.log(`[answer] opus PT=${aPt ? aPt[1] : "?"}`);
await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
console.log("[state] remote set; audio starts on dc open");
// Fallback: if dc already open, start now.
if (dc.readyState === "open") startAudio(OPUS_PT);

setInterval(() => console.log(`[rtp] received ${rtpCount} packets from ffmpeg`), 3000);
setTimeout(() => {
  console.log("\n=== RESULT ===  conn:", pc.connectionState, " dc:", dc.readyState, " rtpPkts:", rtpCount, " events:", JSON.stringify([...seen]));
  pc.close();
  process.exit(0);
}, 30000);
