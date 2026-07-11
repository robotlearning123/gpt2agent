// Spike: does the OpenAI Realtime API accept synthetic audio and return a
// transcript? If yes → it's a valid human-free voice source for tests.
// Run: node experiments/realtime-spike.mjs /tmp/spike.pcm
import { readFileSync } from "node:fs";

const KEY = process.env.OPENAI_API_KEY;
const MODEL = process.env.REALTIME_MODEL || "gpt-realtime-2.1-mini";
const PCM = process.argv[2] || "/tmp/spike.pcm";
if (!KEY) { console.error("OPENAI_API_KEY not set"); process.exit(1); }
const audio = readFileSync(PCM);
const b64 = audio.toString("base64");
console.log(`[spike] model=${MODEL} pcm=${audio.length}B (${(audio.length/2/24000).toFixed(1)}s)`);

const ws = new WebSocket(`wss://api.openai.com/v1/realtime?model=${MODEL}`, {
  headers: { Authorization: `Bearer ${KEY}` },
});

let inputTranscript = "";
const t0 = Date.now();
const finish = (code) => { console.log(`\n[spike] input_transcript=${JSON.stringify(inputTranscript)} (${Date.now()-t0}ms)`); try{ws.close()}catch{} process.exit(code); };
setTimeout(() => { console.log("[spike] timeout"); finish(2); }, 12000);

ws.addEventListener("open", () => {
  console.log("[spike] connected");
  ws.send(JSON.stringify({ type: "session.update", session: {
    type: "realtime",
    audio: {
      input: { format: { type: "audio/pcm", rate: 24000 }, transcription: { model: "gpt-4o-transcribe" } },
      output: { format: { type: "audio/pcm", rate: 24000 } },
    },
  } }));
  // append in ~8KB chunks
  const step = 8000;
  for (let i = 0; i < b64.length; i += step)
    ws.send(JSON.stringify({ type: "input_audio_buffer.append", audio: b64.slice(i, i + step) }));
  ws.send(JSON.stringify({ type: "input_audio_buffer.commit" }));
  console.log("[spike] audio sent + committed");
});
ws.addEventListener("message", (ev) => {
  let e; try { e = JSON.parse(ev.data); } catch { return; }
  const extra = e.transcript || e.text || (e.delta && e.delta.text) || (e.error && JSON.stringify(e.error)) || "";
  console.log(`  evt ${e.type}${extra ? " :: " + String(extra).slice(0,120) : ""}`);
  if (e.type === "conversation.item.input_audio_transcription.completed") inputTranscript = e.transcript || "";
  if (e.type === "error") { console.log("  ERROR:", JSON.stringify(e).slice(0,300)); }
  // finish once we have an input transcript (server VAD detected + transcribed)
  if (inputTranscript && Date.now() - t0 > 2500) finish(0);
});
ws.addEventListener("error", (e) => { console.log("[spike] ws error:", e.message || String(e).slice(0,200)); finish(1); });
