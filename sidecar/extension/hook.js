// hook.js — runs in the PAGE main world (world:"MAIN") at document_start, BEFORE
// ChatGPT's own scripts. Wraps RTCPeerConnection so that when GPT-Live opens its
// negotiated datachannel, we see every inbound message. We reconstruct each HUMAN
// utterance from the real consumer protocol (chat_message_delta, direction:"in",
// JSON-patch appends) and, on turn completion, post it to the isolated-world relay
// (window.postMessage) — which forwards it to our coding agent via the background.
//
// We do NOT inject anything into the datachannel (proven impossible / dropped by
// the server). We only observe the human side and show the agent's reply as text.
(() => {
  if (window.__gptliveHook) return;
  window.__gptliveHook = true;
  const msgs = {};        // mid -> {dir, text, done}
  const order = [];

  const _RTC = window.RTCPeerConnection;
  if (!_RTC || _RTC.__gh) return;
  function W(cfg) {
    const pc = new _RTC(cfg);
    const _cdc = pc.createDataChannel.bind(pc);
    pc.createDataChannel = function (label, opts) {
      const dc = _cdc(label, opts);
      dc.addEventListener("message", (ev) => {
        try {
          let outer = JSON.parse(String(ev.data));
          let inner = outer && outer.type === "data_message" && typeof outer.data === "string"
            ? JSON.parse(outer.data) : outer;
          if ((inner && inner.type) !== "chat_message_delta") return;
          const d = (inner.payload || inner).delta || {};

          // add message skeleton
          if (d.o === "add" && d.v && d.v.message) {
            const m = d.v.message, mid = m.id;
            if (mid && !msgs[mid]) {
              let dir = null, txt = "";
              for (const p of (m.content && m.content.parts) || [])
                if (p && p.direction) { dir = p.direction; txt = p.text || ""; }
              msgs[mid] = { dir, text: txt, done: false };
              order.push(mid);
            }
          }
          const last = order[order.length - 1];
          // patch ops
          if (last && Array.isArray(d.v)) {
            for (const op of d.v) {
              if (op.o === "append" && op.p === "/message/content/parts/0/text" && msgs[last]) msgs[last].text += op.v || "";
              if (op.o === "replace" && op.p === "/message/status" && op.v === "finished_successfully" && msgs[last] && !msgs[last].done) {
                msgs[last].done = true;
                if (msgs[last].dir === "in") emit(msgs[last].text);
              }
            }
          }
          if (d.o === "replace" && d.p === "/message/status" && d.v === "finished_successfully" && last && msgs[last] && !msgs[last].done) {
            msgs[last].done = true;
            if (msgs[last].dir === "in") emit(msgs[last].text);
          }
        } catch {}
      });
      return dc;
    };
    return pc;
  }
  W.prototype = _RTC.prototype;
  try { W.generateCertificate = _RTC.generateCertificate && _RTC.generateCertificate.bind(_RTC); } catch {}
  _RTC.__gh = true;
  window.RTCPeerConnection = W;

  // signal that the hook is installed (for diagnostics)
  window.postMessage({ __gptlive_hooked: true }, location.origin);

  function emit(text) {
    text = (text || "").trim();
    if (text) window.postMessage({ __gptlive_utterance: true, text }, location.origin);
  }

  // show the agent's reply (text overlay; since Live won't speak it)
  window.addEventListener("message", (e) => {
    if (e.source === window && e.data && e.data.__gptlive_reply) showReply(e.data.text);
  });
  function showReply(text) {
    let el = document.getElementById("__gptlive_overlay");
    if (!el) {
      el = document.createElement("div");
      el.id = "__gptlive_overlay";
      el.style.cssText = "position:fixed;right:14px;bottom:14px;max-width:440px;max-height:45vh;overflow:auto;z-index:2147483647;background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:10px;padding:12px 14px;font:13px/1.45 ui-monospace,SFMono-Regular,monospace;white-space:pre-wrap;box-shadow:0 8px 30px rgba(0,0,0,.4)";
      document.documentElement.appendChild(el);
    }
    el.textContent = "🤖 coding agent:\n\n" + text;
  }
})();
