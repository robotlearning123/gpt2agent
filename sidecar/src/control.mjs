// Localhost-only HTTP control plane for the GPT-Live bridge layer.
//
// Agents (including gpt2agent MCP tools) talk text/control here. Direction is
// human → agent: this plane exposes the OBSERVED human transcript and lifecycle
// only. There is no speak channel — GPT-Live silently drops client-injected
// speech, so the agent reply reaches the human out-of-band (overlay), not here.
// No raw audio or account secrets are ever returned.
//
// Routes:
//   GET  /health          → { ok: true }
//   GET  /status          → bridge status (redacted)
//   GET  /transcript      → buffered human/agent text
//   POST /end             → close the bridge
//   GET  /help            → how to start the bridge + Turnstile boundary

import http from "node:http";
import { ModeBExport, ExportState } from "./export.mjs";

export const DEFAULT_CONTROL_HOST = "127.0.0.1";
export const DEFAULT_CONTROL_PORT = 8741;

export const EXPORT_HELP = Object.freeze({
  summary:
    "GPT-Live → coding-agent bridge (human → agent). The browser owns audio; the agent observes the human transcript. Reply reaches the human out-of-band, not by making Live speak.",
  reliablePath: [
    "Reliable path = your real signed-in Chrome + the extension (sidecar/extension) + agent gateway.",
    "1. Load sidecar/extension as an unpacked extension in your logged-in Chrome.",
    "2. AGENT_CMD='claude -p' node sidecar/agent-gateway.mjs   (the coding agent)",
    "3. Open chatgpt.com, start voice, talk — utterances route to the agent; reply shows as a text overlay.",
    "Note: browser/sidecar.mjs (puppeteer + fake WAV mic) is a TEST harness only — synthetic audio is not transcribed by Live.",
  ],
  tools: {
    status: "GET /status — bridge state + transcript count (no secrets/audio)",
    transcript: "GET /transcript — buffered human/agent text",
    end: "POST /end — tear down the bridge",
  },
  boundary: {
    direction: "human → agent only",
    speak: "agent→Live speak-injection is unsupported (server silently drops it); reply is out-of-band",
    audio: "never crosses the control/MCP boundary",
    auth: "human-authenticated real browser session required",
    turnstile:
      "Cloudflare Turnstile / bot-detection bypass is out of scope. Headless/fake-mic SDP is not a supported path.",
  },
});

function sendJson(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(data),
    "Cache-Control": "no-store",
  });
  res.end(data);
}

/**
 * @param {ModeBExport} exportPlane
 * @param {{
 *   host?: string,
 *   port?: number,
 *   onEnd?: () => void|Promise<void>,
 * }} [opts]
 */
export function createControlServer(exportPlane, opts = {}) {
  if (!(exportPlane instanceof ModeBExport)) {
    throw new TypeError("createControlServer requires a ModeBExport instance");
  }
  const host = opts.host ?? DEFAULT_CONTROL_HOST;
  const port = opts.port ?? DEFAULT_CONTROL_PORT;
  const onEnd = opts.onEnd;

  const server = http.createServer(async (req, res) => {
    // Bind to loopback only by listen(); still reject non-local Host abuse lightly.
    try {
      const url = new URL(req.url || "/", `http://${host}`);
      const path = url.pathname;

      if (req.method === "GET" && path === "/health") {
        return sendJson(res, 200, { ok: true });
      }
      if (req.method === "GET" && path === "/help") {
        return sendJson(res, 200, EXPORT_HELP);
      }
      if (req.method === "GET" && path === "/status") {
        return sendJson(res, 200, exportPlane.status());
      }
      if (req.method === "GET" && path === "/transcript") {
        const clear = url.searchParams.get("clear") === "1";
        return sendJson(res, 200, {
          transcripts: exportPlane.getTranscripts({ clear }),
        });
      }
      if (req.method === "POST" && path === "/end") {
        exportPlane.close();
        // Respond BEFORE running onEnd. onEnd may close THIS server (sidecar
        // shutdown → server.close()), which waits for in-flight responses to
        // finish — awaiting onEnd first would deadlock on our own /end response.
        sendJson(res, 200, { ok: true, state: ExportState.CLOSED });
        if (typeof onEnd === "function") {
          setImmediate(() => Promise.resolve(onEnd()).catch(() => {}));
        }
        return;
      }
      return sendJson(res, 404, { error: "not found", help: "/help" });
    } catch (err) {
      return sendJson(res, 500, {
        error: err instanceof Error ? err.message : String(err),
      });
    }
  });

  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => {
      const addr = server.address();
      resolve({
        server,
        host,
        port: typeof addr === "object" && addr ? addr.port : port,
        url: `http://${host}:${typeof addr === "object" && addr ? addr.port : port}`,
      });
    });
  });
}
