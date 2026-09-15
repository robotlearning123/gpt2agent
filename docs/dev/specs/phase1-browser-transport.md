# Spec: Phase 1 — Browser Transport (`chat` via real Chrome)

Date: 2026-09-15 · Owner directive: "must be auto, agent native, no human in
the loop; you can use the chrome profile for the logged-in account" ·
Status: approved for implementation

## Goal

Add a browser execution engine so `chat` works again while the upstream
Sentinel/Turnstile challenge blocks `/backend-api/conversation` (measured
2026-09-08, still blocked 2026-09-15). A real Chrome drives chatgpt.com the
way a human does; Turnstile completes inside the real browser. After a
one-time interactive login into a dedicated profile, the loop is fully
agent-native — no human steps.

## Non-goals

- No Electron/launcher app; Playwright only, optional extra `gpt2agent[browser]`.
- No DR / image-gen / code-interpreter / canvas / custom-GPT via browser
  (later phases). `chat` only.
- No Responses-API bridge, no Codex model impersonation.
- No silent switching: transport is explicit; REST stays the default.
- No change to `manual=True` handoff behavior.

## Design

New module `gpt2agent/browser.py`:

```python
class BrowserTransport:
    def __init__(self, profile_dir: Path | None = None, headed: bool = True) -> None
    async def chat(self, prompt: str, model: str | None = None,
                   temporary: bool = True) -> str
```

- Profile: persistent context at `~/.gpt2agent/chrome-profile` (override via
  config `[browser] profile_dir`). First run: if the page shows logged-out
  state, launch HEADED and print "log in once in the opened window" — the
  ONLY human step, once ever; the profile persists the session.
- Launch: Playwright `chromium.launch_persistent_context` with
  `channel="chrome"` (system Chrome; no browser download). `headed=True`
  default — Turnstile in a visible real browser is the known-good path
  (codex-chatgpt-web precedent); `headless="new"` behind config
  `[browser] headless` is EXPERIMENTAL and must be probed before being
  claimed working.
- Flow: open `https://chatgpt.com/` → (temporary: click the hourglass
  toggle) → select model if `model` given and picker exposes it → type
  prompt → send → poll the assistant message DOM until the streaming
  indicator disappears (bounded timeout, default 180 s) → return text.
- Selectors live in ONE constants block at the top of `browser.py`.
  Fail-closed: any selector miss raises `BrowserDriftError(RuntimeError)`
  naming the failed selector and the action — never a fallback or a hang.
- Config (config.toml `[browser]`): `enabled` (default false), `headed`
  (default true), `profile_dir`, `timeout_s` (default 180).

## Tool surface

`chat(prompt, model, temporary, manual, browser: bool = False)`. When
`browser=True`: import `gpt2agent.browser` lazily; if the optional extra is
missing, raise with the exact install command (`pip install
"gpt2agent[browser]"`). `manual=True` still wins over `browser=True`
(explicit handoff beats engine choice; document in the docstring).

## Acceptance oracle

1. `pytest -q tests/` green; existing tests untouched in expectation.
2. `tests/test_browser_transport.py` with a **mocked** Playwright: selector
   constants used, fail-closed `BrowserDriftError` on missing selector,
   prompt typed verbatim, temporary toggle path, model-picker optional,
   timeout path, lazy-import error message without the extra.
3. `python -c "import gpt2agent.server"` OK without playwright installed.
4. Live smoke (SKIP_LIVE-gated like other live tests):
   `chat(prompt="Reply with exactly: BROWSER OK", browser=True)` returns
   "BROWSER OK" on the real account, real Chrome, no REST call. The smoke
   must be runnable headed; if run headless, the test asserts nothing about
   Turnstile (labeled experimental).
5. `grep -rn "selector" gpt2agent/browser.py` shows selectors only in the
   constants block.

## Deliverables

`gpt2agent/browser.py`, `chat` param wiring in `server.py`,
`tests/test_browser_transport.py`, config `[browser]` handling,
`pyproject.toml` optional extra `[browser]` (playwright), README status
section note ("browser transport: experimental, chat only"), this spec.
