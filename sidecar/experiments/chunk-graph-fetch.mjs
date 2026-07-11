// Robust bundle fetch that does NOT need voice to start: load chatgpt, save all
// loaded chunks, mine the webpack/Turbopack chunk graph from them, then fetch
// every referenced lazy chunk (including the voice client) by URL.
import puppeteer from "puppeteer-core";
import { writeFileSync, readFileSync, readdirSync, statSync, mkdirSync, unlinkSync } from "node:fs";

const CDP = "http://127.0.0.1:9333";
const DIR = "/tmp/gpt-bundle";
mkdirSync(DIR, { recursive: true });
for (const f of readdirSync(DIR)) { try { unlinkSync(`${DIR}/${f}`); } catch {} }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await puppeteer.connect({ browserURL: CDP, defaultViewport: null });
const page = await browser.newPage();
await page.goto("https://chatgpt.com/", { waitUntil: "networkidle2", timeout: 60000 }).catch(() => {});
await sleep(3000);

// 1. all loaded JS resources -> save
let urls = await page.evaluate(() => [...new Set(performance.getEntriesByType("resource").map((r) => r.name).filter((n) => /\.js(\?|$)/.test(n)))]);
console.log(`[graph] ${urls.length} loaded JS chunks; saving…`);
const blob = {};
for (const u of urls) {
  try { const js = await page.evaluate(async (url) => await (await fetch(url)).text(), u); const n = (u.split("/").pop() || "x").split("?")[0]; writeFileSync(`${DIR}/${n}`, js); blob[n] = js; } catch {}
}
// 2. mine chunk-graph references from all loaded JS
const allJS = Object.values(blob).join("\n");
// cdn/assets/<8hex>-<base36>.js literal references
const refRe = /([0-9a-f]{8})-([0-9a-z]{6,20})\.js/g;
const refs = new Set();
let m;
while ((m = refRe.exec(allJS))) refs.add(`${m[1]}-${m[2]}.js`);
console.log(`[graph] ${refs.size} distinct chunk refs mined from loaded JS`);
// 3. fetch referenced chunks not already saved
const origin = "https://chiccdnassets";
const CDN = "https://persistent.oaistatic.com"; // common; will try chatgpt.com too
let newly = 0;
for (const ref of refs) {
  let got = false;
  for (const base of ["https://chatgpt.com/cdn/assets/", "https://persistent.oaistatic.com/chatgpt.com/cdn/assets/", "https://cdn.oaistatic.com/chatgpt.com/cdn/assets/"]) {
    try {
      const js = await page.evaluate(async (u) => { const r = await fetch(u); if (!r.ok) throw new Error(r.status); return await r.text(); }, base + ref).catch(() => null);
      if (js) { writeFileSync(`${DIR}/${ref}`, js); newly++; got = true; break; }
    } catch {}
  }
}
console.log(`[graph] fetched ${newly} new chunks`);

// 4. classify: which chunks contain the voice protocol
const KW = /data_message|spawn_update|RTCPeerConnection|createDataChannel|publishData|voice_session|conversation\.item|\.createDataChannel|realtime/i;
console.log("\n=== VOICE-PROTOCOL CHUNKS ===");
let meat = [];
for (const f of readdirSync(DIR)) {
  const js = readFileSync(`${DIR}/${f}`, "utf8");
  if (KW.test(js)) { const sz = statSync(`${DIR}/${f}`).size; meat.push([f, sz]); }
}
meat.sort((a, b) => b[1] - a[1]).forEach(([f, sz]) => console.log(`   ${(sz / 1024).toFixed(0).padStart(6)} KB  ${f}`));
console.log(`\ntotal chunks on disk: ${readdirSync(DIR).length}`);
await browser.disconnect();
