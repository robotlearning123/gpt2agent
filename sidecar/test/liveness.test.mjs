import { test } from "node:test";
import assert from "node:assert/strict";
import { LivenessMonitor } from "../src/liveness.mjs";

test("fresh monitor is not dead before any activity", () => {
  const m = new LivenessMonitor({ timeoutMs: 100 });
  assert.equal(m.isDead(1_000_000), false);
  assert.equal(m.msSinceSeen(1_000_000), null);
});

test("dead once inbound activity goes quiet past the timeout", () => {
  const m = new LivenessMonitor({ timeoutMs: 100 });
  m.seen(1000);
  assert.equal(m.isDead(1099), false); // within timeout
  assert.equal(m.isDead(1100), false); // exactly at timeout is still alive
  assert.equal(m.isDead(1101), true); // past timeout -> dead
  assert.equal(m.msSinceSeen(1101), 101);
});

test("a fresh event revives the monitor", () => {
  const m = new LivenessMonitor({ timeoutMs: 100 });
  m.seen(1000);
  assert.equal(m.isDead(1200), true);
  m.seen(1200);
  assert.equal(m.isDead(1250), false);
});

test("rejects a non-positive timeout", () => {
  assert.throws(() => new LivenessMonitor({ timeoutMs: 0 }), RangeError);
});
