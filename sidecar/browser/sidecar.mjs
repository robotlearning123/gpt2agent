// GPT-Live browser sidecar — the viable path to a working spoken round-trip.
//
// Why a browser: the werift Node path completes the handshake but its audio SRTP
// egress never reaches the OpenAI server (confirmed — even continuous silence
// closes ~1s after "listening"), while ChatGPT's own web client works perfectly.
// So this drives a real Chrome (the app's working media stack) and reads the
// transcription + spoken-response off the datachannel.
//
// It needs a Chrome profile that is ALREADY LOGGED INTO ChatGPT (you sign in
// once; credentials are never handled here). The "microphone" is a WAV file fed
// via Chrome's --use-file-for-fake-audio-capture, so no real mic is used.
//
// Setup (on the Mac):
//   cd sidecar && npm install
//   # 1) make a logged-in profile once:
//   #    open -na "Google Chrome" --args --user-data-dir="$PWD/.chrome-gptlive"
//   #    -> sign into chatgpt.com in that window, then quit it.
//   # 2) make a question WAV (16k/mono is fine for ASR):
//   #    echo "What is two plus two?" | mb voice -o q.mp3 && \
//   #      ffmpeg -y -i q.mp3 -ar 24000 -ac 1 q.wav
//   # 3) run:
//   node browser/sidecar.mjs --profile "$PWD/.chrome-gptlive" --audio q.wav
//
// Output: the datachannel event stream — you should see the human utterance
// transcribed and GPT-Live's response, i.e. the human -> GPT-Live round-trip.
// (Routing the transcript to YOUR agent and voicing its reply back — full Mode B
// — is the onAgentTurn hook below; the "speak provided text" outbound command
// still needs one capture, see docs evidence.)

import puppeteer from "puppeteer-core";
import { existsSync } from "node:fs";

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

const CHROME = arg("chrome", process.platform === "darwin"
  ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  : "/usr/bin/google-chrome");
const PROFILE = arg("profile", "./.chrome-gptlive");
const AUDIO = arg("audio", "./q.wav");
const RUN_MS = Number(arg("ms", "25000"));

if (!existsSync(AUDIO)) {
  console.error(`audio file not found: ${AUDIO} (make a WAV first — see header)`);
  process.exit(1);
}

// Pluggable Mode B hook: given the human's transcript, return text for GPT-Live
// to speak. Wire this to gpt2agent / Claude. Default just logs.
async function onAgentTurn(humanText) {
  console.log(`\n[human->agent] "${humanText}"`);
  // return "your agent's reply text";  // (needs the speak-injection command)
  return null;
}

// Injected into every page BEFORE its own scripts run (fixes the hook-too-late
// problem): wrap RTCPeerConnection -> createDataChannel -> send/onmessage and
// surface events on window.__gptlive.
function pageHook() {
  window.__gptlive = { out: [], events: [], transcripts: [] };
  const cap = window.__gptlive;
  const _RTC = window.RTCPeerConnection;
  if (!_RTC || _RTC.__hooked) return;
  function W(cfg) {
    const pc = new _RTC(cfg);
    const _cdc = pc.createDataChannel.bind(pc);
    pc.createDataChannel = function (label, opts) {
      const dc = _cdc(label, opts);
      const _send = dc.send.bind(dc);
      dc.send = function (data) { try { cap.out.push(String(data).slice(0, 300)); } catch {} return _send.apply(dc, arguments); };
      dc.addEventListener("message", (ev) => {
        try {
          let inner = String(ev.data);
          const outer = JSON.parse(inner);
          if (outer && outer.type === "data_message" && typeof outer.data === "string") inner = outer.data;
          const e = JSON.parse(inner);
          const t = (e && (e.type || (e.payload && e.payload.type))) || "?";
          cap.events.push(t);
          // Surface anything transcription/response-shaped for the round-trip.
          const s = JSON.stringify(e);
          if (/transcript|response|text|new_state/i.test(s)) {
            cap.transcripts.push(s.slice(0, 400));
            window.dispatchEvent(new CustomEvent("gptlive-event", { detail: { type: t, raw: s } }));
          }
        } catch {}
      });
      return dc;
    };
    return pc;
  }
  W.prototype = _RTC.prototype;
  try { W.generateCertificate = _RTC.generateCertificate && _RTC.generateCertificate.bind(_RTC); } catch {}
  W.__hooked = true;
  window.RTCPeerConnection = W;
}

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: false, // set true once it works; voice UI is simpler to drive headed
  userDataDir: PROFILE,
  args: [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    `--use-file-for-fake-audio-capture=${AUDIO}`,
  ],
});

try {
  const page = (await browser.pages())[0] || (await browser.newPage());
  await page.evaluateOnNewDocument(pageHook);

  // Relay page events to Node + the Mode B hook.
  await page.exposeFunction("__onGptLive", async (detail) => {
    if (detail.type && /input_audio_transcription|transcription/i.test(detail.type)) {
      const m = detail.raw.match(/"transcript"\s*:\s*"([^"]+)"/);
      if (m) { const reply = await onAgentTurn(m[1]); if (reply) console.log(`[agent->speak] "${reply}"`); }
    }
  });

  await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => window.addEventListener("gptlive-event", (e) => window.__onGptLive(e.detail)));

  // Confirm login, then open the voice session (the "Use Voice" waveform button).
  console.log("[sidecar] page loaded; opening voice…");
  await page.waitForSelector("body");
  const clicked = await page.evaluate(() => {
    const btn = [...document.querySelectorAll("button")].find(
      (b) => /voice/i.test(b.getAttribute("aria-label") || "") || /voice/i.test(b.title || ""),
    );
    if (btn) { btn.click(); return true; }
    return false;
  });
  if (!clicked) console.log("[sidecar] voice button not found — is this profile logged in? Open voice manually.");

  console.log(`[sidecar] listening ${RUN_MS / 1000}s for transcription/response…`);
  await new Promise((r) => setTimeout(r, RUN_MS));

  const cap = await page.evaluate(() => window.__gptlive);
  console.log("\n=== datachannel event types ===", JSON.stringify([...new Set(cap.events)]));
  console.log("=== outbound (client protocol) ===");
  console.log([...new Set(cap.out.map((o) => { try { return JSON.parse(o).type; } catch { return o.slice(0, 40); } }))]);
  console.log("=== transcription / response events ===");
  cap.transcripts.slice(0, 20).forEach((t) => console.log("  " + t));
} finally {
  await browser.close();
}
