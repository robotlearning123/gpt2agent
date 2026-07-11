// Persistent real-time monitor + interactive injector for GPT-Live.
// Connects to the running logged-in Chrome (CDP 9333), opens ONE chatgpt voice
// tab with the RTCPeerConnection hook, and streams every datachannel event
// (inbound + outbound) with timestamps to stdout + /tmp/gptlive-live.log.
// Reads injection commands from /tmp/gptlive-inject.jsonl (one inner JSON per line;
// each is envelope-wrapped and dc.send'd) so we can test speak candidates live.

import puppeteer from "puppeteer-core";
import { appendFileSync, readFileSync, writeFileSync, statSync } from "node:fs";
import { wrapDataMessage } from "../src/events.mjs";

const CDP = process.env.CDP_URL || "http://127.0.0.1:9333";
const LIVE_LOG = "/tmp/gptlive-live.log";
const FULL_LOG = "/tmp/gptlive-full.jsonl";
const INJECT_CMD = "/tmp/gptlive-inject.jsonl";
const HOLD_MS = Number(process.argv[2] || "300000");
writeFileSync(LIVE_LOG, "");
writeFileSync(FULL_LOG, "");
try { writeFileSync(INJECT_CMD, ""); } catch {}

const ts = () => new Date().toISOString().slice(11, 23); // HH:MM:SS.mmm
const log = (s) => { const l = `[${ts()}] ${s}`; console.log(l); try { appendFileSync(LIVE_LOG, l + "\n"); } catch {} };

function pageHook() {
  window.__probe = { dc: null };
  const _RTC = window.RTCPeerConnection;
  if (!_RTC || _RTC.__hooked) return;
  function W(cfg) {
    const pc = new _RTC(cfg);
    const _cdc = pc.createDataChannel.bind(pc);
    pc.createDataChannel = function (label, opts) {
      const dc = _cdc(label, opts);
      window.__probe.dc = dc;
      const _send = dc.send.bind(dc);
      dc.send = function (data) {
        try {
          let o = JSON.parse(String(data));
          let inner = o && o.type === "data_message" && typeof o.data === "string" ? JSON.parse(o.data) : o;
          window.__onProbe({ dir: "out", t: (inner && inner.type) || "?", raw: JSON.stringify(inner).slice(0, 600) });
        } catch {}
        return _send.apply(dc, arguments);
      };
      dc.addEventListener("open", () => window.__onProbe({ dir: "sys", t: "dc_open" }));
      dc.addEventListener("close", () => window.__onProbe({ dir: "sys", t: "dc_close" }));
      dc.addEventListener("message", (ev) => {
        try {
          let outer = JSON.parse(String(ev.data));
          let inner = outer && outer.type === "data_message" && typeof outer.data === "string" ? JSON.parse(outer.data) : outer;
          // FULL payload (no slice) so capability content_types deep in the JSON survive.
          window.__onProbe({ dir: "in", t: (inner && inner.type) || "?", raw: JSON.stringify(inner) });
        } catch {}
      });
      return dc;
    };
    return pc;
  }
  W.prototype = _RTC.prototype;
  try { W.generateCertificate = _RTC.generateCertificate && _RTC.generateCertificate.bind(_RTC); } catch {}
  _RTC.__hooked = true;
  window.RTCPeerConnection = W;
  // injector bridge: Node writes inner-JSON lines to a file the page polls? No —
  // Node calls page.evaluate to send directly. Keep __probe.dc as the handle.
  window.__gptliveSend = (wire) => { try { const dc = window.__probe.dc; if (dc && dc.readyState === "open") { dc.send(wire); return true; } } catch {} return false; };
}

const seen = { listening: false };
let browser, page;
let injectPos = 0;

async function pollInject() {
  try {
    const txt = readFileSync(INJECT_CMD, "utf8");
    const lines = txt.split("\n").filter(Boolean);
    for (let i = injectPos; i < lines.length; i++) {
      injectPos = i + 1;
      let inner;
      try { inner = JSON.parse(lines[i]); } catch { log(`[inject] bad json: ${lines[i].slice(0, 80)}`); continue; }
      const wire = wrapDataMessage(inner);
      const r = await page.evaluate((w) => window.__gptliveSend ? window.__gptliveSend(w) : "no-bridge", wire);
      log(`[inject] sent ${inner.type} (${wire.length}B) -> ${r}`);
    }
  } catch {}
}

const finish = async (code) => { try { await browser?.disconnect(); } catch {} log("=== monitor exit ==="); process.exit(code); };
setTimeout(() => finish(0), HOLD_MS);

try {
  browser = await puppeteer.connect({ browserURL: CDP, defaultViewport: null });
  // close stray chatgpt tabs so we own exactly one voice session / mic
  const tabs = (await browser.pages()).filter((p) => /chatgpt\.com/.test(p.url()));
  for (const t of tabs) { try { await t.close(); } catch {} }

  page = await browser.newPage();
  await page.evaluateOnNewDocument(pageHook);
  await page.exposeFunction("__onProbe", (d) => {
    if (d.dir === "sys") { log(`[*] ${d.t}`); if (d.t === "dc_open") log(">>> DATACHANNEL OPEN"); return; }
    // log FULL inbound payload for capability analysis (content_types, tool/search/memory)
    if (d.dir === "in") { try { appendFileSync(FULL_LOG, JSON.stringify({ ts: ts(), t: d.t, raw: d.raw }) + "\n"); } catch {} }
    // surface transcripts and state prominently, plus everything else compactly
    let note = "";
    const m = d.raw.match(/"text"\s*:\s*"([^"]{0,80})"[^}]{0,40}"direction"\s*:\s*"([^"]*)"/);
    if (m) note = `  [${m[2]}] "${m[1]}"`;
    if (/state_update/.test(d.raw)) { const s = d.raw.match(/"new_state"\s*:\s*"([^"]*)"/); note = `  -> ${s ? s[1] : "?"}`; if (s && s[1] === "listening" && !seen.listening) { seen.listening = true; log(">>> LISTENING — mic live"); } }
    // flag capability-suggestive content_types or event types inline
    if (/search|tool|function|memory|canvas|retrieval|image_gen|code_interpreter|connection/i.test(d.raw)) {
      note += `  ⚡${(d.raw.match(/"(content_type|type)":\s*"[a-z_]+"/gi) || []).join(" ")}`;
    }
    log(`${d.dir === "out" ? "OUT" : "IN "} ${d.t}${note}`);
  });

  log("opening chatgpt.com…");
  await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 5000));
  log(`/backend-api/me -> ${await page.evaluate(async () => (await fetch("/backend-api/me", { credentials: "include" })).status)}`);

  await page.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => /voice|speech|composer-speech/i.test(`${x.getAttribute("aria-label") || ""} ${x.title || ""} ${x.getAttribute("data-testid") || ""}`));
    if (b) b.click();
  });
  log("clicked voice; walking onboarding if needed…");

  // poll for dc open + periodic onboarding walk + inject poll
  const t0 = Date.now();
  while (Date.now() - t0 < HOLD_MS - 5000) {
    await new Promise((r) => setTimeout(r, 1500));
    if (!seen.listening) {
      await page.evaluate(() => {
        const w = /start voice|continue|got it|begin/i;
        [...document.querySelectorAll("button")].forEach((b) => { if (w.test(`${b.getAttribute("aria-label") || ""} ${(b.textContent || "").trim()}`)) b.click(); });
      });
    }
    await pollInject();
  }
  await finish(0);
} catch (err) {
  log(`FATAL: ${err.message}`);
  await finish(1);
}
