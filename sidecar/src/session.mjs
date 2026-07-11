// GPT-Live voice session orchestration.
//
// Wires the tested primitives (reconnect, liveness, events, export) to a
// WebRTC peer and the consumer adapter into the Mode B loop:
//
//   mic --(WebRTC)--> Live --(datachannel)--> input transcript
//        --> onUserSaid(text)  [the agent, e.g. Claude/gpt2agent, reasons]
//        --> speak(replyText)  --> Live voices it back
//
// Audio never leaves the media peer; only text crosses to the agent hook.

import { ReconnectPolicy } from "./reconnect.mjs";
import { LivenessMonitor } from "./liveness.mjs";
import {
  buildSpeakWire,
  buildSessionUpdate,
  wrapDataMessage,
} from "./events.mjs";
import { ModeBExport, ExportState } from "./export.mjs";
import * as adapter from "./adapter.mjs";

export const State = Object.freeze({
  IDLE: "idle",
  CONNECTING: "connecting",
  LIVE: "live",
  RECONNECTING: "reconnecting",
  CLOSED: "closed",
});

export class VoiceSession {
  /**
   * @param {{
   *   auth: {token:string, sentinel?:object},
   *   voice?: string,
   *   instructions?: string,
   *   voiceMode?: string,
   *   sessionType?: string,
   *   iceServers?: object[],
   *   createPeer: (iceServers:object[]) => object,
   *   getMicTrack: () => Promise<MediaStreamTrack>,
   *   onUserSaid: (text:string) => (void|string|Promise<void|string>),
   *   onStateChange?: (state:string) => void,
   *   reconnect?: object,
   *   livenessTimeoutMs?: number,
   *   exportPlane?: ModeBExport,
   * }} opts
   */
  constructor(opts) {
    this.opts = opts;
    this.state = State.IDLE;
    this.exportPlane =
      opts.exportPlane ??
      new ModeBExport({
        onAgentTurn: async (text) => {
          const r = await opts.onUserSaid?.(text);
          return typeof r === "string" ? r : null;
        },
      });
    this.router = this.exportPlane.router;
    this.liveness = new LivenessMonitor({ timeoutMs: opts.livenessTimeoutMs ?? 15_000 });
    this.reconnect = new ReconnectPolicy(opts.reconnect ?? { maxAttempts: 8 });
    this._pc = null;
    this._dc = null;

    // Keep liveness fresh on every transcript the export plane sees.
    this.router.onInputTranscript(() => {
      this.liveness.seen(Date.now());
    });
  }

  _setState(s) {
    this.state = s;
    if (s === State.LIVE) this.exportPlane.setState(ExportState.LIVE);
    if (s === State.CLOSED) this.exportPlane.setState(ExportState.CLOSED);
    if (s === State.IDLE) this.exportPlane.setState(ExportState.IDLE);
    this.opts.onStateChange?.(s);
  }

  /** Speak the agent's reply through GPT-Live (Mode B output). */
  speak(text) {
    if (!this._dc || this.state !== State.LIVE) {
      throw new Error(`cannot speak while ${this.state}`);
    }
    const wire = buildSpeakWire(text);
    this._dc.send(wire);
    this.exportPlane.record("agent", text);
    return wire;
  }

  /**
   * Connect (or reconnect). Uses the verified SDP-exchange adapter.
   * Note: persistent sessions still require a Turnstile-cleared browser path
   * for production use; see docs/roadmap and sidecar README.
   */
  async connect() {
    this._setState(this.state === State.IDLE ? State.CONNECTING : State.RECONNECTING);
    const pc = this.opts.createPeer(this.opts.iceServers ?? []);
    const track = await this.opts.getMicTrack();
    pc.addTrack(track);
    this._dc = pc.createDataChannel("", { negotiated: true, id: adapter.DATACHANNEL_ID });
    this._dc.addEventListener("message", (m) => {
      this.liveness.seen(Date.now());
      // Fire-and-forget async agent turn; speak replies when hook returns text.
      void this.exportPlane.handleInbound(m.data).then((result) => {
        for (const wire of result.speakWires) {
          if (this._dc && this.state === State.LIVE) this._dc.send(wire);
        }
      });
    });
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const url = adapter.realtimeUrl({
      mode: this.opts.voiceMode,
      sessionType: this.opts.sessionType,
    });
    const { answerSdp } = await adapter.exchangeSdp({
      url,
      token: this.opts.auth.token,
      offerSdp: offer.sdp,
    });
    await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    this._dc.send(
      wrapDataMessage(
        buildSessionUpdate({
          voice: this.opts.voice,
          instructions: this.opts.instructions,
        }),
      ),
    );
    this._pc = pc;
    this.reconnect.reset();
    this._setState(State.LIVE);
  }

  /** Compute the next reconnect delay after a drop, or null to give up. */
  planReconnect() {
    return this.reconnect.nextDelay();
  }

  /** Agent-safe transcript buffer (text only). */
  getTranscripts(opts) {
    return this.exportPlane.getTranscripts(opts);
  }

  /** Agent-safe status (no tokens/audio). */
  status() {
    return {
      sessionState: this.state,
      ...this.exportPlane.status(),
    };
  }

  close() {
    try {
      this._dc?.close();
      this._pc?.close();
    } finally {
      this.exportPlane.close();
      this._setState(State.CLOSED);
    }
  }
}
