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
account, so it is strictly loopback-only with no remote override. Native MCP Host
and Origin checks also reject non-loopback DNS-rebinding attempts. Prefer stdio.

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

### Are Apps, Plugins, and Skills the same thing?

No. `list_apps` reports connected Apps/connectors. `list_plugins` and
`list_installed_plugins` report the separate Plugin catalog and installation
state. MCP tools perform operations, MCP resources provide static context, and
the bundled Skills guide an agent in using those capabilities. The Claude Code
Plugin is a distribution bundle for the MCP server plus Skills.

### What are the two MCP resources?

`chatgpt://feature-coverage` and `chatgpt://update-evidence` are packaged,
deterministic JSON snapshots. Reading them never contacts ChatGPT. Use the
`account_capabilities` tool for live account reachability; its boolean fields
can be `null` when entitlement or reachability cannot be proven safely. It does
not fetch conversation summaries, memories, or custom instructions; use those
explicit tools only when you intend to read that private content.

### Where does my token go?

It's read locally from `$CODEX_HOME/auth.json` (or `~/.codex/auth.json` by
default) with `~/.gpt2agent/token.json` as the manual fallback. Codex manages
its auth file; gpt2agent creates or tightens the manual fallback to mode `600`
where POSIX supports it. The token is sent only to `chatgpt.com`, and gpt2agent
redacts token/secret values from error output.

### What's NOT supported?

Sora video, Operator/CUA, Projects, and Voice sessions. The Projects candidate
route is not an established adapter. Version 0.0.12 has no Voice tool, audio
stream, microphone capture, WebRTC session, or OpenAI API fallback. A bounded
read-only voice catalog is planned for 0.0.13; AgentRTC is later work.

The current official [Voice in ChatGPT](https://help.openai.com/en/articles/20001274)
guidance describes Live as a separate human-facing feature and says it does not
initially support connected apps or Plugins, Work, Codex, Custom GPTs, Temporary
Chats, or desktop. gpt2agent therefore does not present GPT-Live audio as one of
its 32 MCP tools or as a supported account capability.
