# Account safety (anti-ban) design

Measured context (2026-09-15/16): session-copy probes carrying a real
session-token from foreign browser contexts caused OpenAI to invalidate the
session server-side within minutes. Chrome-automation carries real ban risk;
this document is the binding design for keeping the account healthy.

## Principles

1. **One browser identity, forever.** Exactly one persistent profile
   (`~/.gpt2agent/chrome-profile`), one machine, one Chrome channel. Never
   copy cookies/profiles between contexts (measured: instant session kill,
   and it burned the owner's own session once). Never log the same account
   into a second automated profile.
2. **Human pacing.** Minimum interval between browser turns (default 20 s,
   jittered); no parallel conversations; burst cap (default 5 turns / 10 min).
   A scheduler may queue, never fan out.
3. **Daily budget.** Hard cap on browser turns per UTC day (default 50),
   persisted in `~/.gpt2agent/state/budget.json`. Premium models (6 Pro)
   draw from the account's Pro message pool — check
   `/backend-api/conversation/init` allowance fields when available and
   surface remaining quota in tool output.
4. **Challenge = backoff, never brute force.** On a Cloudflare/Turnstile
   interstitial or repeated logged-out state: abort the turn, exponential
   backoff (1 min → 1 h), alert the owner after 3 consecutive failures.
   Never retry-loop a challenge.
5. **Temporary chats by default.** `temporary=True` stays the default for
   agent turns (smaller account surface); persistent chats only when the
   workflow needs readback via `get_conversation`.
6. **Read-only REST is cheap, browser is expensive.** Prefer REST
   (`list_*`, `get_*`, `memory_*`) for reads even while conversation REST is
   sentinel-blocked; the browser lane is for conversation-class only.
7. **No scraping shape.** Single-turn, task-driven prompts only; no history
   enumeration, no bulk export, no headless fingerprint games.

## Enforcement points

- `gpt2agent/browser.py`: `BrowserTransport` enforces min-interval, burst
  cap, and daily budget before launching; challenge-page detection raises
  `BrowserChallengeError` (fail-closed) and advances the backoff state.
- Config `[browser]`: `min_interval_s`, `burst_limit`, `daily_limit`,
  `backoff_s` (defaults above).
- Doctor row: browser-lane budget/backoff state shown next to the sentinel
  row.

## Incident log

- 2026-09-15: owner session invalidated by cookie-carry experiments — the
  origin of rules 1 and 4.
