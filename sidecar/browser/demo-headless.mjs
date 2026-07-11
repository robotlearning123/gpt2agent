// Autonomous GPT-Live round-trip demo — the browser owns the WebRTC media (which
// werift couldn't match on SRTP egress); Node does the SDP POST via curl_cffi
// (a plain headless-Chrome fetch gets 403 — wrong Origin + bot detection).
//
//   node browser/demo-headless.mjs --audio /path/q.wav
//
// No ChatGPT web login: the SDP exchange uses the account bearer from
// ~/.codex/auth.json (via sdp_exchange.py). The "mic" is the WAV. Success = a
// datachannel that stays open past `listening` with transcription/response events.

import puppeteer from "puppeteer-core";
import http from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const argv = process.argv;
const AUDIO = argv[argv.indexOf("--audio") + 1] || "./q.wav";
const CHROME = process.env.CHROME_BIN || (process.platform === "darwin"
  ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  : "/usr/bin/google-chrome");
const PY = process.env.SDP_PY || "/home/robot/workspace/47-chatgpt2agent/gpt2agent/.venv/bin/python";
const HELPER = fileURLToPath(new URL("../experiments/sdp_exchange.py", import.meta.url));

function exchange(offerSdp) {
  return new Promise((resolve, reject) => {
    const p = spawn(PY, [HELPER, "vp"]);
    let out = "", err = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => { err += d; process.stderr.write(d); });
    p.on("close", (c) => (c === 0 ? resolve(out) : reject(new Error(`sdp exit ${c}: ${err}`))));
    p.stdin.write(offerSdp); p.stdin.end();
  });
}

const server = http.createServer((_, res) => { res.writeHead(200, { "Content-Type": "text/html" }); res.end("<!doctype html><title>gptlive</title>"); });
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const PORT = server.address().port;

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: "new",
  userDataDir: `/tmp/gptlive-chrome-${PORT}`,
  args: [
    "--no-sandbox",
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    `--use-file-for-fake-audio-capture=${AUDIO}%noloop`,
    "--autoplay-policy=no-user-gesture-required",
  ],
});

try {
  const page = (await browser.pages())[0] || (await browser.newPage());
  page.on("console", (m) => { const t = m.text(); if (/\[client\]/.test(t)) console.log(t); });
  await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: "domcontentloaded" });

  // 1) Browser builds the peer + offer (its own WebRTC media stack).
  const offerSdp = await page.evaluate(async () => {
    const log = (...a) => console.log("[client]", ...a);
    const r = (window.__r = { events: [], transcripts: [], state: [], error: null });
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    log("fake mic:", mic.getAudioTracks()[0]?.label);
    const pc = (window.__pc = new RTCPeerConnection({}));
    pc.addTrack(mic.getAudioTracks()[0], mic);
    pc.onconnectionstatechange = () => { r.state.push(pc.connectionState); log("conn:", pc.connectionState); };
    const dc = (window.__dc = pc.createDataChannel("", { negotiated: true, id: 0 }));
    const wrap = (inner) => { try { dc.send(JSON.stringify({ type: "data_message", data: JSON.stringify(inner) })); } catch {} };
    dc.onopen = () => {
      log("datachannel open");
      wrap({ type: "track_state", payload: { type: "track_state", track_id: "microphone", media_type: "audio", media_source: "microphone", state: "live" } });
      setInterval(() => wrap({ type: "client_metrics", payload: { type: "client_metrics", service_rtt_ms: 20, output_audio_bytes_received: 0, output_audio_packets_received: 0, output_audio_packets_lost: 0 } }), 250);
    };
    dc.onclose = () => log("datachannel CLOSED");
    dc.onmessage = (ev) => {
      try {
        let inner = String(ev.data); const o = JSON.parse(inner);
        if (o && o.type === "data_message" && typeof o.data === "string") inner = o.data;
        const e = JSON.parse(inner); const t = (e && (e.type || (e.payload && e.payload.type))) || "?";
        r.events.push(t);
        const s = JSON.stringify(e);
        if (/transcript|response|new_state/i.test(s)) { r.transcripts.push(s.slice(0, 400)); log("EVENT", t, s.slice(0, 220)); }
      } catch {}
    };
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await new Promise((res) => { if (pc.iceGatheringState === "complete") return res(); const iv = setInterval(() => { if (pc.iceGatheringState === "complete") { clearInterval(iv); res(); } }, 200); setTimeout(() => { clearInterval(iv); res(); }, 5000); });
    return pc.localDescription.sdp;
  });
  console.log("[node] offer gathered; POSTing SDP via curl_cffi…");

  // 2) Node does the authenticated SDP POST (browser fetch gets 403).
  const answerSdp = await exchange(offerSdp);
  console.log("[node] got answer, len", answerSdp.length);

  // 3) Browser sets the answer → its WebRTC connects and sends the WAV over SRTP.
  await page.evaluate(async (answer) => { await window.__pc.setRemoteDescription({ type: "answer", sdp: answer }); console.log("[client] remote set"); }, answerSdp);

  await new Promise((res) => setTimeout(res, 22000));
  const r = await page.evaluate(() => window.__r);
  console.log("\n=== RESULT ===");
  console.log("conn states:", JSON.stringify(r.state), "| error:", r.error);
  console.log("event types:", JSON.stringify([...new Set(r.events)]));
  console.log("transcription/response events:");
  r.transcripts.slice(0, 25).forEach((t) => console.log("  " + t));
} finally {
  await browser.close();
  server.close();
}
