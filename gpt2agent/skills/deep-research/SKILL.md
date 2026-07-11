---
name: deep-research
version: 0.1.2
description: |
  ChatGPT Pro Deep Research via gpt2agent. Two modes: light (model=research,
  30-120s, citations preserved) and heavy (gpt-5-5-pro + connector, 5-30 min,
  long-form report recovered from the connector widget state, citations included).
  Reuses `$CODEX_HOME/auth.json` (or `~/.codex/auth.json`) or the manual
  `~/.gpt2agent/token.json` fallback.
  Each run uses the account's Deep Research quota. Output saved as Markdown.
  Use when asked to "deep research", "DR", "ChatGPT deep research",
  "research <topic>", "go deep on <topic>", or when a question genuinely
  needs web-augmented multi-source synthesis beyond what training data covers.
allowed-tools:
  - Bash
  - Read
  - Write
---

# /deep-research — ChatGPT Pro Deep Research

Calls `gpt2agent`'s `deep_research` / `deep_research_heavy` directly via
pipx Python (bypasses MCP — works even before Claude Code session restart).

## Preconditions (check once)

```bash
command -v gpt2agent >/dev/null || \
  echo "gpt2agent not installed; run: pipx install gpt2agent"
test -f "${CODEX_HOME:-$HOME/.codex}/auth.json" || test -f "$HOME/.gpt2agent/token.json" || \
  echo "ChatGPT token missing; run: codex login or gpt2agent setup"
~/.claude/skills/deep-research/bin/quota.sh   # prints remaining DR quota
```

## Usage

```bash
~/.claude/skills/deep-research/bin/run.sh [--heavy] [-o OUT_DIR] "<query>"
```

- Default mode: **light** (`deep_research`, ~1 min, citations included).
- `--heavy`: **deep_research_heavy** (5-30 min, gpt-5-5-pro + connector). The
  connector renders an embedded-UI widget; the report is recovered from the
  hidden widget state (`widget_state.report_message`) via
  `?include_visually_hidden_messages=true&include_widget_state=true` — see
  "Heavy DR retrieval" below.
- `-o OUT_DIR`: output directory (default: a unique
  `./research/dr-YYYYMMDD-HHMMSS-*/` directory).
- Query can be inline string, `-` for stdin, or `@file.md` to read from file.

The script writes:
- `report.md` — final report (reconstructed for heavy mode)
- `status.txt` — shape-only START / DONE / INCOMPLETE / ERROR diagnostics with
  elapsed seconds and event-category counts

The runner deliberately does not persist raw SSE events or server metadata.
The run directory is restricted to mode `0700` and its artifacts to `0600` on
POSIX systems because reports may be sensitive.

## When to invoke

| Situation | Mode |
|---|---|
| Quick factual question with citations | light |
| Literature review, market scan, technical decision matrix | light |
| Big strategic question (>5 questions, want 10+ KB report) | heavy |
| Question that *might* yield 50+ sources | heavy |

Skip this skill for: pure code questions, debugging, tasks the user
explicitly wants you to handle locally, anything covered by `context7`
(library docs) or local files.

## How to use in conversation

1. If the user provides a clear topic, draft a 3-8 question structured
   query (research questions + context + deliverable shape) and **show it
   to the user before firing** so they can edit / approve. Heavy mode
   especially deserves a query review — it takes minutes and consumes one unit
   of the account-reported quota.
2. Pass the approved query over stdin (avoids shell escaping and shared
   temporary files):
   ```bash
   ~/.claude/skills/deep-research/bin/run.sh [--heavy] -o /path/to/out - <<'EOF'
   <your structured query>
   EOF
   ```
3. Use `run_in_background: true` for heavy mode — it takes minutes. Tail
   `status.txt` to monitor shape-only progress; the bash completion notification
   will arrive when finished.
4. After completion: Read `report.md`, summarize key findings in 5-8
   bullets for the user, point to the full file path.

## Heavy DR retrieval — connector widget state (fixed 2026-06-11)

`deep_research_heavy` dispatches the `connector_openai_deep_research` connector
(the "Deep Research App", pineapple URI
`connectors://connector_openai_deep_research`), which renders an embedded-UI
widget. The connector **never** writes its report as an assistant text node in
the conversation `mapping` (the assistant text node stays 0-char), so the legacy
poll — which only scanned for assistant text — timed out at 1800s even though
the research completed server-side.

**The report lives in the hidden widget state.** Fetching the conversation with
`?include_visually_hidden_messages=true&include_widget_state=true` exposes a node
carrying `widget_state.report_message.content.parts[0]` (the full Markdown
report) plus `report_message.metadata.content_references` (the source URLs). The
widget state is delivered via two carriers, both handled by
`_dr_report_from_widget_state`:

1. a tool text node whose part starts with `"The latest state of the widget is: {…}"`, and
2. `message.metadata.chatgpt_sdk.widget_state` (a JSON string).

`_poll_dr_completion` now checks the widget state each poll and emits the report
as a `done` event the moment it appears. Verified by recovering three real
completed reports headlessly (45.6K / 52.4K / 51.5K chars). Raw stream and poll
payloads are intentionally never written to disk.

> **Light mode TODO:** the light `deep_research` (`model=research`) path uses the
> SearchGPT web-search backend, a *different* mechanism. It can still save the
> search-dispatch JSON (`{"queries": […]}`) as the body when the real report is a
> later/longer `done`; the bin script mitigates by picking the longest `done`.
> A dedicated light-mode fix is tracked separately.

Upstream tracking: see `gpt2agent/sse.py` (https://github.com/robotlearning123/gpt2agent/blob/main/gpt2agent/sse.py)
around `_dr_report_from_widget_state` / `_poll_dr_completion`.

## Quota management

Limits and reset timing are reported by the current account and can change.
Check before heavy calls:

```bash
~/.claude/skills/deep-research/bin/quota.sh
```

Refuse to fire `--heavy` if remaining < 10 unless the user explicitly
acknowledges the consumption.

## Account limits & concurrency (IMPORTANT — read before launching)

The DR *quota* shown by `quota.sh` is NOT the only limit. The ChatGPT backend
also rate-limits the conversation endpoints per account.

**Run heavy DR SERIALLY — never concurrently.** One heavy DR at a time. While a
heavy DR is running (it polls `/backend-api/conversation/{id}` every ~120s during
its 5–30 min Phase-2 wait), do NOT launch anything else that hits the same
account's chatgpt.com backend — a second heavy/light DR, `codex`/`cx` exec jobs,
agent mode, or `get_conversation`/`list_conversations` polling. Two pollers on
one account collide.

**Observed failure (2026-06-03):** running a heavy DR concurrently with several
`codex` jobs caused sustained **HTTP 429** on the poll for ~30 min, then the run
died with `ERROR  RuntimeError: DR polling timed out after 1800.0s waiting for
conv <id>` after only the initial metadata event was observed. The exact
per-account request-rate limit is not officially documented — do not assume a
number; just keep account access serial.

**Recovery from a 429 / poll-timeout (no extra quota):** the run almost always
*completed server-side* — only the local poll failed. The `conv_id` is in the
error line. Recover the finished report instead of re-running `--heavy` (which
would burn another quota): GET the conversation **with the hidden-widget flags**
`/backend-api/conversation/<conv_id>?include_visually_hidden_messages=true&include_widget_state=true`
via `BackendClient`, then read the connector's hidden widget state — the report
lives in `widget_state.report_message.content.parts[0]` (text) with
`report_message.metadata.content_references` (citation URLs). This is exactly what
`sse._dr_report_from_widget_state` does; heavy DR via the connector does **not**
write the report as an assistant text node, so walking `mapping` for assistant
text alone will miss it. Wait for the rate limit to ease first — repeated GETs
while rate-limited keep it hot.
