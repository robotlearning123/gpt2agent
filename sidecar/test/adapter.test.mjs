import { test } from "node:test";
import assert from "node:assert/strict";
import { voicePath, realtimeUrl, exchangeSdp, ORIGIN } from "../src/adapter.mjs";

test("voicePath matches the bundle's route builder", () => {
  assert.equal(voicePath("standard"), "/realtime/vps");
  assert.equal(voicePath("advanced"), "/realtime/vp");
  assert.equal(voicePath(undefined), "/realtime/vp");
  assert.equal(voicePath("advanced", "wm"), "/realtime/wm");
});

test("realtimeUrl builds origin + path + dcid", () => {
  assert.equal(realtimeUrl({ mode: "advanced" }), `${ORIGIN}/realtime/vp?dcid=0`);
  assert.equal(realtimeUrl({ mode: "standard" }), `${ORIGIN}/realtime/vps?dcid=0`);
  assert.equal(realtimeUrl({ sessionType: "wm", dcid: 3 }), `${ORIGIN}/realtime/wm?dcid=3`);
  assert.equal(
    realtimeUrl({ origin: "https://example.test", mode: "advanced" }),
    "https://example.test/realtime/vp?dcid=0",
  );
});

test("exchangeSdp POSTs the offer as application/sdp with a bearer and returns the answer", async () => {
  let captured;
  const fakeFetch = async (url, init) => {
    captured = { url, init };
    return { ok: true, text: async () => "v=0\r\n(answer sdp)" };
  };
  const { answerSdp } = await exchangeSdp({
    url: realtimeUrl({ mode: "advanced" }),
    token: "tok-123",
    offerSdp: "v=0\r\n(offer sdp)",
    fetchImpl: fakeFetch,
  });
  assert.equal(answerSdp, "v=0\r\n(answer sdp)");
  assert.equal(captured.init.method, "POST");
  assert.equal(captured.init.body, "v=0\r\n(offer sdp)");
  assert.equal(captured.init.headers["Content-Type"], "application/sdp");
  assert.equal(captured.init.headers.Authorization, "Bearer tok-123");
});

test("exchangeSdp surfaces a non-2xx as an error", async () => {
  const fakeFetch = async () => ({ ok: false, status: 403, text: async () => "" });
  await assert.rejects(
    exchangeSdp({ url: "x", token: "t", offerSdp: "o", fetchImpl: fakeFetch }),
    /HTTP 403/,
  );
});

test("exchangeSdp rejects an empty answer and missing inputs", async () => {
  const emptyFetch = async () => ({ ok: true, text: async () => "   " });
  await assert.rejects(
    exchangeSdp({ url: "x", token: "t", offerSdp: "o", fetchImpl: emptyFetch }),
    /empty answer/,
  );
  await assert.rejects(exchangeSdp({ url: "x", token: "", offerSdp: "o", fetchImpl: emptyFetch }), /bearer token/);
  await assert.rejects(exchangeSdp({ url: "x", token: "t", offerSdp: "", fetchImpl: emptyFetch }), /offer SDP/);
});
