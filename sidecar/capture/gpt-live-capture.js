// GPT-Live handshake capture harness — paste into the DevTools console of an
// AUTHENTICATED chatgpt.com tab, THEN open a voice session for a few seconds.
//
// It records routes and shapes ONLY — no raw audio, tokens, or transcript text
// are stored. It exists to fill the one un-captured seam in the sidecar: the
// consumer session-bootstrap route, the SDP exchange endpoint, the ICE servers,
// and the datachannel event TYPE names (the Mode B make-or-break: is there an
// input-transcript event, and an event that makes Live speak provided text?).
//
// Requirements: a real or fake audio input device must be present, or the app
// aborts before negotiating (verified: with zero input devices, getUserMedia
// throws NotFoundError and no RTCPeerConnection is created). For a headless
// browser launch with:  --use-fake-device-for-media-stream --use-fake-ui-for-media-stream
//
// After ~5s of voice, run:  copy(JSON.stringify(window.__gptLiveCapture, null, 2))
// and paste the result back. End the voice session afterward.

(() => {
  if (window.__gptLiveCapture) {
    console.warn("[gpt-live-capture] already installed");
    return;
  }
  const cap = (window.__gptLiveCapture = {
    installedAt: new Date().toISOString(),
    endpoints: [], // "METHOD host+path" (query stripped) for realtime-ish calls
    iceServers: [], // STUN/TURN url hosts only
    offers: [], // { type, sdpLen } — no SDP body
    answers: [],
    dataChannels: [], // channel labels
    eventTypes: [], // unique inbound datachannel event `type` names (Mode B signal)
    gumCalls: 0,
    notes: [],
  });
  const seenEvent = new Set();
  const pathOnly = (u) => {
    try {
      const url = new URL(u, location.href);
      return url.host + url.pathname;
    } catch {
      return String(u).split("?")[0];
    }
  };

  // fetch — capture realtime/session/SDP endpoints (method + path, no query).
  const _fetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const raw = typeof input === "string" ? input : input && input.url;
      if (raw && /realtime|voice|sdp|session|\brtc\b|webrtc|synthes|candidate|bidi/i.test(raw)) {
        cap.endpoints.push(`${(init && init.method) || "GET"} ${pathOnly(raw)}`);
      }
    } catch {}
    return _fetch.apply(this, arguments);
  };

  // WebSocket — capture signaling endpoints (host+path only).
  const _WS = window.WebSocket;
  function HookedWS(url, protocols) {
    try {
      cap.endpoints.push(`WS ${pathOnly(url)}`);
    } catch {}
    return new _WS(url, protocols);
  }
  HookedWS.prototype = _WS.prototype;
  window.WebSocket = HookedWS;

  // getUserMedia — count calls (does not alter audio; real device still used).
  const md = navigator.mediaDevices;
  if (md && md.getUserMedia) {
    const _gum = md.getUserMedia.bind(md);
    md.getUserMedia = function () {
      cap.gumCalls += 1;
      return _gum.apply(this, arguments);
    };
  }

  // RTCPeerConnection — capture SDP sizes, ICE servers, datachannel labels, and
  // the unique inbound event type names. Patch the prototype so module-local
  // references are still instrumented.
  const P = window.RTCPeerConnection && window.RTCPeerConnection.prototype;
  if (P && !P.__gptLiveHooked) {
    P.__gptLiveHooked = true;
    const _sld = P.setLocalDescription;
    P.setLocalDescription = function (d) {
      try {
        cap.offers.push({ type: (d && d.type) || "", sdpLen: ((d && d.sdp) || "").length });
      } catch {}
      return _sld.apply(this, arguments);
    };
    const _srd = P.setRemoteDescription;
    P.setRemoteDescription = function (d) {
      try {
        cap.answers.push({ type: (d && d.type) || "", sdpLen: ((d && d.sdp) || "").length });
      } catch {}
      return _srd.apply(this, arguments);
    };
    const _cdc = P.createDataChannel;
    P.createDataChannel = function (label) {
      cap.dataChannels.push(label);
      const dc = _cdc.apply(this, arguments);
      try {
        dc.addEventListener("message", (m) => {
          try {
            const t = JSON.parse(m.data).type;
            if (t && !seenEvent.has(t)) {
              seenEvent.add(t);
              cap.eventTypes.push(t);
            }
          } catch {}
        });
      } catch {}
      return dc;
    };
  }
  // Record ICE servers from any PeerConnection config (constructor wrapper).
  const _RTC = window.RTCPeerConnection;
  function HookedRTC(config) {
    try {
      for (const s of (config && config.iceServers) || []) {
        const urls = [].concat(s.urls || []);
        for (const u of urls) cap.iceServers.push(pathOnly(u));
      }
    } catch {}
    return new _RTC(config);
  }
  HookedRTC.prototype = _RTC.prototype;
  window.RTCPeerConnection = HookedRTC;

  console.log("[gpt-live-capture] installed. Open voice for ~5s, then run: copy(JSON.stringify(window.__gptLiveCapture,null,2))");
})();
