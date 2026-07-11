// REAL probe over CDP — attaches to the user's ALREADY-RUNNING, logged-in Chrome
// (relaunched once with --remote-debugging-port=9333). Does NOT launch or close the
// browser; uses browser.disconnect() only.
//
// Question: does the consumer GPT-Live datachannel accept a response.create
// speak-injection (data_message envelope) and produce spoken audio?
// PASS = marker "rutabaga" appears in any inbound event.

import puppeteer from "puppeteer-core";
import { wrapDataMessage } from "../src/events.mjs";
import { appendFileSync, writeFileSync } from "node:fs";

const OBSERVE = !!process.env.OBSERVE;
const LOG = "/tmp/gptlive-events.log";
writeFileSync(LOG, ""); // reset per run

const CDP = process.env.CDP_URL || "http://127.0.0.1:9333";
const HOLD_MS = Number(process.argv[2] || "22000");
const MARKER = "The codeword is rutabaga seven. Say rutabaga seven.";
const SPEAK_WIRE = wrapDataMessage({
  type: "response.create",
  response: { modalities: ["audio", "text"], instructions: MARKER },
});
console.log(`[probe] CDP target: ${CDP}`);
console.log(`[probe] speak wire (${SPEAK_WIRE.length}B): ${SPEAK_WIRE.slice(0, 110)}…`);

function pageHook() {
  window.__probe = { dc: null, pcCount: 0 };
  const _RTC = window.RTCPeerConnection;
  if (!_RTC || _RTC.__hooked) return;
  function W(cfg) {
    window.__probe.pcCount += 1;
    window.__onProbe({ kind: "pc", ice: (cfg && cfg.iceServers && cfg.iceServers.length) || 0 });
    const pc = new _RTC(cfg);
    const _cdc = pc.createDataChannel.bind(pc);
    pc.createDataChannel = function (label, opts) {
      const dc = _cdc(label, opts);
      window.__probe.dc = dc;
      window.__onProbe({ kind: "dc", label: String(label), opts: JSON.stringify(opts || {}) });
      // Also capture OUTBOUND (client->server) types so we see the real app's
      // protocol (track_state, client_metrics, and anything it sends when speaking).
      const _send = dc.send.bind(dc);
      dc.send = function (data) {
        try {
          let o = JSON.parse(String(data));
          let inner =
            o && o.type === "data_message" && typeof o.data === "string" ? JSON.parse(o.data) : o;
          window.__onProbe({ kind: "out", t: (inner && inner.type) || "?" });
        } catch {
          window.__onProbe({ kind: "out", t: "?" });
        }
        return _send.apply(dc, arguments);
      };
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

const seen = { types: new Set(), events: [], out: new Set(), listening: false, dcOpen: false, closed: false, errors: [], pc: 0 };
let browser, page;

const finish = async (code) => {
  try { if (browser) await browser.disconnect(); } catch {}
  console.log("\n=== unique INBOUND event types (server -> client) ===");
  console.log([...seen.types]);
  console.log("=== unique OUTBOUND event types (client -> server, app's own protocol) ===");
  console.log([...seen.out]);
  console.log(`=== total inbound messages: ${seen.events.length}, PC created: ${seen.pc} ===`);
  console.log("=== last 25 events ===");
  seen.events.slice(-25).forEach((e) => console.log("  " + e.slice(0, 220)));
  const markerHit = seen.events.some((e) => /rutabaga/i.test(e));
  console.log(`\n>>> marker "rutabaga" in any inbound event: ${markerHit ? "YES → speak-injection CONFIRMED" : "NO → response.create was not spoken back"}`);
  if (seen.closed) console.log(">>> datachannel CLOSED during run (session aborted)");
  if (seen.errors.length) console.log(">>> datachannel errors:", seen.errors);
  process.exit(code);
};
setTimeout(() => { console.log("\n[probe] HARD TIMEOUT"); finish(2); }, HOLD_MS + 45000);

try {
  browser = await puppeteer.connect({ browserURL: CDP, defaultViewport: null });
  console.log("[probe] connected to running Chrome");

  // Open a FRESH chatgpt tab with the hook installed before any app script runs.
  page = await browser.newPage();
  await page.evaluateOnNewDocument(pageHook);
  await page.exposeFunction("__onProbe", (d) => {
    if (d.kind === "pc") { seen.pc += 1; console.log(`[probe] RTCPeerConnection created (iceServers=${d.ice})`); }
    else if (d.kind === "out") { seen.out.add(d.t); }
    else if (d.kind === "dc") { console.log(`[probe] createDataChannel label="${d.label}" opts=${d.opts}`); }
    else if (d.kind === "dc_open") { seen.dcOpen = true; console.log("[probe] datachannel OPEN"); }
    else if (d.kind === "dc_close") { seen.closed = true; console.log("[probe] datachannel CLOSE"); }
    else if (d.kind === "dc_error") { seen.errors.push(d.msg); console.log("[probe] datachannel ERROR:", d.msg); }
    else if (d.kind === "state") {
      console.log(`[probe] state_update -> ${d.ns}`);
      if (d.ns === "listening" && !seen.listening) { seen.listening = true; console.log("[probe] >>> LISTENING reached"); }
    } else if (d.kind === "msg") {
      seen.types.add(d.t);
      const line = `${d.t} :: ${d.slim}`;
      seen.events.push(line);
      try { appendFileSync(LOG, line + "\n"); } catch {}
      // In OBSERVE mode log EVERY inbound event; otherwise only notable ones.
      if (OBSERVE || /transcript|response|state|error|track|audio|function/i.test(d.t)) {
        console.log(`[in] ${d.t}: ${d.slim.slice(0, 240)}`);
      }
      if (/transcript/i.test(d.slim)) console.log(`   *** TRANSCRIPT FIELD: ${d.slim.slice(0, 240)}`);
    } else if (d.kind === "msg_raw") console.log(`[in raw] ${d.raw}`);
  });

  console.log("[probe] opening fresh chatgpt.com tab…");
  await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 5000));

  try {
    const me = await page.evaluate(async () => (await fetch("/backend-api/me", { credentials: "include" })).status);
    console.log(`[probe] /backend-api/me -> HTTP ${me}`);
  } catch (e) { console.log("[probe] /backend-api/me failed:", e.message); }

  // Click the composer voice control.
  const clicked = await page.evaluate(() => {
    const btn = [...document.querySelectorAll("button")].find((b) => {
      const al = `${b.getAttribute("aria-label") || ""} ${b.title || ""} ${b.getAttribute("data-testid") || ""}`;
      return /voice|speech|composer-speech/i.test(al);
    });
    if (btn) { btn.click(); return btn.getAttribute("data-testid") || btn.getAttribute("aria-label") || "(id)"; }
    return null;
  });
  console.log("[probe] clicked voice button:", clicked);
  await new Promise((r) => setTimeout(r, 4000));

  // Walk the onboarding flow if present (consent / picker / Start Voice).
  for (let step = 0; step < 4 && !seen.dcOpen; step++) {
    const hit = await page.evaluate(() => {
      const want = /start voice|got it|continue|begin|try it|breeze|maple|solomon|cedar|cove|juniper|vale|sage|ember|aria|live|use voice|meet voice|next|enable/i;
      const hits = [];
      [...document.querySelectorAll("button")].forEach((b) => {
        const txt = `${b.getAttribute("aria-label") || ""} ${(b.textContent || "").trim()} ${b.getAttribute("data-testid") || ""}`;
        if (want.test(txt) && !b.disabled) { b.click(); hits.push(txt.trim().slice(0, 36)); }
      });
      return hits;
    });
    if (hit.length) console.log(`[probe] onboarding step ${step}: clicked ${JSON.stringify(hit)}`);
    await new Promise((r) => setTimeout(r, 3500));
  }

  const t0 = Date.now();
  while (!seen.listening && Date.now() - t0 < 12000) await new Promise((r) => setTimeout(r, 300));
  if (!seen.pc) console.log("[probe] !! no RTCPeerConnection was ever created — voice session did not start");
  else if (!seen.dcOpen) console.log("[probe] !! PC created but no datachannel opened");

  if (OBSERVE) {
    console.log("[probe] OBSERVE mode — no injection. Listening for real speech / Live response.");
    console.log(`[probe] holding ${HOLD_MS / 1000}s (events logged to ${LOG})…`);
    console.log('>>>>> SPEAK NOW (e.g. "What is two plus two?") <<<<<');
  } else {
    console.log("[probe] injecting response.create speak wire (marker: rutabaga)…");
    const injected = await page.evaluate((w) => {
      try {
        const dc = window.__probe && window.__probe.dc;
        if (dc && dc.readyState === "open") { dc.send(w); return "sent"; }
        return `dc_readyState=${dc && dc.readyState}`;
      } catch (e) { return "err:" + e.message; }
    }, SPEAK_WIRE);
    console.log("[probe] injection result:", injected);

    console.log(`[probe] holding ${HOLD_MS / 1000}s for any spoken response…`);
  }
  await new Promise((r) => setTimeout(r, HOLD_MS));

  await finish(seen.events.some((e) => /rutabaga/i.test(e)) ? 0 : 1);
} catch (err) {
  console.error("[probe] fatal:", err instanceof Error ? err.message : err);
  await finish(1);
}
