---
name: gpt2agent
description: |
  ChatGPT Plus/Pro account access via 32 MCP tools and 2 static resources covering chat,
  agent mode, deep research, image generation, code execution, canvas,
  memory, custom instructions, conversations, Custom GPTs, Codex, Apps,
  Plugins, Work models, automations, Sites, and capability truth.
  Reuses $CODEX_HOME/auth.json (or ~/.codex/auth.json) or the manual
  ~/.gpt2agent/token.json fallback.
  Use when you need ChatGPT models, web research with citations, DALL-E
  image gen, Python sandbox execution, or ChatGPT account management.
allowed-tools:
  - Bash
  - Read
  - Write
  - mcp__gpt2agent__chat
  - mcp__gpt2agent__agent
  - mcp__gpt2agent__deep_research
  - mcp__gpt2agent__deep_research_heavy
  - mcp__gpt2agent__gpt_chat
  - mcp__gpt2agent__generate_image
  - mcp__gpt2agent__get_file_info
  - mcp__gpt2agent__get_file_download_url
  - mcp__gpt2agent__code_interpreter
  - mcp__gpt2agent__canvas_execute
  - mcp__gpt2agent__account_status
  - mcp__gpt2agent__list_models
  - mcp__gpt2agent__list_conversations
  - mcp__gpt2agent__get_conversation
  - mcp__gpt2agent__list_tasks
  - mcp__gpt2agent__list_apps
  - mcp__gpt2agent__list_custom_gpts
  - mcp__gpt2agent__memory_list
  - mcp__gpt2agent__memory_search
  - mcp__gpt2agent__memory_create_via_chat
  - mcp__gpt2agent__custom_instructions_get
  - mcp__gpt2agent__custom_instructions_set
  - mcp__gpt2agent__list_codex_envs
  - mcp__gpt2agent__list_codex_tasks
  - mcp__gpt2agent__codex_task_create
  - mcp__gpt2agent__list_scheduled_tasks
  - mcp__gpt2agent__list_plugins
  - mcp__gpt2agent__list_installed_plugins
  - mcp__gpt2agent__list_work_models
  - mcp__gpt2agent__sites_access
  - mcp__gpt2agent__list_sites
  - mcp__gpt2agent__account_capabilities
---

# gpt2agent — ChatGPT Account Access via MCP

```!
command -v gpt2agent >/dev/null && echo "gpt2agent: installed ($(gpt2agent --version 2>/dev/null || echo 'unknown version'))" || echo "gpt2agent: NOT INSTALLED — run: pipx install gpt2agent"
if test -f "${CODEX_HOME:-$HOME/.codex}/auth.json"; then
  echo "codex token: present"
elif test -f "$HOME/.gpt2agent/token.json"; then
  echo "gpt2agent token: present (manual fallback)"
else
  echo "ChatGPT token: MISSING — run: codex login or gpt2agent setup"
fi
```

## Preconditions

1. `gpt2agent` installed via pipx. If missing: `pipx install gpt2agent`
2. Auth token at `$CODEX_HOME/auth.json` (or `~/.codex/auth.json`, from
   `codex login`) or `~/.gpt2agent/token.json`.
3. MCP server registered in Claude Code. If missing: `gpt2agent install`
4. Restart Claude Code session after first install to load MCP tools.

If any precondition fails, stop and tell the user the exact fix command.

## Quick Reference

| Category | Tools |
|---|---|
| Chat & reasoning | `chat`, `agent`, `deep_research`, `deep_research_heavy`, `gpt_chat` |
| Image & file | `generate_image`, `get_file_info`, `get_file_download_url` |
| Code execution | `code_interpreter`, `canvas_execute` |
| Account & models | `account_status`, `list_models`, `list_work_models`, `account_capabilities` |
| Apps & Plugins | `list_apps`, `list_plugins`, `list_installed_plugins` |
| Conversations & jobs | `list_conversations`, `get_conversation`, `list_tasks`, `list_scheduled_tasks` |
| Sites | `sites_access`, `list_sites` |
| Custom GPTs | `list_custom_gpts`, `gpt_chat` |
| Memory | `memory_list`, `memory_search`, `memory_create_via_chat` |
| Instructions | `custom_instructions_get`, `custom_instructions_set` |
| Codex | `list_codex_envs`, `list_codex_tasks`, `codex_task_create` |

## When to Use Which Tool

| Need | Tool | Notes |
|---|---|---|
| Quick Q&A with a ChatGPT model | `chat` | Default: gpt-5-3, temporary=True; optional model-aware thinking_effort |
| Chat that needs image gen / code / canvas | `chat(temporary=False)` | Temporary chats block these features |
| Multi-step task with browsing + code exec | `agent` | 262K context, autonomous |
| Web research with citations (30-120s) | `deep_research` | Costs 1 DR quota |
| Long-form research report (5-30 min) | `deep_research_heavy` | Costs 1 DR quota, use background mode |
| Generate an image | `generate_image` | DALL-E, requires temporary=False context |
| Run Python in sandbox | `code_interpreter` | Requires temporary=False |
| Live document editing | `canvas_execute` | Requires temporary=False |
| Use a Custom GPT | `gpt_chat(gizmo_id, prompt)` | List IDs with `list_custom_gpts` |
| Save something to ChatGPT memory | `memory_create_via_chat` | Model-initiated write (REST 405 workaround) |
| Create a Codex coding task | `codex_task_create` | Auto-resolves environment_id from repo_label |
| Check current account truth | `account_capabilities` | Tri-state entitlement/reachability, shape-only output |
| Inspect scheduled automation | `list_scheduled_tasks` | Distinct from generic `list_tasks` jobs |
| Inspect Plugin state | `list_plugins`, `list_installed_plugins` | Plugins are distinct from Apps/connectors |

## Usage Patterns

### Research workflow
```
1. deep_research("topic")          -- fast, citations included
2. deep_research_heavy("topic")    -- long-form, run in background
3. Read result, summarize for user
```

For deep_research_heavy, use `run_in_background: true` in Bash. It takes 5-30 minutes.

### Image generation
```
1. chat("describe what you want", temporary=False)  -- or generate_image directly
2. generate_image("prompt")  -- returns file_id
3. get_file_download_url(file_id)  -- get the URL
```

### Code execution
```
1. code_interpreter("python code here")  -- runs in ChatGPT sandbox
2. canvas_execute(content="...")  -- live document editing
```

Both require a non-temporary conversation context.

### Account inspection
```
1. account_status()   -- plan, features, expiry
2. list_models()      -- all available model slugs
3. list_work_models() -- separate Work-only catalog
4. account_capabilities() -- live shape-only truth
5. list_conversations() -- recent chat history
```

Do not pass a Work-only slug to `chat` or configure it for `agent`. Optional
`thinking_effort` values must appear in the selected general model's live
catalog; leave the value unset to preserve the model default.

### Apps, Plugins, jobs, and resources
```
1. list_apps()              -- connected Apps/connectors
2. list_plugins()           -- Plugin catalog
3. list_installed_plugins() -- installed Plugins
4. list_tasks()             -- generic asynchronous jobs
5. list_scheduled_tasks()   -- scheduled automations
```

MCP resource discovery also exposes `chatgpt://feature-coverage` and
`chatgpt://update-evidence`. They are packaged static JSON and never contact the
account. Use `account_capabilities` for live state.

### Codex integration
```
1. list_codex_envs()              -- see available repos
2. list_codex_tasks()             -- recent tasks
3. codex_task_create(repo, desc)  -- spin up a coding task
```

## Quota Management

Deep Research limits and reset timing are reported by the current account and
can change.

- `deep_research` and `deep_research_heavy` each cost 1 quota.
- Run the bundled `deep-research/bin/quota.sh` before heavy usage.
- Warn user if remaining quota < 10 before firing deep research.

## Configuration

Config file locations (checked in order):
1. `~/.gpt2agent/config.toml`
2. `./config.toml` (cwd)
3. `~/.config/gpt2agent/config.toml`

Key settings:
```toml
[server]
host = "127.0.0.1"   # retained for compatibility; stdio does not bind
port = 9000          # retained for compatibility; stdio does not bind

[models]
chat = "gpt-5-3"
# agent = "agent-mode"
# heavy_dr = "gpt-5-5-pro"
```

Ordinary REST/JSON backend calls share a process-wide limit of 4. Set
`GPT2AGENT_MAX_IN_FLIGHT` only to an integer from 1 through 8. A 429 activates a
route-local cooldown capped at 60 seconds. Direct SSE/Sentinel streams are not
held by this semaphore; endpoint timeouts bound them, and heavy Deep Research
should run serially.

## Troubleshooting

| Error | Fix |
|---|---|
| "gpt2agent not installed" | `pipx install gpt2agent` |
| "ChatGPT token: MISSING" | `codex login` or `gpt2agent setup` |
| MCP tools not appearing | Restart Claude Code session after `gpt2agent install` |
| 401 / auth error | Re-run `codex login` or `gpt2agent setup`, then restart the MCP server |
| Image/code/canvas fails | Ensure `temporary=False` — these features are blocked in temporary chats |
| DR connector unavailable | Enable Deep Research at chatgpt.com > Settings > Connectors |
| "memory_add not available" | Use `memory_create_via_chat` instead (REST POST returns 405) |
| `contract_changed` | Private response no longer satisfies the bounded adapter; update and inspect sanitized evidence |
| HTTP transport disabled | Expected in 0.0.12: use stdio |
| Invalid `thinking_effort` | Call `list_models`; use an effort advertised by that exact general model or leave it unset |

## Detailed Reference

For full parameter docs and return types, see:
`gpt2agent/skills/gpt2agent/tools-reference.md`

Or inspect tool schemas directly: `mcp__gpt2agent__*` tools are self-describing via MCP.
