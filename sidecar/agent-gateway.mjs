// Local agent gateway. The Chrome extension POSTs each human utterance here;
// we invoke the coding agent (default `claude -p`: stdin = utterance, stdout =
// reply) and return the reply text. No browser, no audio — text control only.
//
//   AGENT_CMD='claude -p' node sidecar/agent-gateway.mjs
//   AGENT_CMD='codex exec --skip-git-repo-check' node sidecar/agent-gateway.mjs
import http from "node:http";
import { spawn } from "node:child_process";

const PORT = Number(process.env.PORT || 8742);
const AGENT_CMD = process.env.AGENT_CMD || "claude -p";

function runAgent(text) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const c = spawn(AGENT_CMD, { shell: true, stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    c.stdout.on("data", (d) => (out += d.toString("utf8")));
    c.stderr.on("data", () => {});
    c.on("error", () => resolve({ reply: "[agent spawn error]", ms: Date.now() - t0 }));
    c.on("close", () => resolve({ reply: out.trim(), ms: Date.now() - t0 }));
    c.stdin.write(text);
    c.stdin.end();
  });
}

const srv = http.createServer(async (req, res) => {
  // CORS preflight (in case a page-context fetch is ever used).
  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
    });
    return res.end();
  }
  if (req.method !== "POST" || req.url !== "/agent") {
    // diagnostics: GET /ping (liveness), POST /hooked (extension hook installed)
    if (req.url === "/ping") { res.writeHead(200, { "Access-Control-Allow-Origin": "*" }); return res.end('{"ok":true}'); }
    if (req.url === "/hooked") { console.log("[event] GPT-Live hook installed on chatgpt.com"); res.writeHead(200, { "Access-Control-Allow-Origin": "*" }); return res.end('{"ok":true}'); }
    res.writeHead(404, { "Access-Control-Allow-Origin": "*" });
    return res.end('{"error":"not found"}');
  }
  let body = "";
  for await (const chunk of req) body += chunk;
  let text = "";
  try { text = (JSON.parse(body || "{}").text || "").trim(); } catch {}
  if (!text) {
    res.writeHead(400, { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" });
    return res.end('{"error":"empty text"}');
  }
  console.log(`\n[human] ${text}`);
  const r = await runAgent(text);
  console.log(`[agent ${r.ms}ms] ${(r.reply || "").slice(0, 300)}`);
  res.writeHead(200, { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" });
  res.end(JSON.stringify({ reply: r.reply || "[no reply]" }));
});

srv.listen(PORT, "127.0.0.1", () => {
  console.log(`agent gateway on http://127.0.0.1:${PORT}/agent  (AGENT_CMD=${AGENT_CMD})`);
});
