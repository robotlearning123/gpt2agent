// Datachannel liveness / half-open detection for the GPT-Live session.
//
// A WebRTC datachannel can go silent without a close event (half-open: the peer
// vanished but the local ICE agent hasn't timed out yet). We treat "no inbound
// event for longer than timeoutMs" as dead and let the session tear down and
// reconnect. Pure logic with an injected clock so it is unit-testable; the
// timer wiring lives in session.mjs.

export class LivenessMonitor {
  /** @param {{timeoutMs?:number}} [opts] */
  constructor(opts = {}) {
    const { timeoutMs = 15_000 } = opts;
    if (!(timeoutMs > 0)) throw new RangeError("timeoutMs must be > 0");
    this.timeoutMs = timeoutMs;
    /** @type {number|null} */
    this.lastSeen = null;
  }

  /** Record inbound activity (any datachannel message / pong) at nowMs. */
  seen(nowMs) {
    this.lastSeen = nowMs;
  }

  /** @returns {number|null} ms since last inbound activity, or null if none yet. */
  msSinceSeen(nowMs) {
    return this.lastSeen === null ? null : nowMs - this.lastSeen;
  }

  /**
   * Dead only once we've seen activity and then gone quiet past the timeout.
   * Before the first `seen()` we report alive so startup isn't killed early.
   * @returns {boolean}
   */
  isDead(nowMs) {
    return this.lastSeen !== null && nowMs - this.lastSeen > this.timeoutMs;
  }
}
