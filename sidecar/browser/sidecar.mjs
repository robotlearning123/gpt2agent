// GPT-Live browser sidecar — DIAGNOSTIC / TEST HARNESS (not the reliable path).
//
// It launches puppeteer Chrome with a fake WAV microphone to exercise the bridge
// layer end-to-end WITHOUT a human. Two important limits, both by design:
//   - Synthetic audio (fake WAV mic) is NOT transcribed by GPT-Live — only a real
//     mic is. So this harness proves wiring, not live transcription.
//   - Puppeteer automation may be flagged by the session's anti-bot check.
// The RELIABLE human path is the real signed-in Chrome + sidecar/extension +
// agent-gateway.mjs (see sidecar/README.md). Turnstile bypass is out of scope.
//
// What it does:
//   1. Hooks the datachannel and ingests human transcripts (real chat_message_delta)
//   2. Routes each human utterance to a pluggable coding-agent hook
//   3. Shows the agent reply as a TEXT OVERLAY (out-of-band — Live won't speak it)
//   4. Exposes the localhost control plane (status/transcript/end)
//
// Audio never leaves the browser. Tokens/cookies stay in the Chrome profile.
//
// Run:
//   cd sidecar && npm install
//   node browser/sidecar.mjs --profile "$PWD/.chrome-gptlive" --audio q.wav
//
// Optional:
//   --control-port 8741   localhost control HTTP for agents
//   --reply "..."         fixed agent reply for every human turn (wiring demo)
//   --agent-cmd '...'     shell command; stdin=human text, stdout=reply

import puppeteer from "puppeteer-core";
import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { ModeBExport, ExportState } from "../src/export.mjs";
import { createControlServer, DEFAULT_CONTROL_PORT, EXPORT_HELP } from "../src/control.mjs";

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}
function hasFlag(name) {
  return process.argv.includes(`--${name}`);
}

const CHROME = arg(
  "chrome",
  process.platform === "darwin"
    ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    : "/usr/bin/google-chrome",
);
const PROFILE = arg("profile", "./.chrome-gptlive");
const AUDIO = arg("audio", "./q.wav");
const RUN_MS = Number(arg("ms", "25000"));
const CONTROL_PORT = Number(arg("control-port", String(DEFAULT_CONTROL_PORT)));
const FIXED_REPLY = arg("reply", "");
const AGENT_CMD = arg("agent-cmd", "");
const NO_CONTROL = hasFlag("no-control");
const HELP = hasFlag("help") || hasFlag("h");

if (HELP) {
  console.log(`gpt2agent GPT-Live bridge — DIAGNOSTIC / TEST HARNESS

Fake-mic + puppeteer, for wiring tests only. Synthetic audio is NOT transcribed
by GPT-Live and puppeteer may trip anti-bot; the reliable human path is the real
signed-in Chrome + sidecar/extension + agent-gateway.mjs (see sidecar/README.md).

Usage:
  node browser/sidecar.mjs --profile DIR --audio FILE.wav [options]

Options:
  --profile DIR         Chrome user-data-dir already signed into ChatGPT
  --audio FILE          WAV fed as fake microphone (test only)
  --chrome PATH         Chrome binary
  --ms N                listen duration (default 25000)
  --control-port N      localhost control HTTP (default ${DEFAULT_CONTROL_PORT})
  --no-control          disable control HTTP
  --reply TEXT          fixed agent reply for every human turn (wiring demo)
  --agent-cmd CMD       shell: stdin human text → stdout agent reply
  --help                this help

Control plane (agent surface, text only, human → agent):
  GET  /help /status /transcript /health
  POST /end

Boundary: no audio/secrets on the control plane; agent→Live speak is unsupported;
Turnstile bypass is out of scope.
`);
  console.log(JSON.stringify(EXPORT_HELP, null, 2));
  process.exit(0);
}

if (!existsSync(AUDIO)) {
  console.error(`audio file not found: ${AUDIO} (make a WAV first — see header)`);
  process.exit(1);
}
if (!existsSync(CHROME)) {
  console.error(`Chrome not found at: ${CHROME}`);
  process.exit(1);
}

async function runAgentCmd(humanText) {
  if (!AGENT_CMD) return null;
  return new Promise((resolve) => {
    const child = spawn(AGENT_CMD, {
      shell: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let out = "";
    child.stdout.on("data", (d) => {
      out += d.toString("utf8");
    });
    child.on("error", () => resolve(null));
    child.on("close", () => {
      const reply = out.trim();
      resolve(reply || null);
    });
    child.stdin.write(humanText);
    child.stdin.end();
  });
}

/** @type {(text: string) => Promise<boolean>} */
let showReply = async () => false;

const exportPlane = new ModeBExport({
  onAgentTurn: async (humanText) => {
    console.log(`\n[human->agent] "${humanText}"`);
    if (FIXED_REPLY) {
      console.log(`[agent->reply] fixed reply`);
      return FIXED_REPLY;
    }
    if (AGENT_CMD) {
      const reply = await runAgentCmd(humanText);
      if (reply) console.log(`[agent->reply] from --agent-cmd`);
      return reply;
    }
    // Default: no local agent; the human transcript is still buffered for the
    // control plane (GET /transcript). No reply is shown.
    return null;
  },
});

// Injected into every page BEFORE its own scripts run.
function pageHook() {
  window.__gptlive = { out: [], events: [], transcripts: [], dc: null };
  const cap = window.__gptlive;
  const _RTC = window.RTCPeerConnection;
  if (!_RTC || _RTC.__hooked) return;
  function W(cfg) {
    const pc = new _RTC(cfg);
    const _cdc = pc.createDataChannel.bind(pc);
    pc.createDataChannel = function (label, opts) {
      const dc = _cdc(label, opts);
      cap.dc = dc;
      const _send = dc.send.bind(dc);
      dc.send = function (data) {
        try {
          cap.out.push(String(data).slice(0, 300));
        } catch {
          /* ignore */
        }
        return _send.apply(dc, arguments);
      };
      dc.addEventListener("message", (ev) => {
        try {
          let inner = String(ev.data);
          const outer = JSON.parse(inner);
          if (outer && outer.type === "data_message" && typeof outer.data === "string") {
            inner = outer.data;
          }
          const e = JSON.parse(inner);
          const t = (e && (e.type || (e.payload && e.payload.type))) || "?";
          cap.events.push(t);
          const s = JSON.stringify(e);
          if (/transcript|response|text|new_state/i.test(s)) {
            cap.transcripts.push(s.slice(0, 400));
          }
          window.dispatchEvent(
            new CustomEvent("gptlive-event", { detail: { type: t, raw: s, wire: String(ev.data) } }),
          );
        } catch {
          /* ignore parse errors */
        }
      });
      return dc;
    };
    return pc;
  }
  W.prototype = _RTC.prototype;
  try {
    W.generateCertificate = _RTC.generateCertificate && _RTC.generateCertificate.bind(_RTC);
  } catch {
    /* ignore */
  }
  W.__hooked = true;
  window.RTCPeerConnection = W;
  // Out-of-band egress: show the coding agent's reply as a text overlay. GPT-Live
  // will NOT speak injected text, so the human reads it here instead.
  window.__gptliveShowReply = (text) => {
    try {
      let el = document.getElementById("__gptlive_overlay");
      if (!el) {
        el = document.createElement("div");
        el.id = "__gptlive_overlay";
        el.style.cssText =
          "position:fixed;right:14px;bottom:14px;max-width:440px;max-height:45vh;overflow:auto;z-index:2147483647;background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:10px;padding:12px 14px;font:13px/1.45 ui-monospace,SFMono-Regular,monospace;white-space:pre-wrap;box-shadow:0 8px 30px rgba(0,0,0,.4)";
        document.documentElement.appendChild(el);
      }
      el.textContent = "🤖 coding agent:\n\n" + String(text);
      return true;
    } catch {
      return false;
    }
  };
}

let control = null;
let browser = null;

async function shutdown() {
  exportPlane.close();
  try {
    // Race server.close() with a timeout: a lingering keep-alive socket must not
    // block teardown forever (see control.mjs /end deferral).
    if (control?.server) {
      await Promise.race([
        new Promise((r) => control.server.close(r)),
        new Promise((r) => setTimeout(r, 1500)),
      ]);
    }
  } catch {
    /* ignore */
  }
  try {
    if (browser) await browser.close();
  } catch {
    /* ignore */
  }
}

try {
  browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: false, // real headed browser — Turnstile / anti-bot path
    userDataDir: PROFILE,
    args: [
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${AUDIO}`,
    ],
  });

  const page = (await browser.pages())[0] || (await browser.newPage());
  await page.evaluateOnNewDocument(pageHook);

  showReply = async (text) => {
    try {
      return Boolean(await page.evaluate((t) => window.__gptliveShowReply?.(t) === true, text));
    } catch {
      return false;
    }
  };

  if (!NO_CONTROL) {
    control = await createControlServer(exportPlane, {
      port: CONTROL_PORT,
      onEnd: async () => {
        console.log("[sidecar] control /end — shutting down");
        await shutdown();
        process.exit(0);
      },
    });
    console.log(`[sidecar] control plane: ${control.url}  (GET /help)`);
  }

  await page.exposeFunction("__onGptLive", async (detail) => {
    const raw = detail.wire || detail.raw;
    const result = await exportPlane.ingest(raw);
    if (result.humanText) {
      console.log(`[bridge] human: "${result.humanText}"`);
    }
    if (result.agentReply) {
      const ok = await showReply(result.agentReply);
      console.log(`[bridge] agent reply ${ok ? "shown (overlay)" : "display failed"} (${result.agentReply.length} chars)`);
    }
  });

  await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() =>
    window.addEventListener("gptlive-event", (e) => window.__onGptLive(e.detail)),
  );

  console.log("[sidecar] page loaded; opening voice…");
  exportPlane.setState(ExportState.LIVE);
  await page.waitForSelector("body");
  const clicked = await page.evaluate(() => {
    const btn = [...document.querySelectorAll("button")].find(
      (b) =>
        /voice|speech/i.test(b.getAttribute("aria-label") || "") ||
        /voice|speech/i.test(b.title || "") ||
        (b.getAttribute("data-testid") || "").includes("speech"),
    );
    if (btn) {
      btn.click();
      return true;
    }
    return false;
  });
  if (!clicked) {
    console.log(
      "[sidecar] voice button not found — is this profile logged in? Open voice manually.",
    );
  }

  console.log(`[sidecar] listening ${RUN_MS / 1000}s for transcription/response…`);
  await new Promise((r) => setTimeout(r, RUN_MS));

  const cap = await page.evaluate(() => window.__gptlive);
  console.log("\n=== datachannel event types ===", JSON.stringify([...new Set(cap?.events || [])]));
  console.log("=== outbound (client protocol) ===");
  console.log([
    ...new Set(
      (cap?.out || []).map((o) => {
        try {
          return JSON.parse(o).type;
        } catch {
          return o.slice(0, 40);
        }
      }),
    ),
  ]);
  console.log("=== transcription / response events ===");
  (cap?.transcripts || []).slice(0, 20).forEach((t) => console.log("  " + t));
  console.log("=== export transcripts (text only) ===");
  console.log(JSON.stringify(exportPlane.getTranscripts(), null, 2));
  console.log("=== export status ===");
  console.log(JSON.stringify(exportPlane.status(), null, 2));
} catch (err) {
  console.error("[sidecar] error:", err instanceof Error ? err.message : err);
  process.exitCode = 1;
} finally {
  await shutdown();
}
