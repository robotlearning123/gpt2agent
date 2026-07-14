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

stdio runs the server as a child process of your client, with no listening TCP
port. Version 0.0.12 disables HTTP because loopback sockets remain reachable by
other users and processes on the host; Host/Origin checks do not authenticate
the account owner.

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

### Does Grok Build reuse my ChatGPT login or an xAI API key?

No. The three Grok Build tools use the official CLI's independent
subscription/OAuth state. gpt2agent strips `XAI_API_KEY` and
`GROK_CODE_XAI_API_KEY` from the child environment and never falls back between
ChatGPT auth, Grok Build OAuth, or a website session. Optional `GROK_HOME` and
`GROK_AUTH_PATH` locations are configured as paths, not pasted credentials.

Build remains disabled while `roots = []`. Once an explicit root is configured,
`grok_build_status` and `grok_build_models` are read-only; `grok_build_agent`
defaults to plan mode and requires an explicit `mode="apply"` choice for source
changes. The official CLI retains session history. gpt2agent returns a sanitized
session ID but does not copy transcripts or expose resume/deletion tools.

See xAI's [enterprise/authentication guidance](https://docs.x.ai/build/enterprise),
[CLI reference](https://docs.x.ai/build/cli/reference), [headless scripting
guide](https://docs.x.ai/build/cli/headless-scripting), and [modes and
commands](https://docs.x.ai/build/modes-and-commands).

### What's NOT supported?

Sora video, Operator/CUA, Projects, and Voice sessions. The Projects candidate
route is not an established adapter. Version 0.0.12 has no Voice tool, audio
stream, microphone capture, WebRTC session, or OpenAI API fallback. A bounded
read-only voice catalog is planned for 0.0.13; AgentRTC is later work.

The current official [Voice in ChatGPT](https://help.openai.com/en/articles/20001274)
guidance describes Live as a separate human-facing feature and says it does not
initially support connected apps or Plugins, Work, Codex, Custom GPTs, Temporary
Chats, or desktop. gpt2agent therefore does not present GPT-Live audio among its
registered tools or as a supported account capability.
