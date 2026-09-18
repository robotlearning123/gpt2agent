# gpt2agent — ChatGPT Plus/Pro MCP Tools

Best practices for using the 25 gpt2agent MCP tools that expose your ChatGPT
account inside any MCP client.

## Quick reference

| Task | Tool | Key params |
|---|---|---|
| Ask a model | `chat` | `model="gpt-6-pro"` (410K), `model="gpt-5-6"` (default) |
| Web research | `deep_research` | ~30–120s; inline citations; `auto_confirm=False` for interactive |
| Long-form research | `deep_research_heavy` | 5–30min; gpt-6-pro; **monthly quota** |
| Agent task | `agent` | autonomous browsing + code execution; 262K context |
| Generate image | `generate_image` | returns download URLs + metadata |
| Run code | `code_interpreter` | ChatGPT sandbox; returns output + charts |
| Canvas doc | `canvas_execute` | live editing environment |
| List models | `list_models` | 21 models with capabilities + thinking efforts |
| Read memories | `memory_list` / `memory_search(q=...)` | |
| Account info | `account_status` | plan, expiry, groups |
| Codex tasks | `list_codex_envs` / `list_codex_tasks` / `codex_task_create` | |

## Choosing a model

| Slug | Context | Best for |
|---|---|---|
| `gpt-6-pro` | 410K | Deepest reasoning; heavy DR; single effort tier |
| `gpt-5-6` | default | Fast, balanced; general chat |
| `gpt-5-6-thinking` | | Extra reasoning steps |
| `gpt-6-astra-wm` | 262K | Working-memory variant (may need special hints) |
| `o3-pro` | | Legacy pro reasoning |
| `agent-mode` | 262K | Autonomous browsing/execution (use `agent` tool) |

Run `list_models` first to see what your account has.

## Transport modes (every conversation tool)

```
REST (default)     → fastest; needs sentinel bridge set up
browser=True       → most reliable; needs "gpt2agent[browser]" + one-time login
manual=True        → zero-network; returns a JSON handoff for human paste
```

**Precedence**: `manual=True` > `browser=True` > REST.

## Critical invariants

1. **`temporary=True` (default) blocks tool-based features.** For image gen,
   code interpreter, canvas, or memory writes: pass `temporary=False`.
2. **Deep Research requires `temporary=False`** — it refuses to run in
   temporary chats ("Research is not currently supported in temporary chats").
3. **`gpt_chat` expects the `gizmo_id` from `list_custom_gpts`** — pass the
   `short_url` field. Currently only works with `g-` prefix (private) GPTs.
4. **DR quota is monthly** — check remaining before heavy work with
   `deep-research/bin/quota.sh`.
5. **Agent mode is async** — the `agent` tool polls the conversation; expect
   longer waits.

## Recipes

### Ask GPT-6 Pro a question
```
chat(prompt="...", model="gpt-6-pro", temporary=True)
```

### Web research with citations
```
deep_research(query="...", auto_confirm=True)
# Returns report + inline [N](url) citations + Sources section
```

### Deep research (long-form, uses monthly quota)
```
deep_research_heavy(query="...", auto_confirm=True)
# 5-30 minutes; gpt-6-pro; requires DR connector enabled in Settings
```

### Generate an image
```
generate_image(prompt="A red square on white background", temporary=False)
# temporary=False REQUIRED for image generation
```

### Run code
```
code_interpreter(prompt="Run: print(sum(range(100)))", temporary=False)
# Returns conversation_id + text + tool_responses + multimodal_assets
```

### Create a canvas document
```
canvas_execute(prompt="Create a React component that...", temporary=False)
```

### Agent mode (autonomous)
```
agent(prompt="Search for FNV-1a benchmarks and summarize with citations")
# 262K context; browsing + code execution; async polling
```

### Read conversation history
```
list_conversations(limit=5)          → get IDs
get_conversation(conversation_id="6a...")
```

### Search memories
```
memory_search(query="project deadlines")
```

### Manual fallback (when REST fails)
```
chat(prompt="...", manual=True)
# Returns: {status: "manual_handoff", prompt: "...", url: "https://chatgpt.com/", ...}
# Human pastes the prompt; read result back via list_conversations → get_conversation
```

### Browser fallback
```
chat(prompt="...", browser=True)
# Requires: pip install "gpt2agent[browser]" + [browser] enabled in config
```

## Account safety

- Keep volume human-scale (min 20s between turns)
- Use temporary chats by default
- One browser profile — never copy cookies
- On challenge/403: back off, don't retry-loop
- See docs/account-safety.md for full rules

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `UpstreamChallengeError` | Sentinel bridge not set up | Install bridge + `touch ~/.gpt2agent/sentinel-bridge/ENABLED` |
| Empty response | Transient bridge failure | Retry (bridge has 3-attempt backoff); or `browser=True` |
| DR "not supported" | `temporary=True` | Pass `temporary=False` |
| Image gen fails | `temporary=True` | Pass `temporary=False` |
| `gpt_chat` 422 | `g-p-` prefix GPT | Only `g-` prefix supported currently |
| Heavy DR empty | Connector not enabled | Enable Deep Research in chatgpt.com Settings |
| `astra-wm` empty | Model behavior | Use `gpt-6-pro` instead |
