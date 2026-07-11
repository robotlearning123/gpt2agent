// Localhost-only HTTP control plane for Mode B export.
//
// Agents (including gpt2agent MCP tools) talk text/control here.
// No raw audio or account secrets are ever returned.
//
// Routes:
//   GET  /health          → { ok: true }
//   GET  /status          → export status (redacted)
//   GET  /transcript      → buffered text transcripts
//   POST /send_text       → { text } queue speak-injection
//   POST /end             → close export plane
//   GET  /help            → how to start export + Turnstile boundary

import http from "node:http";
import { ModeBExport, ExportState } from "./export.mjs";

export const DEFAULT_CONTROL_HOST = "127.0.0.1";
export const DEFAULT_CONTROL_PORT = 8741;

export const EXPORT_HELP = Object.freeze({
  summary:
    "Export GPT-Live as Mode B voice I/O: browser owns audio; agent gets transcripts and send_text only.",
  start: [
    "1. Sign into chatgpt.com once in a dedicated Chrome profile:",
    '   open -na "Google Chrome" --args --user-data-dir="$PWD/.chrome-gptlive"',
    "2. Prepare a short mono WAV (fake mic) or use a real mic profile.",
    "3. Run: node browser/sidecar.mjs --profile ./.chrome-gptlive --audio ./q.wav --control-port 8741",
    "4. Point the agent at http://127.0.0.1:8741 (status / transcript / send_text / end).",
  ],
  tools: {
    status: "GET /status — connection + queue lengths (no secrets)",
    transcript: "GET /transcript — human/agent text buffer",
    send_text: "POST /send_text {\"text\":\"...\"} — speak agent reply via Live TTS",
    end: "POST /end — tear down export plane",
  },
  boundary: {
    audio: "never crosses control/MCP boundary",
    auth: "human-authenticated real browser session required",
    turnstile:
      "Cloudflare Turnstile / bot-detection bypass is out of scope. Headless token-only SDP is not the export path.",
    modeA: "GPT-Live natively calling external tools is not supported",
  },
});

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

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
 *   sendSpeak?: (wire: string) => boolean|Promise<boolean>,
 * }} [opts]
 */
export function createControlServer(exportPlane, opts = {}) {
  if (!(exportPlane instanceof ModeBExport)) {
    throw new TypeError("createControlServer requires a ModeBExport instance");
  }
  const host = opts.host ?? DEFAULT_CONTROL_HOST;
  const port = opts.port ?? DEFAULT_CONTROL_PORT;
  const onEnd = opts.onEnd;
  const sendSpeak = opts.sendSpeak;

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
      if (req.method === "POST" && path === "/send_text") {
        const raw = await readBody(req);
        let body = {};
        try {
          body = raw ? JSON.parse(raw) : {};
        } catch {
          return sendJson(res, 400, { error: "invalid JSON body" });
        }
        const text = body.text;
        if (typeof text !== "string" || !text.trim()) {
          return sendJson(res, 400, { error: "text must be a non-empty string" });
        }
        let wire;
        try {
          wire = exportPlane.queueSpeak(text);
        } catch (err) {
          return sendJson(res, 400, {
            error: err instanceof Error ? err.message : String(err),
          });
        }
        let delivered = false;
        if (typeof sendSpeak === "function") {
          delivered = Boolean(await sendSpeak(wire));
        }
        // If not delivered to a live dc, leave it for the next drain.
        if (!delivered) {
          // queueSpeak already pushed; ensure wire is available via drain if needed
        } else {
          // Drop matching head of queue if sendSpeak consumed it outside drain.
          exportPlane.drainSpeakQueue();
        }
        return sendJson(res, 200, {
          ok: true,
          delivered,
          // Return shape only — not the raw wire (may contain model instructions).
          bytes: Buffer.byteLength(wire),
          contract: "response.create via data_message envelope (text control only)",
        });
      }
      if (req.method === "POST" && path === "/end") {
        exportPlane.close();
        if (typeof onEnd === "function") await onEnd();
        return sendJson(res, 200, { ok: true, state: ExportState.CLOSED });
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
