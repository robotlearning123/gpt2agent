// GPT-Live browser sidecar — runnable Mode B export path.
//
// Human-authenticated real Chrome owns WebRTC + media (Turnstile-cleared).
// This process:
//   1. Hooks the datachannel for input transcripts
//   2. Forwards human text to a pluggable agent hook
//   3. Injects speak-wire messages so Live voices the agent reply
//   4. Exposes a localhost control plane for agents (status/transcript/send_text/end)
//
// Audio never leaves the browser. Tokens/cookies stay in the Chrome profile.
// Headless Turnstile bypass is out of scope.
//
// Setup:
//   cd sidecar && npm install
//   open -na "Google Chrome" --args --user-data-dir="$PWD/.chrome-gptlive"
//   # sign into chatgpt.com, quit Chrome
//   echo "What is two plus two?" | mb voice -o q.mp3
//   ffmpeg -y -i q.mp3 -ar 24000 -ac 1 q.wav
//   node browser/sidecar.mjs --profile "$PWD/.chrome-gptlive" --audio q.wav
//
// Optional:
//   --control-port 8741   localhost control HTTP for agents
//   --reply "..."         fixed agent reply (demo Mode B speak path)
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
  console.log(`gpt2agent GPT-Live Mode B export sidecar

Usage:
  node browser/sidecar.mjs --profile DIR --audio FILE.wav [options]

Options:
  --profile DIR         Chrome user-data-dir already signed into ChatGPT
  --audio FILE          WAV fed as fake microphone
  --chrome PATH         Chrome binary
  --ms N                listen duration (default 25000)
  --control-port N      localhost control HTTP (default ${DEFAULT_CONTROL_PORT})
  --no-control          disable control HTTP
  --reply TEXT          fixed agent reply for every human turn (demo)
  --agent-cmd CMD       shell: stdin human text → stdout agent reply
  --help                this help

Control plane (agent surface, text only):
  GET  /help /status /transcript /health
  POST /send_text  {"text":"..."}
  POST /end

Boundary: no audio/secrets on the control plane. Turnstile bypass is out of scope.
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

/** @type {(wire: string) => Promise<boolean>} */
let injectSpeak = async () => false;

const exportPlane = new ModeBExport({
  onAgentTurn: async (humanText) => {
    console.log(`\n[human->agent] "${humanText}"`);
    if (FIXED_REPLY) {
      console.log(`[agent->speak] fixed reply`);
      return FIXED_REPLY;
    }
    if (AGENT_CMD) {
      const reply = await runAgentCmd(humanText);
      if (reply) console.log(`[agent->speak] from --agent-cmd`);
      return reply;
    }
    // Default: surface to control plane; agent may POST /send_text.
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
  // Speak injection used by the Node export plane / control server.
  window.__gptliveSend = (wire) => {
    try {
      if (cap.dc && cap.dc.readyState === "open") {
        cap.dc.send(wire);
        return true;
      }
    } catch {
      /* ignore */
    }
    return false;
  };
}

let control = null;
let browser = null;

async function shutdown() {
  exportPlane.close();
  try {
    if (control?.server) await new Promise((r) => control.server.close(r));
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

  injectSpeak = async (wire) => {
    try {
      return Boolean(await page.evaluate((w) => window.__gptliveSend?.(w) === true, wire));
    } catch {
      return false;
    }
  };

  if (!NO_CONTROL) {
    control = await createControlServer(exportPlane, {
      port: CONTROL_PORT,
      sendSpeak: injectSpeak,
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
    const result = await exportPlane.handleInbound(raw);
    if (result.humanText) {
      console.log(`[export] human: "${result.humanText}"`);
    }
    for (const wire of result.speakWires) {
      const ok = await injectSpeak(wire);
      console.log(`[export] speak inject ${ok ? "ok" : "pending"} (${wire.length}B)`);
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
