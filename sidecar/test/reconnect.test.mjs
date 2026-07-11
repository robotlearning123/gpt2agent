import { test } from "node:test";
import assert from "node:assert/strict";
import { computeBackoff, ReconnectPolicy } from "../src/reconnect.mjs";

const noJitter = () => 1; // deterministic: returns the full capped exponential

test("computeBackoff grows exponentially from base", () => {
  const o = { baseMs: 500, factor: 2, maxMs: 1e9, jitter: noJitter };
  assert.equal(computeBackoff(1, o), 500);
  assert.equal(computeBackoff(2, o), 1000);
  assert.equal(computeBackoff(3, o), 2000);
  assert.equal(computeBackoff(4, o), 4000);
});

test("computeBackoff caps at maxMs", () => {
  const o = { baseMs: 500, factor: 2, maxMs: 3000, jitter: noJitter };
  assert.equal(computeBackoff(10, o), 3000);
});

test("jitter keeps delay within [0.5, 1.0] x exponential term", () => {
  const o = { baseMs: 1000, factor: 2, maxMs: 1e9 };
  for (const j of [0, 0.5, 1]) {
    const d = computeBackoff(3, { ...o, jitter: () => j });
    assert.ok(d >= 2000 && d <= 4000, `delay ${d} out of band`);
  }
  assert.equal(computeBackoff(3, { ...o, jitter: () => 0 }), 2000);
});

test("computeBackoff rejects non-positive attempts", () => {
  assert.throws(() => computeBackoff(0), RangeError);
  assert.throws(() => computeBackoff(-1), RangeError);
});

test("ReconnectPolicy stops after maxAttempts", () => {
  const p = new ReconnectPolicy({ maxAttempts: 3, jitter: noJitter });
  assert.equal(typeof p.nextDelay(), "number");
  assert.equal(typeof p.nextDelay(), "number");
  assert.equal(typeof p.nextDelay(), "number");
  assert.equal(p.nextDelay(), null);
  assert.ok(p.exhausted);
});

test("ReconnectPolicy.reset restarts the backoff sequence", () => {
  const p = new ReconnectPolicy({ baseMs: 500, factor: 2, maxMs: 1e9, jitter: noJitter });
  assert.equal(p.nextDelay(), 500);
  assert.equal(p.nextDelay(), 1000);
  p.reset();
  assert.equal(p.attempt, 0);
  assert.equal(p.nextDelay(), 500);
});
