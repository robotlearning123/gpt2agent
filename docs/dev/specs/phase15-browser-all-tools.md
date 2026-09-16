# Spec: Phase 1.5 — Browser Transport on ALL Conversation-Class Tools

Date: 2026-09-15 · Owner directive: "wire all to the MCP features" ·
Builds on: Phase 1 (PR #52, chat-only browser engine) ·
Status: approved for implementation

## Goal

Extend `browser=True` from `chat` to the remaining 8 conversation-class
tools — agent, deep_research, deep_research_heavy, gpt_chat,
memory_create_via_chat, generate_image, code_interpreter, canvas_execute —
so every sentinel-blocked MCP tool has an agent-native execution path with
zero human steps after the one-time profile login.

## Non-goals

- Canvas ARTIFACT extraction (iframe content) — chat text only, documented.
- Image asset download via browser — chat text + note, assets later.
- DR content_references/sources extraction via browser — report text only.
- No REST/manual behavior changes; precedence stays manual > browser > REST.

## Design

`BrowserTransport.chat(...)` gains `mode: str | None = None` and
`url: str | None = None` (URL override replaces the chatgpt.com root):

- `mode="agent"` → enable Agent via the composer tools menu before typing
- `mode="research"` → select Deep Research via the composer tools menu
  (used by deep_research AND deep_research_heavy; heavy selects the Pro/
  extra-high effort in the reasoning picker BEST-EFFORT — missing picker
  logs a warning and proceeds, same policy as the model picker)
- `mode="images"` → select Images mode (generate_image)
- `mode=None` (default) → plain chat (code_interpreter,
  memory_create_via_chat, canvas_execute, gpt_chat send compiled prompts)
- `url` override: gpt_chat navigates to `https://chatgpt.com/g/{slug}`
  (reuse the g/ normalization from tools/manual.py — import, do not copy)

New selectors (constants block only, fail-closed unless noted):
`SEL_MODES_BUTTON` (composer tools menu), `SEL_MODE_AGENT`,
`SEL_MODE_DEEP_RESEARCH`, `SEL_MODE_IMAGES`. Mode selection is fail-closed
(a missing mode entry is a named BrowserDriftError) EXCEPT the heavy-DR
effort step which is best-effort by spec.

Tool wiring (server.py + tools_features.py + images.py): each of the 8
tools gains `browser: bool = False`; when True and `[browser] enabled`,
send the SAME compiled prompt the REST path would send (DR imperative
prefix, canvas/memory wrappers — reuse, never re-type the wrapper logic)
through `BrowserTransport`; `manual=True` still wins. Lazy import with the
exact install hint on every one of the 8.

Reply reading: unchanged assistant-last text. Documented limitations in
each docstring: canvas returns chat text (artifact extraction follow-up);
generate_image returns chat text (asset pointers follow-up); DR returns
report text without a Sources section.

## Acceptance oracle

1. `pytest -q tests/` green (394 baseline), existing expectations unmodified.
2. New tests (mocked Playwright, same idioms as tests/test_browser_transport.py):
   mode paths for agent/research/images (menu open + option click, fail-closed
   on miss), heavy-DR best-effort effort step, gpt_chat URL override with g/
   normalization (both slug forms), precedence manual>browser on all 8,
   `[browser] enabled` gating on all 8, lazy-import install hint on all 8
   (assert the tools import BrowserTransport lazily, not at module import).
3. `python -c "import gpt2agent.server"` OK without playwright installed.
4. `grep -n "SEL_" gpt2agent/browser.py` — selectors only in the constants block.
5. Journey gate unchanged: `scripts/agent-user-journey.sh` still 15/15
   (J13 now applies to 9 tools, not just chat).

## Deliverables

browser.py mode/url support + new selectors; the 8 tool wirings; tests;
README status line update ("browser transport: experimental, all
conversation tools"); this spec. Live smokes (SKIP_LIVE) for browser agent
and browser DR exist but are NOT run here (no display).
