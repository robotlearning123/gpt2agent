# FAQ

### Is this official / affiliated with OpenAI?

No. gpt2agent is an independent, unofficial project. It talks to ChatGPT's private
backend the way the web client does (TLS impersonation + the Sentinel
proof-of-work/Turnstile challenge). There is no official API behind it.

### Will this get my account banned?

It might. Using a reverse-engineered client likely violates the OpenAI Terms of
Service, and automated/abnormal traffic can get an account rate-limited,
challenged, suspended, or banned. Use an account you can afford to lose, keep
volume human-scale, and don't depend on it for anything critical. See the README's
**Security & risk** section.

### Why stdio instead of HTTP?

stdio runs the server as a local subprocess of your client — nothing is exposed on
the network. The HTTP transport has **no authentication** and proxies your entire
account, so it binds loopback only and refuses non-loopback hosts unless you opt in
with `GPT2AGENT_ALLOW_REMOTE=1`. Prefer stdio.

### Plus vs Pro — what's the difference?

Both work. Pro unlocks the heavier models (e.g. `gpt-6-pro`, `o3-pro`) and a
larger monthly Deep Research quota (GPT-5.5 models retire from all ChatGPT
surfaces on 2026-10-14). Run `list_models` to see exactly what your
account has, and `account_status` for your plan.

### Can I use GPT-6 Sol / GPT-6 Luna?

Not through the Chat surface. OpenAI docs (2026-09-22) put GPT-6 Sol
(`gpt-6-sol`) and GPT-6 Luna (`gpt-6-luna`) in ChatGPT **Work and Codex only** —
they are not available in Chat. Measured 2026-09-23: a Chat request for
`gpt-6-sol`, `gpt-6-luna`, or their `-wm` variants is silently served by
`gpt-5-6`, and the reply carries a *Model note* naming the resolved slug. Use
`gpt-6-pro` / `gpt-5-6` for Chat work; the ChatGPT Work and Codex apps are
where Sol and Luna are selectable.

### How much Deep Research can I run?

Limits and reset timing are account-reported and can change. Run the bundled
`deep-research/bin/quota.sh` to inspect the current account before heavy work,
and run heavy Deep Research serially.

Measured billing model (2026-09-24, two Pro accounts; receipts in
`artifacts/verify/dr-2acct-recovery-2026-09-23.md`):

- **Light DR** (`deep_research`): 1 per **completed** search turn from the
  account's monthly `deep_research` bucket — turns that abort in-band cost 0.
- **Heavy DR** (`deep_research_heavy`): an **independent** monthly cap (the
  backend reports it under a `deep_research_*` variant when it exposes it);
  two full heavy reports left the light bucket unmoved. The authoritative
  exhaustion signal is the in-stream `usage_limit` frame.
- **Conversation posts** share a paced window (~100 per 3 h, 15 s min
  interval, enforced client-side across processes).

Live remaining/reset numbers: `gpt2agent usage`.

### Is `gpt_chat` (Custom GPTs) stable?

It's **experimental**. Pass the `short_url` returned by `list_custom_gpts` as the
`gizmo_id`. The payload field is reverse-engineered and not load-tested across all
Custom GPT types.

### Where does my token go?

It's read locally from `$CODEX_HOME/auth.json` (or `~/.codex/auth.json` by
default) with `~/.gpt2agent/token.json` as the manual fallback. Codex manages
its auth file; gpt2agent creates or tightens the manual fallback to mode `600`
where POSIX supports it. The token is sent only to `chatgpt.com`, and gpt2agent
redacts token/secret values from error output.

### What's NOT supported?

Sora video, Operator/CUA, and voice sessions — those endpoints aren't reverse-engineered
yet. Everything else (chat, agent mode, deep research, image gen, code interpreter,
canvas, memory, custom instructions, Codex tasks) is exposed via the 30 MCP tools.
