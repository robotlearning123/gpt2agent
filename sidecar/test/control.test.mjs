import { test } from "node:test";
import assert from "node:assert/strict";
import { ModeBExport, ExportState } from "../src/export.mjs";
import { createControlServer, EXPORT_HELP } from "../src/control.mjs";

async function withServer(exportPlane, fn, opts = {}) {
  const ctl = await createControlServer(exportPlane, { port: 0, ...opts });
  try {
    return await fn(ctl);
  } finally {
    await new Promise((r) => ctl.server.close(r));
  }
}

test("control /help documents the human→agent bridge and boundary, not a speak route", async () => {
  const plane = new ModeBExport();
  await withServer(plane, async (ctl) => {
    const res = await fetch(`${ctl.url}/help`);
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(typeof body.summary, "string");
    assert.match(body.boundary.turnstile, /out of scope/i);
    assert.match(body.boundary.direction, /human/i);
    // No speak route is advertised anywhere in help.
    assert.equal(/send_text/.test(JSON.stringify(body)), false);
    assert.equal("send_text" in body.tools, false);
    assert.equal(EXPORT_HELP.boundary.audio.includes("never"), true);
  });
});

test("control /status and /transcript are text-only", async () => {
  const plane = new ModeBExport();
  plane.setState(ExportState.LIVE);
  plane.record("human", "hello");
  await withServer(plane, async (ctl) => {
    const st = await (await fetch(`${ctl.url}/status`)).json();
    assert.equal(st.state, ExportState.LIVE);
    assert.equal(st.boundary.audioCrossesBoundary, false);
    assert.equal(st.boundary.speakInjection, "unsupported (server drops it)");
    assert.equal(JSON.stringify(st).includes("Bearer"), false);

    const tx = await (await fetch(`${ctl.url}/transcript`)).json();
    assert.equal(tx.transcripts[0].text, "hello");
    assert.equal(tx.transcripts[0].role, "human");
  });
});

test("there is no /send_text route (agent→Live write channel removed)", async () => {
  const plane = new ModeBExport();
  await withServer(plane, async (ctl) => {
    const res = await fetch(`${ctl.url}/send_text`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: "speak this" }),
    });
    assert.equal(res.status, 404);
  });
});

test("control /transcript?clear=1 drains the buffer", async () => {
  const plane = new ModeBExport();
  plane.record("human", "one");
  await withServer(plane, async (ctl) => {
    const first = await (await fetch(`${ctl.url}/transcript?clear=1`)).json();
    assert.equal(first.transcripts.length, 1);
    const second = await (await fetch(`${ctl.url}/transcript`)).json();
    assert.equal(second.transcripts.length, 0);
  });
});

test("control POST /end closes the bridge and runs onEnd", async () => {
  const plane = new ModeBExport();
  let ended = false;
  await withServer(
    plane,
    async (ctl) => {
      const res = await fetch(`${ctl.url}/end`, { method: "POST" });
      assert.equal(res.status, 200);
      assert.equal(plane.state, ExportState.CLOSED);
      // onEnd runs deferred (after the response) — poll briefly.
      for (let i = 0; i < 50 && !ended; i++) await new Promise((r) => setTimeout(r, 5));
      assert.equal(ended, true);
    },
    { onEnd: async () => { ended = true; } },
  );
});

test("POST /end does not deadlock when onEnd closes the server", async () => {
  // Regression: onEnd closing THIS server must not deadlock on the /end response.
  const plane = new ModeBExport();
  const ctl = await createControlServer(plane, {
    port: 0,
    onEnd: () => new Promise((r) => ctl.server.close(r)),
  });
  const result = await Promise.race([
    fetch(`${ctl.url}/end`, { method: "POST" }).then((r) => r.status),
    new Promise((r) => setTimeout(() => r("timeout"), 2000)),
  ]);
  assert.equal(result, 200);
});
