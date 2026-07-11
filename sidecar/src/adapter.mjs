// ChatGPT consumer GPT-Live adapter — routes VERIFIED from the shipped web
// bundle (chatgpt.com chunk 9a292b8a-…, 2026-07-11; see
// docs/…/2026-07-11-gpt-live-handshake-evidence.md). Not a guess: the endpoint
// builder, the single-shot SDP exchange, and the negotiated datachannel are read
// directly from ChatGPT's own client. What still needs a live round-trip is
// confirmation only (token source, ICE servers) — noted below.

/** Origin the realtime endpoints live on. */
export const ORIGIN = "https://chatgpt.com";

/** Negotiated datachannel id used by the client ( ?dcid=0 ). */
export const DATACHANNEL_ID = 0;

/**
 * Realtime voice path for a mode, mirroring the bundle's `voicePath`:
 *   standard -> /realtime/vps
 *   advanced -> /realtime/vp
 *   wingman  -> /realtime/wm
 * @param {string} [mode] catalog mode ("standard" | "advanced" | ...)
 * @param {string} [sessionType] "wm" for wingman, else the vp family
 */
export function voicePath(mode, sessionType) {
  const base = "/realtime";
  if (sessionType === "wm") return `${base}/wm`;
  return `${base}/vp${mode === "standard" ? "s" : ""}`;
}

/** Full SDP-exchange URL: `${origin}${voicePath}?dcid=<id>`. */
export function realtimeUrl({ origin = ORIGIN, mode, sessionType, dcid = DATACHANNEL_ID } = {}) {
  const params = new URLSearchParams({ dcid: String(dcid) });
  return `${origin}${voicePath(mode, sessionType)}?${params}`;
}

/**
 * Single-shot SDP exchange (verified pattern): POST the offer SDP as
 * `application/sdp` with the account bearer; the response body is the answer SDP.
 * The server mints the session from the authenticated POST — no separate
 * bootstrap call for the core voice path.
 *
 * @param {{url:string, token:string, offerSdp:string,
 *          extraHeaders?:Record<string,string>, fetchImpl?:typeof fetch}} args
 * @returns {Promise<{answerSdp:string}>}
 */
export async function exchangeSdp({ url, token, offerSdp, extraHeaders = {}, fetchImpl }) {
  const doFetch = fetchImpl ?? globalThis.fetch;
  if (typeof doFetch !== "function") throw new Error("no fetch implementation available");
  if (!token) throw new Error("exchangeSdp requires an account bearer token");
  if (typeof offerSdp !== "string" || !offerSdp) throw new Error("exchangeSdp requires an offer SDP");

  const res = await doFetch(url, {
    method: "POST",
    body: offerSdp,
    headers: {
      "Content-Type": "application/sdp",
      Authorization: `Bearer ${token}`,
      ...extraHeaders,
    },
  });
  if (!res.ok) throw new Error(`SDP exchange failed: HTTP ${res.status}`);
  const answerSdp = await res.text();
  if (!answerSdp || !answerSdp.trim()) throw new Error("SDP exchange returned an empty answer");
  return { answerSdp };
}

// Confirmation-only (routes above are verified from shipped code):
//  - token source: appears to be the account bearer (~/.codex/auth.json), so the
//    sidecar can exchange SDP without a browser — confirm with one live POST.
//  - iceServers: server-provided or default STUN (not literal in the bundle).
export const CAPTURED = true;
export const NEEDS_LIVE_CONFIRMATION = Object.freeze(["token_source", "ice_servers", "live_mode_selector"]);
