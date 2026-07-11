// Diagnostic: where does the voice entry flow stop under puppeteer?
// Launches signed-in headed Chrome + fake mic, clicks the composer voice button,
// then dumps a screenshot + all visible button labels + any dialog/consent text,
// and reports whether ANY RTCPeerConnection was constructed at all.

import puppeteer from "puppeteer-core";
import { existsSync } from "node:fs";

const ROOT = "/Users/robert/workspace/52-chatgpt2agent/wt-live-voice/sidecar";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PROFILE = `${ROOT}/.chrome-gptlive`;
const AUDIO = `${ROOT}/mic.wav`;
const SHOT = "/tmp/gptlive-voice-state.png";

for (const [p, name] of [[CHROME, "Chrome"], [PROFILE, "profile"], [AUDIO, "audio"]]) {
  if (!existsSync(p)) { console.error(`${name} not found: ${p}`); process.exit(1); }
}

function pageHook() {
  window.__probe = { pcCount: 0, dc: null };
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
      dc.addEventListener("open", () => window.__onProbe({ kind: "dc_open" }));
      dc.addEventListener("close", () => window.__onProbe({ kind: "dc_close" }));
      return dc;
    };
    return pc;
  }
  W.prototype = _RTC.prototype;
  try { W.generateCertificate = _RTC.generateCertificate && _RTC.generateCertificate.bind(_RTC); } catch {}
  _RTC.__hooked = true;
  window.RTCPeerConnection = W;
}

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: false,
  userDataDir: PROFILE,
  args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${AUDIO}`],
});
const cleanup = async (code) => { try { await browser.close(); } catch {} process.exit(code); };
setTimeout(() => cleanup(3), 60000);

try {
  const page = (await browser.pages())[0] || (await browser.newPage());
  await page.evaluateOnNewDocument(pageHook);
  const signals = [];
  await page.exposeFunction("__onProbe", (d) => {
    if (d.kind === "pc") signals.push(`RTCPeerConnection created (iceServers=${d.ice})`);
    else if (d.kind === "dc") signals.push(`createDataChannel label="${d.label}" opts=${d.opts}`);
    else if (d.kind === "dc_open") signals.push("datachannel OPEN");
    else if (d.kind === "dc_close") signals.push("datachannel CLOSE");
  });

  await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
  await new Promise((r) => setTimeout(r, 5000));

  const clickVoice = async () => {
    return await page.evaluate(() => {
      const btn = [...document.querySelectorAll("button")].find((b) => {
        const al = `${b.getAttribute("aria-label") || ""} ${b.title || ""} ${b.getAttribute("data-testid") || ""}`;
        return /voice|speech|composer-speech/i.test(al);
      });
      if (btn) { btn.click(); return btn.getAttribute("data-testid") || btn.getAttribute("aria-label") || "(no id)"; }
      return null;
    });
  };

  console.log("[1] click composer voice button:", await clickVoice());
  await new Promise((r) => setTimeout(r, 6000));

  // Dump UI state.
  const ui = await page.evaluate(() => {
    const buttons = [...document.querySelectorAll("button")]
      .map((b) => ({
        id: b.getAttribute("data-testid") || "",
        al: b.getAttribute("aria-label") || "",
        t: (b.textContent || "").trim().slice(0, 40),
      }))
      .filter((b) => b.id || b.al || b.t);
    const bodyText = (document.body.innerText || "").slice(0, 1500);
    const dialogText = [...document.querySelectorAll("[role=dialog], [role=alertdialog], [data-modal]")]
      .map((d) => (d.innerText || "").trim().slice(0, 400));
    const orb = !!document.querySelector('[class*="orb" i], [class*="voice" i] canvas, [data-testid*="voice" i]');
    return { buttonCount: buttons.length, buttons: buttons.slice(0, 30), bodyText, dialogText, orb };
  });

  console.log("[2] RTCPeerConnection signals so far:", JSON.stringify(signals));
  console.log(`[3] orb/voice visual present: ${ui.orb}`);
  console.log("[4] visible buttons (id | aria-label | text):");
  ui.buttons.forEach((b) => console.log(`    ${[b.id, b.al, b.t].filter(Boolean).join(" | ")}`));
  console.log("[5] dialog/modal text:");
  ui.dialogText.length ? ui.dialogText.forEach((d) => console.log("    " + d)) : console.log("    (none)");
  console.log("[6] body text snippet (first 800 chars):");
  console.log(ui.bodyText.slice(0, 800));

  await page.screenshot({ path: SHOT, fullPage: false });
  console.log(`[7] screenshot: ${SHOT}`);

  // Try to walk the flow: click any "Start Voice" / "Got it" / voice-name / "Continue" if present.
  const walked = await page.evaluate(() => {
    const hit = [];
    const want = /start voice|got it|continue|begin|select|breeze|maple|solomon|cedar|cove|juniper|vale|sage|dan|ember|aria|live|use voice|meet voice/i;
    [...document.querySelectorAll("button")].forEach((b) => {
      const txt = `${b.getAttribute("aria-label") || ""} ${(b.textContent || "").trim()} ${b.getAttribute("data-testid") || ""}`;
      if (want.test(txt)) { b.click(); hit.push(txt.trim().slice(0, 40)); }
    });
    return hit;
  });
  if (walked.length) { console.log("[8] walked flow, clicked:", walked); await new Promise((r) => setTimeout(r, 8000)); }

  console.log("[9] RTCPeerConnection signals after walk:", JSON.stringify(signals));
  await page.screenshot({ path: "/tmp/gptlive-voice-state2.png", fullPage: false });
  console.log("[10] second screenshot: /tmp/gptlive-voice-state2.png");

  await cleanup(0);
} catch (err) {
  console.error("[diag] fatal:", err instanceof Error ? err.message : err);
  await cleanup(1);
}
