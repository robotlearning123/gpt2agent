// VoiceProvider — the abstraction that lets us test the voice→agent loop HUMAN-FREE
// while shipping with the best voice (consumer GPT-Live).
//
// Contract: a provider produces HUMAN transcript text from audio. Production uses
// ConsumerGptLiveVoiceProvider (real mic, the irreplaceable GPT-Live voice, but
// rejects synthetic audio so it needs a human). Tests use RealtimeVoiceProvider
// (OpenAI Realtime API, accepts synthetic audio → fully human-free).
//
// The agent-wiring that consumes the transcript is identical either way, which is
// why the voice→agent loop can be regression-tested without a human.

/**
 * @typedef {(text: string) => void} OnTranscript
 * @typedef {{ start: () => Promise<void>, onTranscript: (cb: OnTranscript) => void, feed?: (pcm: Buffer) => Promise<void>, stop: () => Promise<void> }} VoiceProvider
 */

/** RealtimeVoiceProvider — human-free STT test double (OpenAI Realtime API). */
export class RealtimeVoiceProvider {
  /**
   * @param {{apiKey?: string, model?: string, transcriptionModel?: string}} [opts]
   */
  constructor(opts = {}) {
    this.opts = opts;
    this._cbs = new Set();
    this._ws = null;
  }
  async start() { /* lazy: connect per feed so each utterance is a clean turn */ }
  onTranscript(cb) { this._cbs.add(cb); }
  /** Feed raw PCM16 mono audio (synthetic) → emits the transcript to callbacks. */
  async feed(pcm) {
    const { transcribePcm } = await import("./realtime-provider.mjs");
    const t = await transcribePcm(pcm, this.opts);
    if (t) for (const cb of this._cbs) cb(t);
    return t;
  }
  /** Convenience: synthesize a phrase → transcribe (full human-free round-trip). */
  async feedText(phrase) {
    const { ttsToPcm } = await import("./realtime-provider.mjs");
    return this.feed(await ttsToPcm(phrase, this.opts));
  }
  async stop() { try { this._ws && this._ws.close(); } catch {} }
}

/**
 * ConsumerGptLiveVoiceProvider — production voice (real mic, consumer GPT-Live).
 * Wraps the CDP bridge: hooks RTCPeerConnection, reconstructs human utterances from
 * the real datachannel protocol (chat_message_delta, direction:"in"). Needs a human
 * speaker (GPT-Live rejects synthetic audio — see protocol spec §10).
 *
 * Wired by experiments/voice-to-agent.mjs / the extension hook; this class is the
 * shared seam so the agent loop is provider-agnostic.
 */
export class ConsumerGptLiveVoiceProvider {
  /**
   * @param {{ page: import("puppeteer-core").Page }} opts — a CDP page with the
   *   voice datachannel hook installed (exposes window.__utterances or posts via
   *   exposeFunction). Audio stays in the browser; only transcript text crosses.
   */
  constructor(opts) { this.opts = opts; this._cbs = new Set(); this._poll = null; }
  async start() {
    const { page } = this.opts;
    // Drain completed human utterances the hook parked on the page.
    this._poll = setInterval(async () => {
      try {
        const us = await page.evaluate(() => { const a = window.__utterances || []; window.__utterances = []; return a; });
        for (const u of us || []) for (const cb of this._cbs) cb(u);
      } catch {}
    }, 400);
  }
  onTranscript(cb) { this._cbs.add(cb); }
  async stop() { if (this._poll) clearInterval(this._poll); }
}
