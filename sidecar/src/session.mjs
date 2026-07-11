// GPT-Live voice session orchestration (skeleton).
//
// Wires the tested primitives (reconnect, liveness, events) to a WebRTC peer and
// the consumer adapter into the Mode B loop:
//
//   mic --(WebRTC)--> Live --(datachannel)--> input transcript
//        --> onUserSaid(text)  [the agent, e.g. Claude/gpt2agent, reasons]
//        --> speak(replyText)  --> Live voices it back
//
// This file is orchestration, not a leaf unit: running it needs a WebRTC
// implementation (browser context, or node `werift`/`wrtc`) AND the captured
// adapter routes. It is intentionally thin so the substance stays in the
// unit-tested modules. States and wiring are real; the connect path throws via
// the adapter until the handshake is captured.

import { ReconnectPolicy } from "./reconnect.mjs";
import { LivenessMonitor } from "./liveness.mjs";
import { EventRouter, buildSpeakText, buildSessionUpdate } from "./events.mjs";
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
   *   createPeer: (iceServers:object[]) => object,  // returns an RTCPeerConnection-like
   *   getMicTrack: () => Promise<MediaStreamTrack>,  // real mic at the human's machine
   *   onUserSaid: (text:string) => void,             // hand transcript to the agent
   *   onStateChange?: (state:string) => void,
   *   reconnect?: object,
   *   livenessTimeoutMs?: number,
   * }} opts
   */
  constructor(opts) {
    this.opts = opts;
    this.state = State.IDLE;
    this.router = new EventRouter();
    this.liveness = new LivenessMonitor({ timeoutMs: opts.livenessTimeoutMs ?? 15_000 });
    this.reconnect = new ReconnectPolicy(opts.reconnect ?? { maxAttempts: 8 });
    this._pc = null;
    this._dc = null;

    this.router.onInputTranscript((text) => {
      this.liveness.seen(Date.now());
      opts.onUserSaid(text); // Mode B: the agent is the brain.
    });
  }

  _setState(s) {
    this.state = s;
    this.opts.onStateChange?.(s);
  }

  /** Speak the agent's reply through GPT-Live (Mode B output). */
  speak(text) {
    if (!this._dc || this.state !== State.LIVE) {
      throw new Error(`cannot speak while ${this.state}`);
    }
    this._dc.send(JSON.stringify(buildSpeakText(text)));
  }

  /**
   * Connect (or reconnect). Throws NotYetCapturedError from the adapter until the
   * consumer handshake is captured — honest failure, not a fake endpoint.
   */
  async connect() {
    this._setState(this.state === State.IDLE ? State.CONNECTING : State.RECONNECTING);
    const boot = await adapter.bootstrapSession(this.opts.auth); // throws until captured
    const pc = this.opts.createPeer(boot.iceServers);
    const track = await this.opts.getMicTrack();
    pc.addTrack(track);
    this._dc = pc.createDataChannel("oai-events");
    this._dc.addEventListener("message", (m) => {
      this.liveness.seen(Date.now());
      this.router.handle(m.data);
    });
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const { answerSdp } = await adapter.exchangeSdp({
      sdpUrl: boot.sdpUrl,
      clientSecret: boot.clientSecret,
      offerSdp: offer.sdp,
    });
    await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    this._dc.send(JSON.stringify(buildSessionUpdate({ voice: this.opts.voice, instructions: this.opts.instructions })));
    this._pc = pc;
    this.reconnect.reset();
    this._setState(State.LIVE);
  }

  /** Compute the next reconnect delay after a drop, or null to give up. */
  planReconnect() {
    return this.reconnect.nextDelay();
  }

  close() {
    try {
      this._dc?.close();
      this._pc?.close();
    } finally {
      this._setState(State.CLOSED);
    }
  }
}
