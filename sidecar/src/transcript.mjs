// Transcript assembler for the consumer GPT-Live datachannel protocol.
// Pure logic, no DOM, no network — so it is unit-testable with synthetic event
// streams (no voice, no human, no LLM). Used by the extension hook and the CDP
// bridge to turn the chat_message_delta JSON-patch stream into human utterances.
//
// Envelope both directions: { type:"data_message", data:"<inner-json-string>" }
// Inner: { type:"chat_message_delta", payload:{ delta:{ o, p, v } } }
//   o:"add"    v.message = { id, author.role, content.parts:[{content_type,direction,text}] }
//   o:"patch"/appended via v:[ { p:"/message/content/parts/0/text", o:"append", v:"chunk" }, … ]
//   o:"replace" p:"/message/status" v:"finished_successfully"  (utterance complete)

/** Unwrap the data_message envelope if present; return the inner event object. */
export function unwrap(message) {
  let m = message;
  if (typeof m === "string") { try { m = JSON.parse(m); } catch { return null; } }
  if (!m || typeof m !== "object") return null;
  if (m.type === "data_message" && typeof m.data === "string") {
    try { return JSON.parse(m.data); } catch { return null; }
  }
  return m;
}

export class TranscriptAssembler {
  constructor() {
    this._msgs = {};     // mid -> {dir, text, done}
    this._order = [];    // message ids in arrival order
  }

  /**
   * Feed one raw datachannel message (string | object, enveloped or inner).
   * Returns an array of newly-completed HUMAN utterance strings (direction:in).
   */
  feed(message) {
    const inner = unwrap(message);
    if (!inner || inner.type !== "chat_message_delta") return [];
    const d = (inner.payload || inner).delta || {};
    const out = [];

    if (d.o === "add" && d.v && d.v.message) {
      const m = d.v.message;
      const mid = m.id;
      if (mid && !this._msgs[mid]) {
        let dir = null, txt = "";
        for (const p of (m.content && m.content.parts) || [])
          if (p && p.direction) { dir = p.direction; txt = p.text || ""; }
        this._msgs[mid] = { dir, text: txt, done: false };
        this._order.push(mid);
      }
    }

    const last = this._order[this._order.length - 1];
    if (!last) return out;

    // patch ops array
    if (Array.isArray(d.v)) {
      for (const op of d.v) {
        if (op.o === "append" && op.p === "/message/content/parts/0/text" && this._msgs[last])
          this._msgs[last].text += op.v || "";
        if (op.o === "replace" && op.p === "/message/status" && op.v === "finished_successfully")
          out.push(...this._complete(last));
      }
    }
    // single replace op
    if (d.o === "replace" && d.p === "/message/status" && d.v === "finished_successfully")
      out.push(...this._complete(last));

    return out;
  }

  _complete(mid) {
    const m = this._msgs[mid];
    if (!m || m.done) return [];
    m.done = true;
    if (m.dir !== "in") return [];      // only human (direction:in) utterances
    const t = (m.text || "").trim();
    return t ? [t] : [];
  }
}

/** A human utterance worth acting on (drops acks / filler). Used by the bridge. */
export function isActionable(text) {
  const t = (text || "").trim().toLowerCase();
  if (!t) return false;
  if (["ok", "okay", "um", "uh", "yeah", "yes", "no", "hello", "hi", "hey", "mm", "hmm"].includes(t)) return false;
  return true;
}
