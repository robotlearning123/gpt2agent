// Option-2 MVP: consumer GPT-Live voice → our coding agent (no browser in the
// agent loop; browser only hosts the voice session for Turnstile).
//
// Tap the live voice datachannel, reconstruct each HUMAN utterance from the real
// consumer protocol (chat_message_delta, direction:"in", JSON-patch appends), and
// on utterance completion invoke a pluggable coding-agent backend (default
// `claude -p`: stdin=utterance, stdout=reply). Reply is printed and (optionally)
// posted to the shared conversation. We do NOT try to make Live speak the reply —
// that's the proven-dead injection wall.
//
// Usage:
//   node experiments/voice-to-agent.mjs [--agent-cmd 'claude -p'] [--post-reply]
// Then click "Start Voice" in the Chrome tab and talk.
import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import { appendFileSync, writeFileSync } from "node:fs";

const CDP = "http://127.0.0.1:9333";
const LOG = "/tmp/voice-to-agent.log";
writeFileSync(LOG, "");
const AGENT_CMD = (process.argv.slice(2).find((a, i, arr) => arr[i - 1] === "--agent-cmd")) || "claude -p";
const POST_REPLY = process.argv.includes("--post-reply");
const log = (s) => { const l = `[${new Date().toISOString().slice(11, 23)}] ${s}`; console.log(l); appendFileSync(LOG, l + "\n"); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Run the coding agent: stdin = human text, stdout = reply text.
function runAgent(humanText) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const child = spawn(AGENT_CMD, { shell: true, stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    child.stdout.on("data", (d) => (out += d.toString("utf8")));
    child.stderr.on("data", () => {});
    child.on("error", () => resolve(null));
    child.on("close", () => resolve({ reply: out.trim(), ms: Date.now() - t0 }));
    child.stdin.write(humanText);
    child.stdin.end();
  });
}

// Browser hook: reconstruct messages from chat_message_delta patches.
function pageHook() {
  window.__msgs = {};       // mid -> {role, dir, text, done}
  window.__order = [];
  window.__dcOpen = false;
  const _RTC = window.RTCPeerConnection;
  if (!_RTC || _RTC.__v2a) return;
  function W(cfg) {
    const pc = new _RTC(cfg);
    window.__lastPc = pc;
    const _cdc = pc.createDataChannel.bind(pc);
    pc.createDataChannel = function (label, opts) {
      const dc = _cdc(label, opts);
      dc.addEventListener("open", () => { window.__dcOpen = true; window.__on({ sys: "dc_open" }); });
      dc.addEventListener("message", (ev) => {
        try {
          let o = JSON.parse(String(ev.data));
          let inner = o && o.type === "data_message" && typeof o.data === "string" ? JSON.parse(o.data) : o;
          if ((inner && inner.type) !== "chat_message_delta") return;
          const d = (inner.payload || inner).delta || {};
          // add message skeleton
          if (d.o === "add" && d.v && d.v.message) {
            const m = d.v.message; const mid = m.id;
            if (mid && !window.__msgs[mid]) {
              let dir = null, txt = "";
              for (const p of (m.content && m.content.parts) || []) if (p.direction) { dir = p.direction; txt = p.text || ""; }
              window.__msgs[mid] = { role: (m.author || {}).role, dir, text: txt, done: false };
              window.__order.push(mid);
            }
          }
          // patch appends (apply to most recent message)
          const last = window.__order[window.__order.length - 1];
          if (last && Array.isArray(d.v)) {
            for (const op of d.v) {
              if (op.o === "append" && op.p === "/message/content/parts/0/text" && window.__msgs[last]) {
                window.__msgs[last].text += op.v || "";
              }
              if (op.o === "replace" && op.p === "/message/status" && op.v === "finished_successfully" && window.__msgs[last]) {
                window.__msgs[last].done = true;
                // emit completed human utterance
                if (window.__msgs[last].dir === "in") window.__on({ utterance: window.__msgs[last].text });
              }
            }
          }
          if (d.o === "replace" && d.p === "/message/status" && d.v === "finished_successfully" && last && window.__msgs[last]) {
            window.__msgs[last].done = true;
            if (window.__msgs[last].dir === "in") window.__on({ utterance: window.__msgs[last].text });
          }
        } catch {}
      });
      return dc;
    };
    return pc;
  }
  W.prototype = _RTC.prototype;
  try { W.generateCertificate = _RTC.generateCertificate && _RTC.generateCertificate.bind(_RTC); } catch {}
  _RTC.__v2a = true;
  window.RTCPeerConnection = W;
}

const browser = await puppeteer.connect({ browserURL: CDP, defaultViewport: null });
const page = await browser.newPage();
await page.evaluateOnNewDocument(pageHook);
await page.exposeFunction("__on", async (d) => {
  if (d.sys === "dc_open") { log(">>> datachannel OPEN — voice live"); return; }
  if (d.utterance == null) return;
  const human = String(d.utterance).trim();
  if (!human) return;
  log(`\n==========\n[human] ${human}\n----------`);
  log(`[agent] invoking: ${AGENT_CMD}`);
  const r = await runAgent(human);
  if (r && r.reply) {
    log(`[agent reply, ${r.ms}ms]\n${r.reply}\n==========`);
    try {
      await page.evaluate((t) => {
        let el = document.getElementById("__gptlive_overlay");
        if (!el) {
          el = document.createElement("div");
          el.id = "__gptlive_overlay";
          el.style.cssText = "position:fixed;right:14px;bottom:14px;max-width:440px;max-height:45vh;overflow:auto;z-index:2147483647;background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:10px;padding:12px 14px;font:13px/1.45 ui-monospace,monospace;white-space:pre-wrap;box-shadow:0 8px 30px rgba(0,0,0,.4)";
          document.documentElement.appendChild(el);
        }
        el.textContent = "🤖 coding agent:\n\n" + t;
      }, r.reply);
    } catch {}
  } else {
    log(`[agent] no reply (${r ? r.ms + "ms" : "error"})`);
  }
});

log(`voice→agent bridge. agent-cmd=${AGENT_CMD!="" ? AGENT_CMD : "claude -p"}  post-reply=${POST_REPLY}`);
log("loading chatgpt.com…");
await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
await page.bringToFront();

// getStats poll: confirm synthetic mic audio is egressing (replaces-human diagnostic)
setInterval(async () => {
  try {
    const sent = await page.evaluate(async () => {
      if (!window.__lastPc) return null;
      const s = await window.__lastPc.getStats();
      let packets = null, bytes = null;
      s.forEach((r) => { if (r.type === "outbound-rtp" && r.kind === "audio") { packets = r.packetsSent; bytes = r.bytesSent; } });
      return { packets, bytes };
    });
    if (sent && sent.packets != null) log(`[audio egress] outbound-rtp packetsSent=${sent.packets} bytes=${sent.bytes}`);
  } catch {}
}, 3000);
log("auto-starting voice…");
for (let i = 0; i < 12; i++) {
  const open = await page.evaluate(() => !!window.__dcOpen).catch(() => false);
  if (open) { log(">>> datachannel OPEN — speak now"); break; }
  await page.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => /voice|speech|composer-speech/i.test(`${x.getAttribute("aria-label") || ""} ${x.title || ""} ${x.getAttribute("data-testid") || ""}`));
    if (b) b.click();
    const w = /start voice|continue|got it|begin/i;
    [...document.querySelectorAll("button")].forEach((b) => { if (w.test(`${b.getAttribute("aria-label") || ""} ${(b.textContent || "").trim()}`)) b.click(); });
  });
  await sleep(2500);
}
log('>>>>> SPEAK NOW (if voice did not open, click Start Voice in the tab) <<<<<');
for (;;) await sleep(5000);
