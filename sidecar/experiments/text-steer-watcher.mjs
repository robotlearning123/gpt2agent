// Cooperative text-steering watcher. Opens a fresh chatgpt tab with the dc hook,
// brings it forward, and WAITS for you to click "Start Voice". Then it captures
// the conversation_id and records every direction:"out" (Live's spoken text) so we
// can test whether a backend POST to that conversation makes Live speak it.
import puppeteer from "puppeteer-core";
import { appendFileSync, writeFileSync } from "node:fs";

const CDP = "http://127.0.0.1:9333";
const FULL = "/tmp/gptlive-full.jsonl";
const OUT_TXT = "/tmp/voice-out.txt";
const CONV_ID_FILE = "/tmp/voice-conv-id";
writeFileSync(FULL, ""); writeFileSync(OUT_TXT, ""); writeFileSync(CONV_ID_FILE, "");

function pageHook() {
  window.__dcOpen = false;
  const _RTC = window.RTCPeerConnection;
  if (!_RTC || _RTC.__sh) return;
  function W(cfg) {
    const pc = new _RTC(cfg);
    const _cdc = pc.createDataChannel.bind(pc);
    pc.createDataChannel = function (label, opts) {
      const dc = _cdc(label, opts);
      dc.addEventListener("open", () => { window.__dcOpen = true; window.__on({ sys: "dc_open" }); });
      dc.addEventListener("message", (ev) => {
        try {
          let o = JSON.parse(String(ev.data));
          let inner = o && o.type === "data_message" && typeof o.data === "string" ? JSON.parse(o.data) : o;
          window.__on({ t: inner && inner.type, raw: JSON.stringify(inner) });
        } catch {}
      });
      return dc;
    };
    return pc;
  }
  W.prototype = _RTC.prototype;
  try { W.generateCertificate = _RTC.generateCertificate && _RTC.generateCertificate.bind(_RTC); } catch {}
  _RTC.__sh = true;
  window.RTCPeerConnection = W;
}

let convId = null;
const browser = await puppeteer.connect({ browserURL: CDP, defaultViewport: null });
const page = await browser.newPage();
await page.evaluateOnNewDocument(pageHook);
await page.exposeFunction("__on", (d) => {
  if (d.sys === "dc_open") { appendFileSync(OUT_TXT, "[dc_open]\n"); console.log("[watch] datachannel OPEN"); return; }
  try { appendFileSync(FULL, JSON.stringify(d) + "\n"); } catch {}
  const r = d.raw || "";
  const cid = r.match(/"conversation_id":"([a-f0-9-]+)"/);
  if (cid && !convId) { convId = cid[1]; writeFileSync(CONV_ID_FILE, convId); console.log(`[watch] conversation_id = ${convId}`); }
  // capture direction:out (Live speaking) text
  if (/"direction":"out"/.test(r)) {
    const tm = r.match(/"text":"((?:[^"\\]|\\.)*)"[^}]{0,30}"direction":"out"/);
    const txt = tm ? tm[1] : "";
    if (txt) appendFileSync(OUT_TXT, `[out] ${txt}\n`);
  }
});

console.log("[watch] loading chatgpt.com…");
await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
await page.bringToFront();
console.log('\n>>>>>>>>>>  CLICK "Start Voice" IN THE CHROME TAB NOW.  <<<<<<<<<<\n');

for (let i = 0; i < 360; i++) {  // 6 min window
  const open = await page.evaluate(() => !!window.__dcOpen).catch(() => false);
  if (open) break;
  if (i % 20 === 19) console.log(`[watch] still waiting for voice… (${i / 2}s)`);
  await new Promise((r) => setTimeout(r, 500));
}
console.log("[watch] voice open; recording for 240s. conversation_id file: /tmp/voice-conv-id");
await new Promise((r) => setTimeout(r, 240000));
await browser.disconnect();
