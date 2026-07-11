// ChatGPT consumer GPT-Live adapter — THE un-verified seam.
//
// Everything else in this sidecar is provider-agnostic and unit-tested. This
// file is the one place that speaks the private chatgpt.com routes, and it is
// deliberately isolated so filling it in from a real capture never destabilizes
// the tested core.
//
// STATUS: the two routes below are NOT yet captured (see
// sidecar/capture/gpt-live-capture.js and the investigation doc). The shapes
// follow the OpenAI Realtime API WebRTC handshake, which the consumer product is
// built on; confirm the exact paths from a capture, then replace the throwing
// stubs. Until then, callers get a clear, honest failure — never a fabricated
// endpoint pretending to work.

export class NotYetCapturedError extends Error {
  constructor(what) {
    super(
      `GPT-Live ${what} route is not captured yet. Run ` +
        `sidecar/capture/gpt-live-capture.js in an authenticated ChatGPT voice ` +
        `session and fill in adapter.mjs from the recorded routes.`,
    );
    this.name = "NotYetCapturedError";
  }
}

/**
 * Mint an ephemeral realtime session from the signed-in consumer account.
 * Must reuse the same bearer token gpt2agent already loads
 * (~/.codex/auth.json / ~/.gpt2agent/token.json) and pass the Sentinel
 * challenge headers the account routes require.
 *
 * @param {{token:string, sentinel?:object}} _auth
 * @returns {Promise<{clientSecret:string, sdpUrl:string, iceServers:object[]}>}
 */
// eslint-disable-next-line no-unused-vars
export async function bootstrapSession(_auth) {
  // TODO(capture): POST <bootstrap route> with the account bearer + Sentinel;
  // read back the ephemeral secret, the SDP-exchange URL, and ICE servers.
  throw new NotYetCapturedError("session bootstrap");
}

/**
 * Exchange the local SDP offer for the remote answer.
 * Realtime API does this as an HTTP POST of the SDP; the consumer route may use
 * a WebSocket signaling channel instead — capture decides.
 *
 * @param {{sdpUrl:string, clientSecret:string, offerSdp:string}} _args
 * @returns {Promise<{answerSdp:string}>}
 */
// eslint-disable-next-line no-unused-vars
export async function exchangeSdp(_args) {
  // TODO(capture): POST offerSdp to sdpUrl with the ephemeral secret; return the
  // SDP answer body.
  throw new NotYetCapturedError("SDP exchange");
}

/** True once the routes above have been filled in from a real capture. */
export const CAPTURED = false;
