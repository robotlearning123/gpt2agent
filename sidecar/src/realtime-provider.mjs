// RealtimeApiProvider — a HUMAN-FREE voice STT for tests.
//
// Why this exists: consumer GPT-Live transcribes ONLY real-microphone audio and
// rejects synthetic audio (verified 2026-07-11: fake-device + replaceTrack both
// egress real speech but get 0 transcription). So you cannot feed synthetic audio
// to consumer GPT-Live for automated STT testing. The OpenAI Realtime API has no
// such restriction — it transcribes synthetic audio fine (verified). This module
// is the voice test double: turn a synthetic utterance into a transcript with no
// human, no mic, no browser.
//
// GA protocol (verified against gpt-realtime-2.1-mini, 2026-07-11):
//   wss://api.openai.com/v1/realtime?model=...  (Bearer, NO OpenAI-Beta header)
//   session.update { session: { type:"realtime", audio: {
//       input:  { format:{type:"audio/pcm",rate:24000}, transcription:{model:"gpt-4o-transcribe"} },
//       output: { format:{type:"audio/pcm",rate:24000} } } } }
//   input_audio_buffer.append { audio: <base64 pcm16> }  (chunked)
//   input_audio_buffer.commit
//   <- conversation.item.input_audio_transcription.completed { transcript }

const REALTIME_DEFAULTS = { model: "gpt-realtime-2.1-mini", transcriptionModel: "gpt-4o-transcribe", rate: 24000, timeoutMs: 10000 };

/** Transcribe a PCM16-mono buffer (raw, little-endian) via the Realtime API. */
export function transcribePcm(pcm, opts = {}) {
  const KEY = opts.apiKey || process.env.OPENAI_API_KEY;
  if (!KEY) return Promise.reject(new Error("OPENAI_API_KEY required"));
  const model = opts.model || REALTIME_DEFAULTS.model;
  const transcriptionModel = opts.transcriptionModel || REALTIME_DEFAULTS.transcriptionModel;
  const rate = opts.rate || REALTIME_DEFAULTS.rate;
  const timeoutMs = opts.timeoutMs || REALTIME_DEFAULTS.timeoutMs;
  const b64 = Buffer.from(pcm).toString("base64");

  return new Promise((resolve, reject) => {
    let transcript = "";
    let done = false;
    const ws = new WebSocket(`wss://api.openai.com/v1/realtime?model=${model}`, { headers: { Authorization: `Bearer ${KEY}` } });
    const finish = (v) => { if (done) return; done = true; clearTimeout(to); try { ws.close(); } catch {}; resolve(v); };
    const to = setTimeout(() => finish(transcript), timeoutMs);
    ws.addEventListener("error", (e) => { if (!done) { done = true; clearTimeout(to); reject(new Error(e.message || "realtime ws error")); } });
    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({ type: "session.update", session: { type: "realtime", audio: {
        input: { format: { type: "audio/pcm", rate }, transcription: { model: transcriptionModel } },
        output: { format: { type: "audio/pcm", rate } },
      } } }));
      const step = 8000;
      for (let i = 0; i < b64.length; i += step)
        ws.send(JSON.stringify({ type: "input_audio_buffer.append", audio: b64.slice(i, i + step) }));
      ws.send(JSON.stringify({ type: "input_audio_buffer.commit" }));
    });
    ws.addEventListener("message", (ev) => {
      let e; try { e = JSON.parse(ev.data); } catch { return; }
      if (e.type === "conversation.item.input_audio_transcription.completed" && e.transcript) finish(e.transcript);
    });
  });
}

/** Synthesize a phrase to raw PCM16 24kHz mono via OpenAI Speech (test fixture, no human). */
export async function ttsToPcm(text, opts = {}) {
  const KEY = opts.apiKey || process.env.OPENAI_API_KEY;
  if (!KEY) throw new Error("OPENAI_API_KEY required");
  const r = await fetch("https://api.openai.com/v1/audio/speech", {
    method: "POST",
    headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ model: opts.ttsModel || "gpt-4o-mini-tts", voice: opts.voice || "alloy", input: text, response_format: "pcm" }),
  });
  if (!r.ok) throw new Error(`tts HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return Buffer.from(await r.arrayBuffer()); // raw PCM16 24kHz mono
}

/** Human-free voice STT round-trip: text → TTS → Realtime transcription → text. */
export async function transcribeText(text, opts = {}) {
  return transcribePcm(await ttsToPcm(text, opts), opts);
}
