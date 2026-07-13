// agent-gateway.mjs — the bridge layer's AGENT ADAPTER + control plane (③ + ② in
// docs/superpowers/plans/2026-07-11-gpt-live-bridge-layer-spec.md).
//
// The Chrome extension POSTs each completed human utterance to POST /agent. We run
// it through the SAME ModeBExport bridge the harness uses — so isActionable
// filtering, the capped transcript buffer, hooks, and error handling all apply on
// this (the reliable) path — then invoke the coding agent (default `claude -p`) and
// return the reply. We also serve the localhost control plane (status/transcript/
// end) so the gpt2agent `voice_live_*` MCP tools observe THIS path.
//
// Hardening: loopback-only; NO wildcard CORS; bounded request body; per-call agent
// timeout that kills the whole process group and returns immediately; optional
// GPTLIVE_TOKEN gate on /agent (note: the bundled extension does NOT send a token,
// so leave it unset if you rely on the extension — see sidecar/README.md).
//
//   AGENT_CMD='claude -p' node sidecar/agent-gateway.mjs
//   AGENT_CMD='codex exec --skip-git-repo-check' node sidecar/agent-gateway.mjs
import http from "node:http";
import { ModeBExport, ExportState } from "./src/export.mjs";
import { createControlServer, DEFAULT_CONTROL_PORT } from "./src/control.mjs";
import { runAgent } from "./src/agent-runner.mjs";

const PORT = Number(process.env.PORT || 8742);
const CONTROL_PORT = Number(process.env.GPTLIVE_CONTROL_PORT || DEFAULT_CONTROL_PORT);
const AGENT_CMD = process.env.AGENT_CMD || "claude -p";
const TOKEN = process.env.GPTLIVE_TOKEN || ""; // optional; extension does NOT send it
const MAX_BODY = 64 * 1024; // 64 KB request cap
const MAX_OUT = 1024 * 1024; // 1 MB reply cap
const AGENT_TIMEOUT_MS = Number(process.env.AGENT_TIMEOUT_MS || 120_000);

// One shared bridge: filtering + transcript buffer + agent hook, observed by the
// control plane below. The agent runner enforces a hard, group-killing timeout.
const bridge = new ModeBExport({
  onAgentTurn: (text) => runAgent(AGENT_CMD, text, { timeoutMs: AGENT_TIMEOUT_MS, maxOut: MAX_OUT }),
});
bridge.setState(ExportState.LIVE);

function readBodyLimited(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    let over = false;
    req.on("data", (chunk) => {
      if (over) return;
      body += chunk;
      if (body.length > MAX_BODY) {
        over = true;
        req.destroy();
        reject(new Error("body too large"));
      }
    });
    req.on("end", () => {
      if (!over) resolve(body);
    });
    req.on("error", reject);
  });
}

const srv = http.createServer(async (req, res) => {
  const send = (status, obj) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(obj));
  };
  // Diagnostics (no token; harmless): POST /hooked, GET /ping.
  if (req.method !== "POST" || req.url !== "/agent") {
    if (req.url === "/ping") return send(200, { ok: true });
    if (req.url === "/hooked") {
      console.log("[event] GPT-Live hook installed on chatgpt.com");
      return send(200, { ok: true });
    }
    return send(404, { error: "not found" });
  }
  if (TOKEN && req.headers["x-gptlive-token"] !== TOKEN) {
    return send(401, { error: "missing or invalid token" });
  }
  let raw;
  try {
    raw = await readBodyLimited(req);
  } catch {
    return send(413, { error: "body too large" });
  }
  let text = "";
  try {
    text = (JSON.parse(raw || "{}").text || "").trim();
  } catch {}
  if (!text) return send(400, { error: "empty text" });
  // Route through the bridge: isActionable filtering + transcript buffering + agent.
  const { humanText, agentReply } = await bridge.handleUtterance(text);
  if (!humanText) return send(200, { reply: "", filtered: true }); // dropped as filler
  console.log(`\n[human] ${humanText}`);
  console.log(`[agent] ${(agentReply || "").slice(0, 300)}`);
  send(200, { reply: agentReply || "[no reply]" });
});

let control = null;

async function shutdown() {
  bridge.close();
  try {
    if (control?.server) {
      await Promise.race([
        new Promise((r) => control.server.close(r)),
        new Promise((r) => setTimeout(r, 1500)),
      ]);
    }
  } catch {
    /* ignore */
  }
  try {
    await new Promise((r) => srv.close(r));
  } catch {
    /* ignore */
  }
}

srv.listen(PORT, "127.0.0.1", async () => {
  control = await createControlServer(bridge, {
    port: CONTROL_PORT,
    onEnd: async () => {
      console.log("[gateway] control /end — shutting down");
      await shutdown();
      process.exit(0);
    },
  });
  console.log(
    `agent gateway on http://127.0.0.1:${PORT}/agent  (AGENT_CMD=${AGENT_CMD}${TOKEN ? ", token required" : ""})`,
  );
  console.log(`control plane (voice_live_* MCP tools) on ${control.url}  (GET /help)`);
});
