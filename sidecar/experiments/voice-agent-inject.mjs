// Human-free voice test harness (T2). Replaces the human speaker by INJECTING TTS
// audio directly into GPT-Live's WebRTC audio track (RTCRtpSender.replaceTrack),
// bypassing the fake-device path that the server refuses to transcribe.
//
// Flow: serve a TTS WAV → CDP-inject a hook that (a) replaces the mic audio
// sender's track with a looped AudioBuffer of the WAV, (b) assembles human
// utterances from chat_message_delta. Live hears the TTS (real speech → Opus,
// same wire as a real mic) → transcribes → our agent → reply overlay. No human.
//
//   node experiments/voice-agent-inject.mjs [--wav /tmp/inject_prompt.wav] [--once]
import puppeteer from "puppeteer-core";
import http from "node:http";
import { readFileSync } from "node:fs";
import { spawn } from "node:child_process";

const CDP = "http://127.0.0.1:9333";
const PORT = 8743;
const WAV = process.argv.slice(2).reduce((a, x, i, arr) => (arr[i - 1] === "--wav" ? x : a), "/tmp/inject_prompt.wav");
const ONCE = process.argv.includes("--once");
const AGENT = process.env.AGENT_CMD || "claude -p";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function runAgent(text) {
  return new Promise((res) => {
    const t0 = Date.now(); const c = spawn(AGENT, { shell: true, stdio: ["pipe", "pipe", "pipe"] });
    let out = ""; c.stdout.on("data", (d) => (out += d)); c.on("error", () => res(null)); c.on("close", () => res({ reply: out.trim(), ms: Date.now() - t0 }));
    c.stdin.write(text); c.stdin.end();
  });
}

// serve the WAV to the page (fetch + decodeAudioData)
const wavBuf = readFileSync(WAV);
http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "audio/wav", "Access-Control-Allow-Origin": "*", "Content-Length": wavBuf.length });
  res.end(wavBuf);
}).listen(PORT, "127.0.0.1");

const PAGE_HOOK = `
(wavB64) => {
  window.__pcs = []; window.__src = null; window.__replaced = false; window.__wavB64 = wavB64;
  try { window.__on({ sys: "hook" }); } catch {}
  const msgs = {}, order = [];
  function feed(inner){
    if(!inner || inner.type!=="chat_message_delta") return [];
    const d=(inner.payload||inner).delta||{}; const out=[];
    if(d.o==="add" && d.v && d.v.message){ const m=d.v.message, mid=m.id;
      if(mid && !msgs[mid]){ let dir=null,txt=""; for(const p of (m.content&&m.content.parts)||[]) if(p&&p.direction){dir=p.direction; txt=p.text||"";} msgs[mid]={dir,text:txt,done:false}; order.push(mid);} }
    const last=order[order.length-1]; if(!last) return out;
    if(Array.isArray(d.v)) for(const op of d.v){ if(op.o==="append"&&op.p==="/message/content/parts/0/text"&&msgs[last]) msgs[last].text+=op.v||"";
      if(op.o==="replace"&&op.p==="/message/status"&&op.v==="finished_successfully"){ const m=msgs[last]; if(m&&!m.done){m.done=true; if(m.dir==="in"){const t=(m.text||"").trim(); if(t) out.push(t);}} } }
    if(d.o==="replace"&&d.p==="/message/status"&&d.v==="finished_successfully"){ const m=msgs[last]; if(m&&!m.done){m.done=true; if(m.dir==="in"){const t=(m.text||"").trim(); if(t) out.push(t);}} }
    return out;
  }
  async function makeTrack(){ const ctx=new AudioContext(); if(ctx.state==="suspended"){try{await ctx.resume()}catch{}}
    try{window.__on({sys:"ctx", state:ctx.state})}catch{}
    const bin=atob(window.__wavB64); const ab=new ArrayBuffer(bin.length); const u8=new Uint8Array(ab); for(let i=0;i<bin.length;i++)u8[i]=bin.charCodeAt(i);
    const buf=await ctx.decodeAudioData(ab);
    const dest=ctx.createMediaStreamDestination(); const src=ctx.createBufferSource(); src.buffer=buf; src.loop=true; src.connect(dest); src.start();
    const an=ctx.createAnalyser(); src.connect(an);
    setTimeout(()=>{ try{ const d=new Uint8Array(an.fftSize); an.getByteTimeDomainData(d); let peak=0; for(const v of d){const x=Math.abs(v-128)/128; if(x>peak)peak=x;} window.__on({sys:"amp", peak:+peak.toFixed(3), dur:buf.duration}); }catch{} }, 600);
    window.__src=src; return dest.stream.getAudioTracks()[0]; }
  const _RTC=window.RTCPeerConnection; if(!_RTC||_RTC.__inj) return;
  function W(cfg){ const pc=new _RTC(cfg); window.__pcs.push(pc);
    try { window.__on({ sys: "pc", n: window.__pcs.length, trs: pc.getTransceivers ? pc.getTransceivers().length : -1 }); } catch {}
    const _cdc=pc.createDataChannel.bind(pc);
    pc.createDataChannel=function(label,opts){ const dc=_cdc(label,opts);
      dc.addEventListener("message", ev=>{ try{ let o=JSON.parse(String(ev.data)); let inner=o&&o.type==="data_message"&&typeof o.data==="string"?JSON.parse(o.data):o;
        const us=feed(inner); for(const u of us){ window.__on({utterance:u}); if(window.__src){try{window.__src.stop();window.__src=null;}catch{}} } }catch{} });
      return dc; };
    return pc; }
  W.prototype=_RTC.prototype; try{W.generateCertificate=_RTC.generateCertificate&&_RTC.generateCertificate.bind(_RTC);}catch{}
  _RTC.__inj=true; window.RTCPeerConnection=W;
  setInterval(()=>{ if(window.__replaced) return;
    try { window.__on({ sys: "poll", pcs: window.__pcs.length, trs: window.__pcs.reduce((s,pc)=>{try{return s+pc.getTransceivers().length}catch{return s}},0) }); } catch {}
    for(const pc of window.__pcs){ try{
      for(const tr of pc.getTransceivers()){
        const isAudio = (tr.receiver&&tr.receiver.track&&tr.receiver.track.kind==="audio") || (tr.sender&&tr.sender.track&&tr.sender.track.kind==="audio");
        if(isAudio && tr.sender){ makeTrack().then(t=>tr.sender.replaceTrack(t).then(()=>{window.__replaced=true; window.__on({sys:"inject", same: tr.sender.track===t, kind: tr.sender.track&&tr.sender.track.kind, senderTrackId: tr.sender.track&&tr.sender.track.id, injectedId: t.id});})).catch(e=>window.__on({sys:"inject_err",e:String(e&&e.message||e)})); return; }
      }
    }catch{} } }
  , 400);
}`;

const wavB64 = readFileSync(WAV).toString("base64");
const browser = await puppeteer.connect({ browserURL: CDP, defaultViewport: null });
const page = await browser.newPage();
await page.evaluateOnNewDocument((code) => { eval(code); }, `(${PAGE_HOOK})(${JSON.stringify(wavB64)})`);
let got = 0;
await page.exposeFunction("__on", async (d) => {
  if (d.sys) {
    console.log(`[${d.sys}] ${JSON.stringify(d)}`);
    return;
  }
  if (d.utterance == null) return;
  console.log(`\n[human] ${d.utterance}\n[agent] invoking ${AGENT}`);
  got++;
  const r = await runAgent(d.utterance);
  console.log(`[agent ${r?.ms}ms] ${(r?.reply || "").slice(0, 400)}`);
  try { await page.evaluate((t) => { let el = document.getElementById("__ov"); if (!el) { el = document.createElement("div"); el.id = "__ov"; el.style.cssText = "position:fixed;right:14px;bottom:14px;max-width:440px;max-height:45vh;overflow:auto;z-index:2147483647;background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:10px;padding:12px;font:13px/1.45 ui-monospace,monospace;white-space:pre-wrap"; document.documentElement.appendChild(el); } el.textContent = "🤖 coding agent:\\n\\n" + t; }, r?.reply || ""); } catch {}
});

console.log(`[harness] serving ${WAV} on :${PORT}; opening chatgpt + auto-starting voice…`);
await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
await page.bringToFront();
for (let i = 0; i < 10; i++) {
  await page.evaluate(() => { const b = [...document.querySelectorAll("button")].find((x) => /voice|speech|composer-speech/i.test(`${x.getAttribute("aria-label") || ""} ${x.title || ""} ${x.getAttribute("data-testid") || ""}`)); if (b) b.click(); const w = /start voice|continue|got it|begin/i; [...document.querySelectorAll("button")].forEach((b) => { if (w.test(`${b.getAttribute("aria-label") || ""} ${(b.textContent || "").trim()}`)) b.click(); }); });
  await sleep(2500);
}
console.log("[harness] voice started; waiting for transcription (no human)…");
for (let i = 0; i < 25; i++) {
  try {
    const st = await page.evaluate(async () => {
      const out = [];
      for (const pc of window.__pcs || []) {
        try { const s = await pc.getStats(); let p = null, b = null;
          s.forEach((r) => { if (r.type === "outbound-rtp" && r.kind === "audio") { p = r.packetsSent; b = r.bytesSent; } });
          out.push({ p, b });
        } catch {}
      }
      return out;
    }).catch(() => null);
    if (st) console.log(`[getStats] outbound-rtp audio per-pc: ${JSON.stringify(st)}`);
  } catch {}
  if (got > 0 && (!ONCE || got >= 1)) { await sleep(2000); break; }
  await sleep(2000);
}
console.log(`[harness] done. utterances captured: ${got}`);
await browser.disconnect();
