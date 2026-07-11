// Foundation v2: reliably start voice (wait for datachannel OPEN) so the lazy
// voice chunks load, then SAVE EVERY JS chunk (grep later) and capture full
// bootstrap payloads (system-prompt / prefill / session-config hunt).
import puppeteer from "puppeteer-core";
import { writeFileSync, appendFileSync, mkdirSync, readFileSync, readdirSync, statSync, unlinkSync } from "node:fs";

const CDP = "http://127.0.0.1:9333";
const BUNDLE_DIR = "/tmp/gpt-bundle";
const BOOT_LOG = "/tmp/gptlive-bootstrap.jsonl";
mkdirSync(BUNDLE_DIR, { recursive: true });
for (const f of readdirSync(BUNDLE_DIR)) { try { unlinkSync(`${BUNDLE_DIR}/${f}`); } catch {} }
writeFileSync(BOOT_LOG, "");

let dcOpen = false;
function pageHook() {
  window.__dcOpen = false;
  const _RTC = window.RTCPeerConnection;
  if (!_RTC || _RTC.__bh) return;
  function W(cfg) {
    const pc = new _RTC(cfg);
    const _cdc = pc.createDataChannel.bind(pc);
    pc.createDataChannel = function (label, opts) {
      const dc = _cdc(label, opts);
      const _send = dc.send.bind(dc);
      dc.send = function (d) { try { window.__onBoot({ dir: "out", raw: String(d).slice(0, 2000) }); } catch {} return _send.apply(dc, arguments); };
      dc.addEventListener("open", () => { window.__dcOpen = true; try { window.__onBoot({ dir: "sys", t: "dc_open" }); } catch {} });
      dc.addEventListener("message", (ev) => {
        try {
          let o = JSON.parse(String(ev.data));
          let inner = o && o.type === "data_message" && typeof o.data === "string" ? JSON.parse(o.data) : o;
          window.__onBoot({ dir: "in", t: inner && inner.type, raw: JSON.stringify(inner).slice(0, 4000) });
        } catch {}
      });
      return dc;
    };
    return pc;
  }
  W.prototype = _RTC.prototype;
  try { W.generateCertificate = _RTC.generateCertificate && _RTC.generateCertificate.bind(_RTC); } catch {}
  _RTC.__bh = true;
  window.RTCPeerConnection = W;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const browser = await puppeteer.connect({ browserURL: CDP, defaultViewport: null });
const page = await browser.newPage();
await page.evaluateOnNewDocument(pageHook);
await page.exposeFunction("__onBoot", (d) => {
  if (d.dir === "sys" && d.t === "dc_open") dcOpen = true;
  try { appendFileSync(BOOT_LOG, JSON.stringify(d) + "\n"); } catch {}
});

console.log("[fetch] loading chatgpt.com + opening voice…");
await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
await sleep(5000);
await page.evaluate(() => {
  const b = [...document.querySelectorAll("button")].find((x) => /voice|speech|composer-speech/i.test(`${x.getAttribute("aria-label") || ""} ${x.title || ""} ${x.getAttribute("data-testid") || ""}`));
  if (b) b.click();
});
// walk onboarding + wait for dc open
for (let i = 0; i < 10 && !dcOpen; i++) {
  await page.evaluate(() => { const w = /start voice|continue|got it|begin/i; [...document.querySelectorAll("button")].forEach((b) => { if (w.test(`${b.getAttribute("aria-label") || ""} ${(b.textContent || "").trim()}`)) b.click(); }); });
  await sleep(2500);
  dcOpen = await page.evaluate(() => !!window.__dcOpen);
}
console.log("[fetch] voice datachannel open:", dcOpen);
if (!dcOpen) console.log("[fetch] !! voice did not start — big voice chunk may be missing");
await sleep(8000); // let chunks + bootstrap settle

console.log("[fetch] collecting ALL JS resource URLs…");
const urls = await page.evaluate(() => [...new Set(performance.getEntriesByType("resource").map((r) => r.name).filter((n) => /\.js(\?|$)/.test(n)))]);

console.log(`[fetch] saving all ${urls.length} chunks to ${BUNDLE_DIR}`);
let saved = 0, voiceHits = 0;
for (const u of urls) {
  try {
    const js = await page.evaluate(async (url) => { const r = await fetch(url); return await r.text(); }, u);
    const name = (u.split("/").pop() || "x").split("?")[0];
    writeFileSync(`${BUNDLE_DIR}/${name}`, js);
    saved++;
    if (/data_message|spawn_update|RTCPeerConnection|voicePath|publishData|voice_session|conversation\.item/.test(js)) voiceHits++;
  } catch {}
}
console.log(`[fetch] saved ${saved} chunks; ${voiceHits} contain core voice-protocol terms`);

const bootLines = readFileSync(BOOT_LOG, "utf8").split("\n").filter(Boolean);
const byType = {};
for (const l of bootLines) { try { const o = JSON.parse(l); if (o.t) byType[o.t] = (byType[o.t] || 0) + 1; } catch {} }
console.log(`\n[fetch] bootstrap events: ${bootLines.length} -> ${BOOT_LOG}`);
console.log("  inbound types:", byType);
console.log("\n=== voice-protocol chunks (the real meat) ===");
const KW = /data_message|spawn_update|RTCPeerConnection|voicePath|publishData|voice_session|conversation\.item|createDataChannel/;
for (const f of readdirSync(BUNDLE_DIR)) {
  const js = readFileSync(`${BUNDLE_DIR}/${f}`, "utf8");
  if (KW.test(js)) console.log(`   ${(statSync(`${BUNDLE_DIR}/${f}`).size / 1024).toFixed(0).padStart(6)} KB  ${f}`);
}
await browser.disconnect();
