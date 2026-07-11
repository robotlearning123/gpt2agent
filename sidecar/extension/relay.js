// relay.js — isolated-world content script. Bridges page-main-world postMessage
// ↔ extension background (chrome.runtime). The main-world hook can't call chrome.*
// APIs, so it window.postMessage's utterances here; we forward to the background,
// which hits the localhost agent gateway, and we post the reply back for display.
(() => {
  window.addEventListener("message", (e) => {
    if (e.source !== window || !e.data || !e.data.__gptlive_utterance) return;
    const text = e.data.text;
    try {
      chrome.runtime.sendMessage({ type: "utterance", text }, (resp) => {
        if (chrome.runtime.lastError) return;
        if (resp && resp.reply) {
          window.postMessage({ __gptlive_reply: true, text: resp.reply }, location.origin);
        }
      });
    } catch {}
  });
  // diagnostic: hook-installed ping
  try { chrome.runtime.sendMessage({ type: "hooked" }, () => void chrome.runtime.lastError); } catch {}
})();
