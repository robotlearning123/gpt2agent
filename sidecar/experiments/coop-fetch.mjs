// Cooperative bundle fetch: you click Start Voice in the Chrome window, this
// watches for the datachannel to open + voice chunks to load, then saves every
// chunk and captures full bootstrap payloads (system-prompt / prefill hunt).
import puppeteer from "puppeteer-core";
import { writeFileSync, appendFileSync, mkdirSync, readFileSync, readdirSync, statSync, unlinkSync } from "node:fs";

const CDP = "http://127.0.0.1:9333";
const BUNDLE_DIR = "/tmp/gpt-bundle";
const BOOT_LOG = "/tmp/gptlive-bootstrap.jsonl";
mkdirSync(BUNDLE_DIR, { recursive: true });
for (const f of readdirSync(BUNDLE_DIR)) { try { unlinkSync(`${BUNDLE_DIR}/${f}`); } catch {} }
writeFileSync(BOOT_LOG, "");

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
      dc.send = function (d) { try { window.__onBoot({ dir: "out", raw: String(d).slice(0, 3000) }); } catch {} return _send.apply(dc, arguments); };
      dc.addEventListener("open", () => { window.__dcOpen = true; try { window.__onBoot({ dir: "sys", t: "dc_open" }); } catch {} });
      dc.addEventListener("message", (ev) => {
        try {
          let o = JSON.parse(String(ev.data));
          let inner = o && o.type === "data_message" && typeof o.data === "string" ? JSON.parse(o.data) : o;
          window.__onBoot({ dir: "in", t: inner && inner.type, raw: JSON.stringify(inner).slice(0, 6000) });
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
let page = (await browser.pages()).find((p) => /chatgpt\.com/.test(p.url()));
if (!page) { page = await browser.newPage(); await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" }); }
await page.bringToFront();
await page.evaluateOnNewDocument(pageHook);
// install hook on the live page too (in case voice creates PC after reload)
await page.reload({ waitUntil: "domcontentloaded" });
await page.exposeFunction("__onBoot", (d) => { try { appendFileSync(BOOT_LOG, JSON.stringify(d) + "\n"); } catch {} });
await sleep(3000);

console.log("\n>>>>>>>>>>  In the Chrome window, click \"Start Voice\" now.  <<<<<<<<<<\n");
let dcOpen = false;
for (let i = 0; i < 60; i++) {            // 120s window
  dcOpen = await page.evaluate(() => !!window.__dcOpen).catch(() => false);
  if (dcOpen) { console.log(`[coop] datachannel OPEN after ~${i * 2}s`); break; }
  if (i % 10 === 9) {
    const ui = await page.evaluate(() => (document.body.innerText || "").slice(0, 400)).catch(() => "");
    const lim = ui.match(/(limit|try again|unavailable|error|rate|block|cannot|unable)[^\n]{0,60}/i);
    if (lim) console.log(`[coop] visible msg: ${lim[0].trim()}`);
    else console.log(`[coop] waiting for voice… (${i * 2}s)`);
  }
  await sleep(2000);
}
if (!dcOpen) { console.log("[coop] !! voice never opened in 120s — dump UI:"); console.log(await page.evaluate(() => (document.body.innerText || "").slice(0, 600)).catch(() => "")); }
await sleep(8000);

const urls = await page.evaluate(() => [...new Set(performance.getEntriesByType("resource").map((r) => r.name).filter((n) => /\.js(\?|$)/.test(n)))]);
let saved = 0;
for (const u of urls) {
  try { const js = await page.evaluate(async (url) => (await (await fetch(url)).text()), u); writeFileSync(`${BUNDLE_DIR}/${(u.split("/").pop() || "x").split("?")[0]}`, js); saved++; } catch {}
}
console.log(`[coop] saved ${saved} chunks`);

const bootLines = readFileSync(BOOT_LOG, "utf8").split("\n").filter(Boolean);
const byType = {};
for (const l of bootLines) { try { const o = JSON.parse(l); if (o.t) byType[o.t] = (byType[o.t] || 0) + 1; } catch {} }
console.log(`[coop] bootstrap events: ${bootLines.length}; types:`, byType);

console.log("\n=== voice-protocol chunks ===");
const KW = /data_message|spawn_update|RTCPeerConnection|voicePath|publishData|voice_session|conversation\.item|createDataChannel|realtimeVoice|sonic/;
for (const f of readdirSync(BUNDLE_DIR)) {
  const js = readFileSync(`${BUNDLE_DIR}/${f}`, "utf8");
  if (KW.test(js)) console.log(`   ${(statSync(`${BUNDLE_DIR}/${f}`).size / 1024).toFixed(0).padStart(6)} KB  ${f}`);
}
console.log("\n=== system-prompt / instructions hunt in bootstrap ===");
const boot = readFileSync(BOOT_LOG, "utf8");
const m = boot.match(/"(instructions|system_prompt|systemInstructions|preamble|greeting|prefill)"[^,}]{0,120}/gi);
console.log(m ? m.slice(0, 10) : "(no instructions-shaped field seen in bootstrap)");
await browser.disconnect();
