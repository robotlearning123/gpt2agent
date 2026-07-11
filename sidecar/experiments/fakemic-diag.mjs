// Diagnose why synthetic mic audio (Chrome --use-fake-device-for-media-stream
// + --use-file-for-fake-audio-capture) doesn't get transcribed by GPT-Live.
// Hooks EVERY RTCPeerConnection + getUserMedia + enumerateDevices, polls getStats
// across all peers for outbound audio packets, and logs any transcription.
import puppeteer from "puppeteer-core";
import { appendFileSync, writeFileSync } from "node:fs";
const CDP = "http://127.0.0.1:9333";
writeFileSync("/tmp/fakemic-diag.log", "");
const log = (s) => { const l = `[${new Date().toISOString().slice(11, 23)}] ${s}`; console.log(l); appendFileSync("/tmp/fakemic-diag.log", l + "\n"); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function pageHook() {
  window.__pcs = [];
  window.__gum = 0;
  const _RTC = window.RTCPeerConnection;
  if (!_RTC || _RTC.__d) return;
  function W(cfg) {
    const pc = new _RTC(cfg);
    window.__pcs.push(pc);
    window.__on({ sys: "pc", ice: (cfg && cfg.iceServers && cfg.iceServers.length) || 0 });
    const _cdc = pc.createDataChannel.bind(pc);
    pc.createDataChannel = function (label, opts) { const dc = _cdc(label, opts); window.__on({ sys: "dc" }); dc.addEventListener("open", () => window.__on({ sys: "dc_open" })); dc.addEventListener("message", (ev) => { try { let o = JSON.parse(String(ev.data)); let inner = o && o.type === "data_message" && typeof o.data === "string" ? JSON.parse(o.data) : o; if ((inner && inner.type) === "chat_message_delta") { const r = JSON.stringify(inner); if (/"direction":"in"/.test(r)) { const m = r.match(/"text":"((?:[^"\\]|\\.)*)"[^}]{0,30}"direction":"in"/); window.__on({ transcript: m ? m[1] : "" }); } } } catch {} }); return dc; };
    // also watch tracks added
    const _add = pc.addTrack.bind(pc);
    pc.addTrack = function (t, ...rest) { window.__on({ sys: "addTrack", kind: t && t.kind, ready: t && t.readyState }); return _add(t, ...rest); };
    return pc;
  }
  W.prototype = _RTC.prototype;
  try { W.generateCertificate = _RTC.generateCertificate && _RTC.generateCertificate.bind(_RTC); } catch {}
  _RTC.__d = true; window.RTCPeerConnection = W;
  const _gum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = function (c) { window.__gum++; window.__on({ sys: "getUserMedia", audio: !!(c && c.audio) }); return _gum(c); };
}

const browser = await puppeteer.connect({ browserURL: CDP, defaultViewport: null });
const page = await browser.newPage();
await page.evaluateOnNewDocument(pageHook);
await page.exposeFunction("__on", (d) => {
  if (d.sys) log(`[*] ${d.sys} ${d.ice != null ? "iceServers=" + d.ice : ""} ${d.kind ? "kind=" + d.kind + " ready=" + d.ready : ""} ${d.audio != null ? "audio=" + d.audio : ""}`);
  if (d.transcript != null && d.transcript.trim()) log(`[TRANSCRIPT in] ${d.transcript}`);
});
log("loading chatgpt.com + auto-starting voice…");
await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
await page.bringToFront();
for (let i = 0; i < 8; i++) {
  await page.evaluate(() => { const b = [...document.querySelectorAll("button")].find((x) => /voice|speech|composer-speech/i.test(`${x.getAttribute("aria-label") || ""} ${x.title || ""} ${x.getAttribute("data-testid") || ""}`)); if (b) b.click(); const w = /start voice|continue|got it|begin/i; [...document.querySelectorAll("button")].forEach((b) => { if (w.test(`${b.getAttribute("aria-label") || ""} ${(b.textContent || "").trim()}`)) b.click(); }); });
  await sleep(2500);
}
log("voice started; polling getStats on all PCs for 30s…");
for (let i = 0; i < 15; i++) {
  const stats = await page.evaluate(async () => {
    const out = [];
    for (const pc of window.__pcs || []) {
      try {
        const s = await pc.getStats();
        let p = null, b = null;
        s.forEach((r) => { if (r.type === "outbound-rtp" && r.kind === "audio") { p = r.packetsSent; b = r.bytesSent; } });
        out.push({ pc: pc.__id || "?", audioPackets: p, audioBytes: b });
      } catch (e) { out.push({ err: String(e.message) }); }
    }
    return { n: (window.__pcs || []).length, gum: window.__gum, out };
  }).catch(() => null);
  log(`[getStats] PCs=${stats?.n} getUserMedia_calls=${stats?.gum} → ${JSON.stringify(stats?.out)}`);
  await sleep(2000);
}
await browser.disconnect();
