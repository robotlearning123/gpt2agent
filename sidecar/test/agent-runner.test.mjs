import { test } from "node:test";
import assert from "node:assert/strict";
import { runAgent } from "../src/agent-runner.mjs";

test("runAgent returns the command stdout", async () => {
  const reply = await runAgent("printf 'hello world'", "ignored-stdin", { timeoutMs: 5000 });
  assert.equal(reply, "hello world");
});

test("runAgent enforces the timeout and returns promptly (kills the whole tree)", async () => {
  // Regression: a `sleep 5` under `shell:true` must NOT block the call for 5s.
  const t0 = Date.now();
  const reply = await runAgent("sleep 5", "x", { timeoutMs: 200 });
  const dt = Date.now() - t0;
  assert.equal(reply, "[agent timed out]");
  assert.ok(dt < 1500, `expected prompt timeout, took ${dt}ms`);
});

test("runAgent resolves (never throws) when the command exits non-zero with no output", async () => {
  const reply = await runAgent("this-command-does-not-exist-xyz 2>/dev/null; exit 127", "x", {
    timeoutMs: 3000,
  });
  // Shell exit 127, empty stdout → "[no reply]"; the call resolves, never throws.
  assert.equal(reply, "[no reply]");
});
