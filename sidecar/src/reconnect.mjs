// Reconnect backoff policy for the GPT-Live WebRTC session.
//
// WebRTC sessions drop (network changes, ICE failures, server resets). "Stable
// and reliable" starts here: bounded exponential backoff with jitter, a hard
// attempt ceiling, and a reset on every clean connect. Pure and deterministic
// (inject `jitter`) so it is unit-testable without timers or a network.

/**
 * @param {number} attempt 1-based attempt number.
 * @param {{baseMs?:number,maxMs?:number,factor?:number,jitter?:()=>number}} [opts]
 * @returns {number} delay in ms, in [0.5, 1.0] x the capped exponential term.
 */
export function computeBackoff(attempt, opts = {}) {
  const { baseMs = 500, maxMs = 30_000, factor = 2, jitter = Math.random } = opts;
  if (!Number.isInteger(attempt) || attempt < 1) {
    throw new RangeError("attempt must be a positive integer");
  }
  const exp = Math.min(maxMs, baseMs * factor ** (attempt - 1));
  // Full-ish jitter: spread within the lower half so retries never synchronize
  // into a thundering herd, but never collapse to ~0.
  return Math.round(exp * (0.5 + 0.5 * jitter()));
}

export class ReconnectPolicy {
  /** @param {{maxAttempts?:number,baseMs?:number,maxMs?:number,factor?:number,jitter?:()=>number}} [opts] */
  constructor(opts = {}) {
    const { maxAttempts = Infinity, ...backoff } = opts;
    this.maxAttempts = maxAttempts;
    this._backoff = backoff;
    this.attempt = 0;
  }

  /** @returns {number|null} delay in ms, or null once the attempt ceiling is passed. */
  nextDelay() {
    this.attempt += 1;
    if (this.attempt > this.maxAttempts) return null;
    return computeBackoff(this.attempt, this._backoff);
  }

  /** Call after a clean connect so the next drop starts from base again. */
  reset() {
    this.attempt = 0;
  }

  get exhausted() {
    return this.attempt >= this.maxAttempts;
  }
}
