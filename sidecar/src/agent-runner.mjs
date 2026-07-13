// agent-runner.mjs — run a shell coding-agent command with a HARD timeout.
//
// The timeout kills the whole process group and resolves IMMEDIATELY. A bare
// `child.kill()` + waiting on `close` is ineffective: `shell:true` spawns a shell
// whose grandchild (the real agent) keeps the stdio pipes open, so `close` never
// fires and the call hangs to completion anyway. `detached:true` makes the child a
// process-group leader so `process.kill(-pid)` reaps the whole tree.

import { spawn } from "node:child_process";

/**
 * @param {string} cmd   shell command (stdin = text, stdout = reply)
 * @param {string} text  the human utterance piped to stdin
 * @param {{timeoutMs?: number, maxOut?: number}} [opts]
 * @returns {Promise<string>} the reply, or "[agent timed out]" / "[agent spawn error]"
 */
export function runAgent(cmd, text, opts = {}) {
  const timeoutMs = opts.timeoutMs ?? 120_000;
  const maxOut = opts.maxOut ?? 1024 * 1024;
  return new Promise((resolve) => {
    let done = false;
    const finish = (reply) => {
      if (done) return;
      done = true;
      resolve(reply);
    };
    const c = spawn(cmd, { shell: true, detached: true, stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    const timer = setTimeout(() => {
      try {
        process.kill(-c.pid, "SIGKILL"); // kill the whole group
      } catch {
        try {
          c.kill("SIGKILL");
        } catch {
          /* already gone */
        }
      }
      finish("[agent timed out]"); // return NOW — do not wait for `close`
    }, timeoutMs);
    c.stdout.on("data", (d) => {
      if (out.length < maxOut) out += d.toString("utf8");
    });
    c.stderr.on("data", () => {});
    c.on("error", () => {
      clearTimeout(timer);
      finish("[agent spawn error]");
    });
    c.on("close", () => {
      clearTimeout(timer);
      finish(out.trim() || "[no reply]");
    });
    // A fast command (or one that ignores stdin) may close the pipe before we
    // write — swallow the async EPIPE instead of crashing the process.
    c.stdin.on("error", () => {});
    try {
      c.stdin.write(text);
      c.stdin.end();
    } catch {
      /* stdin may already be closed if spawn failed */
    }
  });
}
