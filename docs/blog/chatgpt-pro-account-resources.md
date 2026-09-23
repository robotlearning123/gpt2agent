# What a ChatGPT Pro account actually contains — a measured inventory

Most "what do you get with ChatGPT Pro?" answers list features. This one lists
**numbers**: how many models, how much context, how many requests, how much
memory, which counters reset when. Everything below was measured against a live
ChatGPT Pro account on **2026-09-23** with [`gpt2agent`](https://github.com/robotlearning123/gpt2agent)
v0.0.21. Most numbers come from read-only calls the web client itself makes
(`/backend-api/conversation/init` for quotas, `list_models` for the roster,
`/backend-api/memories` for the memory budget); one finding — the
working-memory slug behaviour in §2 — required a handful of one-word
conversation probes, which do spend a message each.

Two caveats before the numbers. **First**, this is one account on one date:
counters reset, models roll out per tier, and OpenAI changes both without
notice — treat the shape as the point, not the digits. **Second**, driving this
programmatically is a reverse-engineered path and carries real account risk
(see [Account safety](../account-safety.md)); the inventory below is what a
*normal* account exposes, not an argument to hammer it.

## The stack, top-down

```
subscription     plan, feature flags (69 enabled on this account)
  └─ models      23 slugs: 128K–410K context for chat models
      └─ capabilities  chat, agent, deep research (light/heavy), code, canvas,
                       image gen, browsing/connectors, memory
          └─ quotas    per-feature counters, each with its own reset time
              └─ data  conversations, tasks, memory, GPTs, apps/connectors, Codex
                  └─ access  gpt2agent: 30 MCP tools, three per-call transports
```

## 1. The subscription

`chatgptpro`, active, not delinquent. The account reports **69 enabled feature
flags** — `code_interpreter_available`, `canvas*`, `dalle_3` /
`image_gen_tool_enabled`, `plugins_available`, `search_tool`, and so on. The
plan name is marketing; the feature list is the machine-readable version of
what the backend will serve you.

## 2. Models: 23 slugs

The account's model list, verbatim slugs and context windows:

| Slug | Title | Context | Reasoning |
|---|---|---:|---|
| `gpt-6-pro` | GPT-6 Pro | 410,000 | pro |
| `gpt-5-6-pro` | GPT-5.6 Pro | 410,000 | pro |
| `gpt-5-5-pro` | GPT-5.5 Pro | 410,000 | pro |
| `o3-pro` | o3-pro | 196,608 | pro |
| `gpt-5-5-thinking` | GPT-5.5 Thinking | 410,000 | reasoning |
| `gpt-5-6-thinking` | GPT-5.6 Sol | 262,144 | reasoning |
| `gpt-5-4-t-mini` | GPT-5.4 Thinking Mini | 262,144 | reasoning |
| `gpt-5-6-t-mini` | GPT-5.6 Luna | 262,144 | reasoning |
| `gpt-5-6` | GPT-5.6 Sol | 137,000 | auto |
| `gpt-5-6-instant` | GPT-5.6 Sol | 137,000 | — |
| `gpt-5-5` | GPT-5.5 | 137,000 | auto |
| `gpt-5-5-instant` | GPT-5.5 Instant | 137,000 | — |
| `gpt-5-5-mini` | GPT-5.5 Mini | 137,000 | — |
| `gpt-5-6-mini` | GPT-5.6 Luna | 137,000 | — |
| `gpt-5-3-mini` | GPT-5.3 Mini | 128,000 | — |
| `research` | Deep Research | 34,815 | — |
| `gpt-6-sol-wm` | GPT-6 Sol | 262,144 | reasoning (working-memory) |
| `gpt-6-luna-wm` | GPT-6 Luna | 262,144 | reasoning (working-memory) |
| `gpt-6-astra-wm` | GPT-6 Astra | 262,144 | reasoning (working-memory) |
| `gpt-5.6-sol-wm` | GPT-5.6 Sol | 262,144 | reasoning (working-memory) |
| `gpt-5.6-terra-wm` | GPT-5.6 Terra | 262,144 | reasoning (working-memory) |
| `gpt-5.6-luna-wm` | GPT-5.6 Luna | 262,144 | reasoning (working-memory) |
| `gpt-5.5-wm` | GPT-5.5 | 262,144 | reasoning (working-memory) |

The roster distinguishes four `reasoning_type` values — `pro`, `reasoning`,
`auto`, `none` — and the working-memory (`-wm`) slugs carry `reasoning` plus a
`is_work_mode_model: true` marker. Chat models span 128K–410K context; the
`research` slug advertises the smallest window (34,815), because it drives the
deep-research pipeline rather than free-form chat.

Three things worth knowing:

- The account's **default slug is `gpt-6-pro`** (410K, pro reasoning) — that is
  the ChatGPT UI/backend default. gpt2agent's own `chat` tool ships its own
  default (`gpt-5-6`, configurable via `[models].chat`), so a `chat` call that
  omits `model=` does **not** use the account default.
- **Title ≠ slug.** Several slugs carry renamed titles (`gpt-5-6-mini` is titled
  "GPT-5.6 Luna", `gpt-5-6` is titled "GPT-5.6 Sol"). If you key on titles you
  will mis-route; key on slugs.
- The `-wm` (working-memory) slugs are listed, but the three that were probed —
  `gpt-6-sol-wm`, `gpt-6-luna-wm`, `gpt-6-astra-wm` (and plain `gpt-6-sol` /
  `gpt-6-luna`) — were answered by `gpt-5-6` on Chat instead, with the client
  noting the substitution. The other four work-mode slugs were not
  completion-probed, and a substituted answer alone does not prove a product
  block; the one official availability statement in play covers Sol and Luna:
  GPT-6 Sol and GPT-6 Luna are ChatGPT **Work and Codex-only** — "They aren't
  available in Chat" ([ChatGPT models
  page](https://learn.chatgpt.com/docs/models)). A tool that does not compare
  the *resolved* model against the *requested* one will silently report the
  wrong model's answer.

Also on the clock: **GPT-5.5 retires from ChatGPT, ChatGPT Work, and Codex on
2026-10-14**; the OpenAI API is unaffected
([same page](https://learn.chatgpt.com/docs/models)). The official note names
`gpt-5.5`; the `gpt-5-5*` and `gpt-5.5-wm` rows above belong to that line —
check the note for the exact per-variant list before pinning any of them.

## 3. Capabilities and their real quotas

The quotas are not in the plan page; the backend exposes them per feature, each
with its own reset time. Measured on this account:

| Resource | Where it comes from | Measured value |
|---|---|---|
| Deep research (light) | `limits_progress` counter | **181 remaining**, resets 2026-10-17 (~24 days after this reading) |
| Deep research (heavy) | separate budget | **no counter reported** (`heavy_remaining: null`) — this snapshot cannot say what gates it |
| Image generation | `limits_progress` counter | **1,000 remaining**, resets 2026-09-24 (~24 h after this reading) |
| Memory storage | `/backend-api/memories` | **455 tokens used of a 5,000,000-token budget** (5 entries) |
| Per-model caps | `model_limits` | **none active** (`capped: []`) — no model currently throttled |
| Default-model downgrade | `intended` vs `default` slug | **not downgraded** |

Two lessons from these numbers. The **counters reset on their own schedules** —
in this snapshot roughly a day for image generation and roughly three weeks for
light deep research — so "how much do I have left" is a per-feature question,
not one number. And the **heavy deep-research path does not report the same
counter** as the light one; a client that gates both on the light counter will
treat two independent budgets as one.

On top of the account's own limits, a well-behaved client adds its own pacing.
gpt2agent ships a file-backed budget so a fleet of agents shares one queue:
**100 conversation posts per 3-hour window, minimum 15 s between posts**, with
`429`s escalating cooldowns instead of retries.

## 4. The account's data surfaces

Beyond conversations, a Pro account carries several addressable stores:

| Surface | Count on this account | What it holds |
|---|---:|---|
| Apps & connectors | 107 | official connectors + third-party Apps SDK entries |
| Conversations | 5 recent (list capped at `limit=5`) | full message history, multimodal, incl. deep-research reports |
| Tasks | 1 | scheduled/completed task runs with prompt and final message |
| Memory entries | 5 | the persistent memory store (5M-token budget above) |
| Custom GPTs | 0 | private Custom GPTs (this account has none) |
| Codex environments / tasks | 0 / 0 | Codex cloud workspaces and their runs |

Counts are per account — and the conversation count is a probe artifact: the
read asks for a small page. The shape is what matters. A Pro account can
address the same stores the web UI shows you, which is why an MCP server over
this backend can offer memory search, conversation readback, and connector use
rather than just chat.

## 5. The access layer: 30 tools

Wrapped as an MCP server (stdio or streamable-HTTP transport), the surfaces
above become **30 tools**. The conversation tools:

`chat` · `agent` (browsing + code execution; the project docs describe a 262K
context) · `deep_research`
(inline citations) · `deep_research_heavy` (connector-backed) · `gpt_chat`
(your Custom GPTs) · `code_interpreter` · `canvas_execute` · `generate_image` ·
`memory_create_via_chat`

Account and data tools:

`account_status` · `usage_stats` · `list_models` · `memory_list` ·
`memory_search` · `custom_instructions_get` · `custom_instructions_set` ·
`list_conversations` · `get_conversation` · `list_tasks` · `list_apps` ·
`list_custom_gpts` · `list_codex_envs` · `list_codex_tasks` ·
`codex_task_create` · `get_file_info` · `get_file_download_url`

Queue tools (serialize work across a fleet):

`queue_submit` · `queue_status` · `queue_result` · `queue_cancel`

Each conversation tool also takes per-call transport flags: `manual=True`
returns a zero-network handoff (the exact prompt and URL for a human to paste),
and `browser=True` drives a real Chrome instead of the direct backend call.

What a Pro account does **not** expose through this path, as of today: Sora
video, Operator/CUA sessions, voice sessions, Projects, Tasks (write), and file
upload — the file tools read files that already exist. Sol and Luna are
selected inside the ChatGPT **Work** and **Codex** apps themselves; the Codex
tools here create tasks but carry no model parameter.

## 6. Measure your own

Everything above is reproducible on your own account:

```bash
gpt2agent doctor     # which surfaces answer right now (no quota spent)
gpt2agent usage      # quotas, counters, resets, subscription state
# and from an MCP client: the list_models and usage_stats tools
```

The memory-budget figures come from the same endpoint `memory_list` reads
(`/backend-api/memories`): the tool returns the entries, and the budget fields
(`memory_num_tokens`, `memory_max_tokens`) ride along in the raw response.

A final warning that belongs in any inventory of this kind: this access path is
unofficial, and automating it can get an account rate-limited or banned. Keep
volume human-scale, keep one identity per account, and read
[docs/account-safety.md](../account-safety.md) before pointing anything
long-running at it.
