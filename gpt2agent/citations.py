"""Inline citation rendering for Deep Research replies.

The DR stream emits raw internal citation markers in the report text
(private-use unicode wrapping ``citeturn0searchN`` tokens), plus a
``content_references`` list whose ``matched_text`` entries carry the exact
marker spans and ``safe_urls`` the destinations. Rendered naively, the
anchors appear as garbage text (the long-standing "citation bug"); this
module rewrites each marker to ``[N](url)`` in order of appearance and
strips any leftover private-use characters.
"""
from __future__ import annotations

import re

#: private-use plane characters OpenAI wraps citation tokens in
_PRIVATE_USE = re.compile("[-]")


def apply_inline_citations(text: str, refs: list) -> str:
    """Replace raw DR citation markers with markdown anchors.

    ``refs`` is the ``content_references`` list: dicts with ``matched_text``
    (the marker span as it appears in the text) and ``safe_urls`` (ordered
    destinations). Markers are numbered by first appearance; a marker with
    no URL degrades to ``[N]``. Leftover private-use unicode is stripped.
    """
    if not text:
        return text
    out = text
    index: dict[str, int] = {}
    for ref in refs or []:
        marker = (ref or {}).get("matched_text") or ""
        if not marker or marker not in out:
            continue
        urls = [u for u in ((ref or {}).get("safe_urls") or []) if u]
        if marker not in index:
            index[marker] = len(index) + 1
        num = index[marker]
        repl = "".join(f"[{num}]({u})" for u in urls) if urls else f"[{num}]"
        out = out.replace(marker, repl, 1)
    return _PRIVATE_USE.sub("", out)
