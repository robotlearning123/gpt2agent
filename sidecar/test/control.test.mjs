import { test } from "node:test";
import assert from "node:assert/strict";
import { ModeBExport, ExportState } from "../src/export.mjs";
import { createControlServer, EXPORT_HELP } from "../src/control.mjs";
import { DATA_MESSAGE } from "../src/events.mjs";

async function withServer(exportPlane, fn, opts = {}) {
  const ctl = await createControlServer(exportPlane, { port: 0, ...opts });
  try {
    return await fn(ctl);
  } finally {
    await new Promise((r) => ctl.server.close(r));
  }
}

test("control /help documents export and Turnstile boundary", async () => {
  const plane = new ModeBExport();
  await withServer(plane, async (ctl) => {
    const res = await fetch(`${ctl.url}/help`);
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(typeof body.summary, "string");
    assert.match(body.boundary.turnstile, /out of scope/i);
    assert.match(JSON.stringify(body), /send_text/);
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
    assert.equal(JSON.stringify(st).includes("Bearer"), false);

    const tx = await (await fetch(`${ctl.url}/transcript`)).json();
    assert.equal(tx.transcripts[0].text, "hello");
    assert.equal(tx.transcripts[0].role, "human");
  });
});

test("control POST /send_text queues speak wire and rejects empty", async () => {
  const delivered = [];
  const plane = new ModeBExport();
  await withServer(
    plane,
    async (ctl) => {
      const bad = await fetch(`${ctl.url}/send_text`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "" }),
      });
      assert.equal(bad.status, 400);

      const ok = await fetch(`${ctl.url}/send_text`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "agent voice reply" }),
      });
      assert.equal(ok.status, 200);
      const body = await ok.json();
      assert.equal(body.ok, true);
      assert.equal(body.delivered, true);
      assert.equal(delivered.length, 1);
      const outer = JSON.parse(delivered[0]);
      assert.equal(outer.type, DATA_MESSAGE);
      // Delivered path must leave speak queue empty (not held after send).
      assert.equal(plane.drainSpeakQueue().length, 0);
      // Response must not include the raw wire or audio.
      assert.equal("wire" in body, false);
      assert.equal("audio" in body, false);
    },
    {
      sendSpeak: async (wire) => {
        delivered.push(wire);
        return true;
      },
    },
  );
});

test("control POST /send_text does not drop prior undelivered wires on later success", async () => {
  // Repro from review: first sendSpeak=false queues LOST; second=true must not
  // drainSpeakQueue() and erase LOST while only delivering the second wire.
  let calls = 0;
  const plane = new ModeBExport();
  await withServer(
    plane,
    async (ctl) => {
      const first = await fetch(`${ctl.url}/send_text`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "first undelivered" }),
      });
      assert.equal((await first.json()).delivered, false);
      assert.equal(plane.status().speakQueueLength, 1);

      const second = await fetch(`${ctl.url}/send_text`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "second delivered" }),
      });
      assert.equal((await second.json()).delivered, true);

      // Prior undelivered speak must still be waiting — not wiped by success path.
      assert.equal(plane.status().speakQueueLength, 1);
      const remaining = plane.drainSpeakQueue();
      assert.equal(remaining.length, 1);
      const inner = JSON.parse(JSON.parse(remaining[0]).data);
      assert.equal(inner.response.instructions, "first undelivered");
    },
    {
      sendSpeak: async () => {
        calls += 1;
        return calls >= 2; // fail first, succeed second
      },
    },
  );
});

test("control POST /end closes export plane", async () => {
  const plane = new ModeBExport();
  let ended = false;
  await withServer(
    plane,
    async (ctl) => {
      const res = await fetch(`${ctl.url}/end`, { method: "POST" });
      assert.equal(res.status, 200);
      assert.equal(plane.state, ExportState.CLOSED);
      assert.equal(ended, true);
    },
    { onEnd: async () => { ended = true; } },
  );
});
