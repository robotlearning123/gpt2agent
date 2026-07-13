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

Both work. Pro unlocks the heavier models (e.g. `gpt-5-5-pro`, `o3-pro`) and a
larger monthly Deep Research quota. Run `list_models` to see exactly what your
account has, and `account_status` for your plan.

### How much Deep Research can I run?

Limits and reset timing are account-reported and can change. Run the bundled
`deep-research/bin/quota.sh` to inspect the current account before heavy work,
and run heavy Deep Research serially.

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

Sora video and Operator/CUA are not supported. The read-only `list_voices` tool
does expose the account's current Voice catalog, but that is not a Voice
session: GPT-Live realtime audio, microphone/playback transport, speech
synthesis, and guaranteed transcript extraction remain unsupported. The
catalog route is a private website contract and may change. The server exposes
30 MCP tools in total (including optional GPT-Live bridge control tools).
