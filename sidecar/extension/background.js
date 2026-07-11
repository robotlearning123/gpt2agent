// background.js — MV3 service worker. Receives human utterances from the relay,
// POSTs to the localhost agent gateway, returns the coding agent's reply.
// host_permissions for http://127.0.0.1:8742/* lets the service worker fetch it
// (no page CSP / CORS issue).
const GATEWAY = "http://127.0.0.1:8742/agent";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return false;
  if (msg.type === "hooked") {
    // diagnostic: confirm the content scripts are live on chatgpt.com
    fetch("http://127.0.0.1:8742/hooked", { method: "POST" }).catch(() => {});
    return false;
  }
  if (msg.type !== "utterance") return false;
  fetch(GATEWAY, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: msg.text }),
  })
    .then((r) => r.json())
    .then((j) => sendResponse({ reply: (j && j.reply) || "" }))
    .catch((e) => sendResponse({ reply: "[gateway unreachable: " + e.message + "]" }));
  return true; // keep the message channel open for the async sendResponse
});
