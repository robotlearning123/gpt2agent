// REAL probe (2026-07-11): does the consumer GPT-Live datachannel accept a
// response.create speak-injection (data_message envelope) and produce spoken
// audio? This is the Mode B make-or-break that static analysis could not close.
//
// Path: headed, signed-in Chrome (.chrome-gptlive) + fake WAV mic → open Voice →
// wait for state_update -> listening → send ONE response.create wire carrying a
// marker phrase → log every inbound datachannel event.
//
// PASS = marker word appears in any inbound event (Live spoke our injected text).
// Bonus  = the unique inbound event-type set fills the events.mjs verification gap.
//
// Owner-gated. Uses the real account; no audio/tokens are persisted — types/shapes only.

import puppeteer from "puppeteer-core";
import { existsSync } from "node:fs";
import { wrapDataMessage } from "../src/events.mjs";

const ROOT = "/Users/robert/workspace/52-chatgpt2agent/wt-live-voice/sidecar";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PROFILE = `${ROOT}/.chrome-gptlive`;
const AUDIO = `${ROOT}/mic.wav`;
const HOLD_MS = Number(process.argv[2] || "20000");
const MARKER = "The codeword is rutabaga seven. Say rutabaga seven.";

for (const [p, name] of [[CHROME, "Chrome"], [PROFILE, "profile"], [AUDIO, "audio"]]) {
  if (!existsSync(p)) { console.error(`${name} not found: ${p}`); process.exit(1); }
}

// response.create wrapped in the consumer data_message envelope — exactly what
// src/events.mjs::buildSpeakWire produces. If THIS makes Live speak, the contract holds.
const SPEAK_WIRE = wrapDataMessage({
  type: "response.create",
  response: { modalities: ["audio", "text"], instructions: MARKER },
});
console.log(`[probe] speak wire (${SPEAK_WIRE.length}B): ${SPEAK_WIRE.slice(0, 120)}…`);

// Injected before each page's own scripts. Wraps RTCPeerConnection -> createDataChannel
// so we see the negotiated dc's open/close/error/message regardless of how the app
// constructs it.
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
      dc.addEventListener("open", () => window.__onProbe({ kind: "dc_open" }));
      dc.addEventListener("close", () => window.__onProbe({ kind: "dc_close" }));
      dc.addEventListener("error", (e) => window.__onProbe({ kind: "dc_error", msg: String((e && e.message) || e) }));
      dc.addEventListener("message", (ev) => {
        try {
          let outer = JSON.parse(String(ev.data));
          let inner =
            outer && outer.type === "data_message" && typeof outer.data === "string"
              ? JSON.parse(outer.data)
              : outer && outer.type === "data_message" && outer.data && typeof outer.data === "object"
              ? outer.data
              : outer;
          const t = (inner && inner.type) || "?";
          const slim = JSON.stringify(inner).slice(0, 900);
          window.__onProbe({ kind: "msg", t, slim });
          if (t === "state_update" || (inner && inner.payload && inner.payload.type === "state_update")) {
            const ns = (inner && inner.payload && inner.payload.new_state) || (inner && inner.new_state);
            if (ns) window.__onProbe({ kind: "state", ns });
          }
        } catch {
          window.__onProbe({ kind: "msg_raw", raw: String(ev.data).slice(0, 200) });
        }
      });
      return dc;
    };
    return pc;
  }
  W.prototype = _RTC.prototype;
  try { W.generateCertificate = _RTC.generateCertificate && _RTC.generateCertificate.bind(_RTC); } catch {}
  _RTC.__hooked = true;
  window.RTCPeerConnection = W;
}

const seen = { types: new Set(), events: [], listening: false, dcOpen: false, closed: false, errors: [] };

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: false, // real headed browser — Turnstile/anti-bot path
  userDataDir: PROFILE,
  args: [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${AUDIO}`,
  ],
});

const cleanup = async (code) => {
  try { await browser.close(); } catch {}
  process.exit(code);
};
// Hard guard so we never hang the shell.
setTimeout(() => { console.log("\n[probe] HARD TIMEOUT — dumping and exiting"); cleanup(2); }, HOLD_MS + 45000);

try {
  const page = (await browser.pages())[0] || (await browser.newPage());
  await page.evaluateOnNewDocument(pageHook);

  await page.exposeFunction("__onProbe", (d) => {
    if (d.kind === "dc_open") { seen.dcOpen = true; console.log("[probe] datachannel OPEN"); }
    else if (d.kind === "dc_close") { seen.closed = true; console.log("[probe] datachannel CLOSE"); }
    else if (d.kind === "dc_error") { seen.errors.push(d.msg); console.log("[probe] datachannel ERROR:", d.msg); }
    else if (d.kind === "state") {
      console.log(`[probe] state_update -> ${d.ns}`);
      if (d.ns === "listening" && !seen.listening) { seen.listening = true; console.log("[probe] >>> LISTENING reached"); }
    } else if (d.kind === "msg") {
      seen.types.add(d.t);
      seen.events.push(`${d.t} :: ${d.slim}`);
      if (/transcript|response|state|error|track|audio|function/i.test(d.t)) {
        console.log(`[in] ${d.t}: ${d.slim.slice(0, 200)}`);
      }
      if (/transcript/i.test(d.slim)) console.log(`   *** TRANSCRIPT FIELD: ${d.slim.slice(0, 220)}`);
    } else if (d.kind === "msg_raw") {
      console.log(`[in raw] ${d.raw}`);
    }
  });

  console.log("[probe] navigating to chatgpt.com…");
  await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 5000));

  // Auth sanity check.
  try {
    const me = await page.evaluate(async () => {
      const r = await fetch("/backend-api/me", { credentials: "include" });
      return r.status;
    });
    console.log(`[probe] /backend-api/me -> HTTP ${me}`);
  } catch (e) {
    console.log("[probe] /backend-api/me check failed:", e.message);
  }

  console.log("[probe] clicking voice button…");
  const clicked = await page.evaluate(() => {
    const btn = [...document.querySelectorAll("button")].find((b) => {
      const al = `${b.getAttribute("aria-label") || ""} ${b.title || ""} ${b.getAttribute("data-testid") || ""}`;
      return /voice|speech|composer-speech/i.test(al);
    });
    if (btn) { btn.click(); return true; }
    return false;
  });
  console.log("[probe] voice button clicked:", clicked);

  // Wait up to 12s for listening (or dc open at least).
  const t0 = Date.now();
  while (!seen.listening && Date.now() - t0 < 12000) await new Promise((r) => setTimeout(r, 300));

  if (!seen.dcOpen) console.log("[probe] !! datachannel never opened — session gated (Turnstile?)");
  else if (!seen.listening) console.log("[probe] dc opened but never reached listening");

  console.log(`[probe] injecting response.create speak wire (marker: rutabaga)…`);
  const injected = await page.evaluate((w) => {
    try {
      const dc = window.__probe.dc;
      if (dc && dc.readyState === "open") { dc.send(w); return true; }
      return `dc_readyState=${dc && dc.readyState}`;
    } catch (e) { return "err:" + e.message; }
  }, SPEAK_WIRE);
  console.log("[probe] injection result:", injected);

  console.log(`[probe] holding ${HOLD_MS / 1000}s to capture any spoken response…`);
  await new Promise((r) => setTimeout(r, HOLD_MS));

  console.log("\n=== unique inbound event types (consumer enum) ===");
  console.log([...seen.types]);
  console.log(`\n=== total inbound messages: ${seen.events.length} ===`);
  console.log("=== last 25 events ===");
  seen.events.slice(-25).forEach((e) => console.log("  " + e.slice(0, 220)));

  const markerHit = seen.events.some((e) => /rutabaga/i.test(e));
  console.log(`\n>>> marker "rutabaga" in any inbound event: ${markerHit ? "YES → speak-injection CONFIRMED" : "NO → response.create was not spoken"}`);
  if (seen.closed) console.log(">>> datachannel CLOSED during run (session aborted — likely Turnstile/server validation)");
  if (seen.errors.length) console.log(">>> datachannel errors:", seen.errors);

  await cleanup(markerHit ? 0 : 1);
} catch (err) {
  console.error("[probe] fatal:", err instanceof Error ? err.message : err);
  await cleanup(1);
}
