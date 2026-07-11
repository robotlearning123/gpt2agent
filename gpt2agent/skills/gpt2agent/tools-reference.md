# gpt2agent MCP Tools Reference

Complete parameter reference for all 31 MCP tools exposed by the gpt2agent server.
Source: `gpt2agent/server.py` and `gpt2agent/tools/*.py`.

---

## Table of Contents

- [Chat & Reasoning (5)](#chat--reasoning)
- [Image & File (3)](#image--file)
- [Code Execution (2)](#code-execution)
- [Account Introspection (8)](#account-introspection)
- [Memory & Instructions (5)](#memory--instructions)
- [Codex (3)](#codex)
- [GPT-Live Mode B export (5)](#gpt-live-mode-b-export)

---

## Chat & Reasoning

### chat

- **Purpose**: Send a single prompt to any ChatGPT model and get a text response.
- **Parameters**:
  - `prompt` (str, required) -- the user message to send.
  - `model` (str, default: value from `config.toml` `[models].chat`, fallback `"gpt-5-3"`) -- model slug. Run `list_models` to see all available slugs.
  - `temporary` (bool, default: `True`) -- when `True`, sets `history_and_training_disabled=True` which prevents the conversation from being saved and **blocks tool-based features** (image gen, code interpreter, canvas, memory persistence). Set `False` to enable those features.
- **Returns**: `str` -- the assistant's reply text.
- **When to use**: General Q&A, text generation, translation, summarization. Default choice for single-turn queries.
- **Example**:
  ```python
  chat("Explain the difference between LTP and LTD in hippocampal neurons.")
  chat("Summarize this abstract:", model="o3")
  chat("Generate a chart of this data", model="gpt-5-3", temporary=False)
  ```
- **Notes**:
  - `temporary=True` (default) means the conversation is ephemeral -- not saved to ChatGPT history, cannot use image gen / code interpreter / canvas.
  - If you need tool-based features (image gen, code interpreter, canvas), you **must** pass `temporary=False`.
  - Available model slugs depend on your subscription tier. Pro plan unlocks `gpt-5-5-pro`, `gpt-5-4-pro`, `o3-pro`, etc.

---

### agent

- **Purpose**: ChatGPT Agent Mode -- autonomous browsing, code execution, and tool use with 262K context window.
- **Parameters**:
  - `prompt` (str, required) -- the task description.
- **Returns**: `str` -- the agent's response text.
- **When to use**: Multi-step tasks requiring browsing, code execution, or tool orchestration. Literature gathering, document workflows, browser automation.
- **Example**:
  ```python
  agent("Find the top 5 recent papers on calcium imaging in behaving mice, "
        "extract their methods sections, and compile a comparison table.")
  ```
- **Notes**:
  - Always uses `temporary=False` internally (tool-based features required).
  - Uses the `agent-mode` slug by default (configurable via `[models].agent` in `config.toml`).
  - SSE-only transport (no REST fallback).
  - Long-running tasks may take several minutes.

---

### deep_research

- **Purpose**: Web-augmented research with citations. Searches the web and synthesizes a detailed report.
- **Parameters**:
  - `query` (str, required) -- the research question or topic.
  - `auto_confirm` (bool, default: `True`) -- when `True`, prepends an imperative prefix so the model starts immediately without asking "Do you want me to start?".
- **Returns**: `str` -- the research report text, followed by a `---\n**Sources:**` section with markdown links to cited URLs.
- **When to use**: Current events, literature review, market research, any question needing web-augmented multi-source synthesis.
- **Example**:
  ```python
  deep_research("Compare optogenetics vs. chemogenetics for circuit dissection "
                "in primates. Include recent 2025-2026 advances.")
  ```
- **Notes**:
  - Uses `model='research'` + `system_hints=['research']` internally (resolves to i-mini-m / SearchGPT backend).
  - Takes 30-120 seconds typically.
  - `history_and_training_disabled` is forced to `False` for DR (ChatGPT refuses DR in temporary chats).
  - The `auto_confirm` prefix is: "Begin the deep research immediately without asking for confirmation. Do not ask clarifying questions; proceed with the best interpretation."
  - If the model asks a clarification question instead of researching, the tool detects it via heuristics and can auto-proceed.

---

### deep_research_heavy

- **Purpose**: Long-form Deep Research using gpt-5-5-pro with the DR connector. Produces extended multi-section reports.
- **Parameters**:
  - `query` (str, required) -- the research question.
  - `auto_confirm` (bool, default: `True`) -- same behavior as `deep_research`.
- **Returns**: `str` -- the long-form report. Sources section appended if citations are available. May include a connector-unavailable warning if the DR connector fails.
- **When to use**: Big strategic questions (>5 sub-questions), topics expecting 50+ sources, questions needing 10+ KB reports.
- **Example**:
  ```python
  deep_research_heavy(
      "Comprehensive analysis of brain-computer interface technologies for "
      "speech restoration: invasive vs. semi-invasive approaches, clinical "
      "trial status 2024-2026, regulatory pathways, and comparison of "
      "decoding accuracy benchmarks."
  )
  ```
- **Notes**:
  - **Quota**: limits and reset timing are account-reported and can change. Run the bundled `deep-research/bin/quota.sh` before heavy calls.
  - Takes 5-30 minutes. Use `run_in_background` for shell integration.
  - Uses `/backend-api/f/conversation` (frontend endpoint), not the standard `/backend-api/conversation`.
  - Model slug configurable via `[models].heavy_dr` in `config.toml` (default: `gpt-5-5-pro`).
  - **Report + citations recovered from the connector widget state** (fixed in 0.0.4): the connector never writes the report as an assistant text node, so the poll fetches the conversation with `include_widget_state=true` and recovers `widget_state.report_message` (text + `content_references`). Grouped source URLs are usually present but not guaranteed; if absent, the model may have cited sources inline in the body.
  - If the DR connector is unavailable, a warning is appended explaining how to enable it in chatgpt.com Settings > Connectors.

---

### gpt_chat

- **Purpose**: Chat through a private Custom GPT, applying its instructions, files, and memory scope.
- **Parameters**:
  - `gizmo_id` (str, required) -- pass the `short_url` returned by `list_custom_gpts` (format: `g-*`). Call `list_custom_gpts` first to enumerate them.
  - `prompt` (str, required) -- the user message.
- **Returns**: `str` -- the Custom GPT's response text.
- **When to use**: When you need a specialized GPT's persona, knowledge base, or tool access.
- **Example**:
  ```python
  # First, find available GPTs
  gpts = list_custom_gpts()                  # each has {name, short_url}
  # Then use one's short_url as the gizmo_id
  gpt_chat(gpts[0]["short_url"], "Analyze this dataset for seasonal trends.")
  ```
- **Notes**:
  - **EXPERIMENTAL**: passes `gizmo_id` via the `conversation_origin` field, reverse-engineered from chatgpt.com web bundles.
  - Always uses `temporary=False` internally.
  - The gizmo's custom instructions, uploaded files, and memory scope apply to the conversation.
  - Only works with your **private** Custom GPTs (not public ones you've used).

---

## Image & File

### generate_image

- **Purpose**: Generate an image using ChatGPT's built-in image generation (DALL-E).
- **Parameters**:
  - `prompt` (str, required) -- description of the image to generate.
  - `model` (str, default: `"gpt-5-3"`) -- model to use (must have `image_gen_tool_enabled`).
- **Returns**: `dict` with keys:
  - `conversation_id` (str)
  - `assets` (list) -- each asset contains: `asset_pointer`, `file_id`, `width`, `height`, `size_bytes`, `download_url`, `file_name`, `mime_type`
  - `metadata` (dict)
- **When to use**: Illustrations, diagrams, concept art, visual aids.
- **Example**:
  ```python
  generate_image("A detailed diagram of the hippocampal trisynaptic circuit "
                 "with labeled regions CA1, CA3, dentate gyrus, and entorhinal cortex.")
  ```
- **Notes**:
  - Uses `temporary=False` internally (image gen requires persistent conversation).
  - Timeout: 300 seconds for complex prompts.
  - Each asset automatically gets a `download_url` fetched from `/backend-api/files/{file_id}/download`.
  - Download URLs are temporary (~1 hour expiry). Use `get_file_download_url` to refresh.
  - If `conv` is not injected, the tool creates its own `ConversationClient` instance.

---

### get_file_info

- **Purpose**: Get metadata for any ChatGPT file (images, uploads, etc.).
- **Parameters**:
  - `file_id` (str, required) -- the file ID (e.g., `file_00000000c02471f88295cda5f3b8c66b`).
- **Returns**: `dict` with keys: `id`, `name`, `size`, `use_case`, `state`, `creation_time`, `mime_type`, etc.
- **When to use**: Inspect a file's metadata before downloading, check file state, get file name.
- **Example**:
  ```python
  get_file_info("file_00000000c02471f88295cda5f3b8c66b")
  ```
- **Notes**:
  - Async handler; offloads `GET /backend-api/files/{file_id}` to the synchronous backend client.
  - Returns `{}` if the file doesn't exist or the request fails.

---

### get_file_download_url

- **Purpose**: Get a temporary download URL for a ChatGPT file.
- **Parameters**:
  - `file_id` (str, required) -- the file ID.
- **Returns**: `str` -- the download URL. Empty string if not found.
- **When to use**: Download an image or file generated by ChatGPT. Refresh expired download URLs.
- **Example**:
  ```python
  url = get_file_download_url("file_00000000c02471f88295cda5f3b8c66b")
  # url = "https://cdn.oaistatic.com/..."
  ```
- **Notes**:
  - URLs expire after approximately 1 hour.
  - Async handler; offloads `GET /backend-api/files/{file_id}/download` to the synchronous backend client.

---

## Code Execution

### code_interpreter

- **Purpose**: Execute Python code in ChatGPT's sandboxed code interpreter.
- **Parameters**:
  - `prompt` (str, required) -- the code or instruction to execute (e.g., `"Run this Python code: ..."`).
  - `model` (str, default: `"gpt-5-3"`) -- model to use.
- **Returns**: `dict` with keys:
  - `conversation_id` (str)
  - `text` (str) -- assistant's explanation of the output
  - `tool_calls` (list) -- code execution calls made
  - `tool_responses` (list) -- execution results
  - `multimodal_assets` (list) -- any charts/images generated
- **When to use**: Run Python code, data analysis, math computations, generate charts.
- **Example**:
  ```python
  code_interpreter(
      "import numpy as np\n"
      "data = np.random.normal(0, 1, 1000)\n"
      "print(f'mean={data.mean():.3f}, std={data.std():.3f}')\n"
      "Plot a histogram."
  )
  ```
- **Notes**:
  - Uses `temporary=False` internally (code interpreter requires persistent conversation).
  - Runs in ChatGPT's server-side sandbox -- no local filesystem access.
  - Can produce charts/images as `multimodal_assets`.
  - If `conv` is not injected, creates its own `ConversationClient` instance.

---

### canvas_execute

- **Purpose**: Execute code via ChatGPT's Canvas feature -- a live editing environment.
- **Parameters**:
  - `prompt` (str, required) -- the code or instruction (e.g., `"Create a React component that..."`).
  - `model` (str, default: `"gpt-5-3"`) -- model to use.
- **Returns**: `dict` with keys: `conversation_id`, `text`, `tool_calls`, `tool_responses`.
- **When to use**: Create and test interactive documents, React components, HTML pages, code with live preview.
- **Example**:
  ```python
  canvas_execute(
      "Create an interactive HTML page with a canvas element that draws "
      "a spiral animation using JavaScript."
  )
  ```
- **Notes**:
  - Internally prepends `"Use Canvas to: "` to the prompt before sending.
  - Uses `temporary=False` internally.
  - Similar to `code_interpreter` but uses the Canvas editing environment instead of the sandbox.
  - If `conv` is not injected, creates its own `ConversationClient` instance.

---

## Account Introspection

### account_status

- **Purpose**: Return ChatGPT account info: subscription plan, features, and entitlements.
- **Parameters**: None.
- **Returns**: `dict` with keys:
  - `email` (str) -- PII-redacted email
  - `country` (str) -- account country code
  - `groups` (list) -- account groups
  - `subscription` (str) -- subscription plan name
  - `has_active_subscription` (bool)
  - `expires_at` (str) -- subscription expiry timestamp
  - `features_count` (int) -- number of enabled features
- **When to use**: Check subscription status, verify account tier, troubleshoot access issues.
- **Example**:
  ```python
  status = account_status()
  # {'email': '<EMAIL>', 'country': 'US', 'subscription': 'pro',
  #  'has_active_subscription': True, 'expires_at': '2026-06-15T...', 'features_count': 42}
  ```
- **Notes**:
  - Async handler; offloads calls to `/backend-api/me` and `/backend-api/accounts/check/v4-2023-04-27` to the synchronous backend client.
  - Email is PII-redacted (regex pattern matching).

---

### list_models

- **Purpose**: Return all available ChatGPT models with full metadata.
- **Parameters**: None.
- **Returns**: `list[dict]` -- each dict contains:
  - `slug` (str) -- model identifier (e.g., `"gpt-5-3"`, `"o3-pro"`)
  - `title` (str) -- display name
  - `description` (str)
  - `max_tokens` (int)
  - `reasoning_type` (str) -- e.g., `"chain"`, `null`
  - `thinking_efforts` (list) -- available thinking effort levels
  - `tags` (list) -- model tags
  - `capabilities` (dict) -- feature flags
  - `enabled_tools` (list) -- tools available to this model
  - `product_features_keys` (list)
- **When to use**: Discover available models before calling `chat` with a specific slug. Check model capabilities.
- **Example**:
  ```python
  models = list_models()
  pro_models = [m for m in models if 'pro' in (m.get('slug') or '')]
  ```
- **Notes**:
  - Returns models for `history_and_training_disabled=false` variant. Some models may differ in capabilities between temporary/non-temporary modes.
  - Async handler; offloads the REST call to the synchronous backend client.

---

### list_voices

- **Purpose**: Return the Voice choices currently available to the signed-in ChatGPT account.
- **Parameters**:
  - `voice_mode` (str, optional) -- select a mode-specific catalog. Values accepted by the live account contract on 2026-07-11 are `standard`, `advanced`, and `wingman`. Omit for the account default. The value is not restricted to that list (modes change), but must be a short lowercase token or it is rejected before any request. GPT-Live audio is a separate session contract, not a currently accepted catalog mode.
- **Returns**: `list[dict]` -- each dict contains exactly:
  - `id` (str) -- the opaque backend Voice ID; preserved verbatim and not derived from the display name
  - `name` (str) -- display name, with common PII/secret patterns redacted
  - `description` (str) -- display description, with common PII/secret patterns redacted
  - `selected` (bool or None) -- `True`/`False` only when the response identifies a selected ID present in the returned catalog; otherwise `None`
  - `has_preview` (bool) -- whether the private response advertised preview media
- **When to use**: Discover account/rollout-specific Voice IDs and display metadata.
- **Example**:
  ```python
  voices = list_voices()                          # account default
  advanced_voices = list_voices(voice_mode="advanced")
  selected = next((voice for voice in voices if voice["selected"] is True), None)
  ```
- **Notes**:
  - Uses the private `GET /backend-api/settings/voices` website route (with `?voice_mode=` when a mode is given). Voice is an official ChatGPT product, but this adapter is not an official API and may drift.
  - The catalog is live and rollout-specific; names, IDs, ordering, selection, and count are not hard-coded.
  - Raw preview URLs, colors, gain values, unknown response fields, and account identifiers are not returned.
  - This tool does not fetch preview audio, start a Voice session, capture a microphone, synthesize speech, stream GPT-Live audio, or guarantee transcript extraction.
  - A malformed private response fails closed with `voice catalog contract changed` rather than pretending the catalog is empty.
  - Async handler; offloads the REST call to the synchronous backend client.

---

### list_conversations

- **Purpose**: Return recent ChatGPT conversations.
- **Parameters**:
  - `limit` (int, default: `20`) -- maximum number of conversations to return.
- **Returns**: `list[dict]` -- each dict contains:
  - `id` (str) -- conversation ID (pass to `get_conversation`)
  - `title` (str) -- PII-redacted title
  - `update_time` (float) -- Unix timestamp
  - `is_archived` (bool)
  - `gizmo_id` (str or None) -- non-null if this is a Custom GPT conversation
- **When to use**: Browse recent conversations, find a conversation by title, list Custom GPT conversations.
- **Example**:
  ```python
  convos = list_conversations(limit=5)
  # [{'id': 'abc-123', 'title': 'Discussion about <TOPIC>', 'update_time': 1716...}]
  ```
- **Notes**:
  - Titles are PII-redacted (emails, phone numbers replaced).
  - Sorted by `update_time` descending (most recent first).
  - Async handler; offloads the REST call to the synchronous backend client.

---

### get_conversation

- **Purpose**: Get full details of a ChatGPT conversation including all messages.
- **Parameters**:
  - `conversation_id` (str, required) -- the conversation ID from `list_conversations`.
  - `max_messages` (int, default: `100`) -- maximum number of messages to return.
- **Returns**: `dict` with keys:
  - `id` (str)
  - `title` (str) -- PII-redacted
  - `create_time` (float)
  - `update_time` (float)
  - `message_count` (int)
  - `messages` (list) -- each message contains:
    - `id`, `role` (`"user"`, `"assistant"`, `"tool"`), `recipient`, `content_type`, `status`, `create_time`
    - `text` (str, truncated to 2000 chars) -- for text/multimodal messages
    - `images` (list) -- for multimodal messages, each with `asset_pointer`, `width`, `height`
    - `code` (str, truncated to 500 chars) -- for code messages
- **When to use**: Inspect a conversation's full history, extract prior research, audit tool calls.
- **Example**:
  ```python
  convo = get_conversation("abc-123-def-456", max_messages=50)
  for msg in convo["messages"]:
      if msg["role"] == "assistant" and "text" in msg:
          print(msg["text"][:200])
  ```
- **Notes**:
  - Text parts are truncated to 2000 characters, code parts to 500 characters.
  - Only includes messages with role `user`, `assistant`, or `tool` (skips system messages).
  - Async handler; offloads the REST call to the synchronous backend client. Large conversations can still take time to transfer and parse.

---

### list_tasks

- **Purpose**: Return scheduled/completed ChatGPT tasks with full metadata.
- **Parameters**:
  - `limit` (int, default: `20`) -- maximum number of tasks to return.
- **Returns**: `list[dict]` -- each dict contains:
  - `task_id` (str)
  - `title` (str) -- PII-redacted
  - `status` (str) -- e.g., `"completed"`, `"scheduled"`, `"running"`
  - `created_at` (str)
  - `updated_at` (str)
  - `prompt` (str) -- PII-redacted original prompt
  - `conversation_id` (str)
  - `image_gen_message` (bool) -- whether the task involves image generation
  - `interruptions_disabled` (bool)
- **When to use**: Check scheduled task status, review completed tasks, manage automated workflows.
- **Example**:
  ```python
  tasks = list_tasks(limit=10)
  running = [t for t in tasks if t["status"] == "running"]
  ```
- **Notes**:
  - Titles and prompts are PII-redacted.
  - Async handler; offloads the REST call to the synchronous backend client.

---

### list_apps

- **Purpose**: Return ChatGPT connected apps and connectors.
- **Parameters**: None.
- **Returns**: `list[dict]` -- each dict contains:
  - `id` (str) -- app/connector ID
  - `type` (str) -- classified as `"official_connector"` (prefix `connector_`), `"third_party_sdk"` (prefix `asdk_app_`), or `"unknown"`
  - `enabled` (bool)
  - `connected` (bool)
- **When to use**: Check which connectors are active, troubleshoot DR connector availability.
- **Example**:
  ```python
  apps = list_apps()
  dr_connector = [a for a in apps if "deep_research" in a["id"]]
  ```
- **Notes**:
  - App names are not resolvable from the API -- only IDs are returned. The `type` field is inferred from the ID prefix.
  - Async handler; offloads the REST call to the synchronous backend client.

---

### list_custom_gpts

- **Purpose**: Return private Custom GPTs from the ChatGPT sidebar.
- **Parameters**: None.
- **Returns**: `list[dict]` -- each dict contains:
  - `name` (str) -- PII-redacted GPT name
  - `short_url` (str) -- the GPT's short URL identifier
- **When to use**: Discover available Custom GPTs before calling `gpt_chat`. Get GPT identifiers.
- **Example**:
  ```python
  gpts = list_custom_gpts()
  # [{'name': 'Data Analyst', 'short_url': 'g-abc123...'}]
  # Use short_url as gizmo_id with gpt_chat
  ```
- **Notes**:
  - Names are PII-redacted.
  - The `short_url` field is what you pass as `gizmo_id` to `gpt_chat`.
  - Uses the `/backend-api/gizmos/snorlax/sidebar` endpoint.
  - Async handler; offloads the REST call to the synchronous backend client.

---

## Memory & Instructions

### memory_list

- **Purpose**: Return all ChatGPT memories.
- **Parameters**: None.
- **Returns**: `list[dict]` -- each dict contains:
  - `id` (str) -- memory entry ID
  - `status` (str)
  - `content` (str) -- PII-redacted memory text
  - `created_timestamp` (str)
- **When to use**: Review what ChatGPT remembers, audit stored memories, verify memory creation.
- **Example**:
  ```python
  memories = memory_list()
  for m in memories:
      print(f"[{m['status']}] {m['content'][:100]}")
  ```
- **Notes**:
  - Content is PII-redacted (emails, phone numbers replaced with `<EMAIL>`, `<PHONE>`).
  - Async handler; offloads `GET /backend-api/memories` to the synchronous backend client.

---

### memory_search

- **Purpose**: Keyword search over ChatGPT memories.
- **Parameters**:
  - `query` (str, required) -- search keyword (case-insensitive substring match).
- **Returns**: `list[dict]` -- matching memories, each with `id`, `content` (redacted), `created_timestamp`.
- **When to use**: Find specific memories by keyword, verify a memory was stored.
- **Example**:
  ```python
  memory_search("hippocampus")
  # Returns memories containing "hippocampus" (case-insensitive)
  ```
- **Notes**:
  - Case-insensitive substring match (not regex, not fuzzy).
  - Fetches all memories then filters client-side. May be slow if you have many memories.
  - Content is PII-redacted.
  - Async handler; offloads the memory fetch to the synchronous backend client.

---

### memory_create_via_chat

- **Purpose**: Add an entry to ChatGPT memories via model-initiated write.
- **Parameters**:
  - `content` (str, required) -- the text to remember verbatim.
- **Returns**: `str` -- the assistant's reply (usually confirms what was stored).
- **When to use**: Persist a fact, preference, or context for future ChatGPT conversations.
- **Example**:
  ```python
  memory_create_via_chat(
      "User's lab uses Inscopix nVista miniscopes for calcium imaging. "
      "Primary mouse line is Thy1-GCaMP7f. Typical recording: 20 min, 20 fps."
  )
  ```
- **Notes**:
  - **Workaround**: `POST /backend-api/memories` returns 405 (Method Not Allowed). ChatGPT only allows model-initiated memory writes. This tool asks the model to remember the content directly.
  - Uses `temporary=False` (memory persistence requires non-temporary conversations).
  - The model may paraphrase or summarize rather than store verbatim. Use `memory_search` to verify.
  - Uses `gpt-5-3` by default (from config).

---

### custom_instructions_get

- **Purpose**: Read ChatGPT custom instructions.
- **Parameters**: None.
- **Returns**: `dict` with keys:
  - `enabled` (bool) -- whether custom instructions are active
  - `traits_enabled` (bool)
  - `personality_type` (str)
  - `about_user` (str) -- PII-redacted user instructions
  - `about_model` (str) -- PII-redacted model instructions
- **When to use**: Inspect current instructions before modifying, audit what ChatGPT knows about the user.
- **Example**:
  ```python
  instructions = custom_instructions_get()
  print(instructions["about_user"])
  print(instructions["about_model"])
  ```
- **Notes**:
  - Content is PII-redacted.
  - Async handler; offloads `GET /backend-api/user_system_messages` to the synchronous backend client.

---

### custom_instructions_set

- **Purpose**: Update ChatGPT custom instructions. Read-modify-write pattern preserves fields not supplied.
- **Parameters**:
  - `about_user` (str or None, default: `None`) -- new user instructions. If `None`, existing value is preserved.
  - `about_model` (str or None, default: `None`) -- new model instructions. If `None`, existing value is preserved.
- **Returns**: `dict` -- minimal acknowledgement: `{"updated": true, "fields": [...]}` listing only the fields updated.
- **When to use**: Update what ChatGPT knows about the user, change model behavior/persona.
- **Example**:
  ```python
  custom_instructions_set(
      about_user="Neuroscientist studying hippocampal circuits. "
                 "Prefers technical terminology. Lab uses Python + MATLAB.",
      about_model="Always cite DOIs when referencing papers. "
                  "Use SI units. Explain electrophysiology concepts at graduate level."
  )
  ```
- **Notes**:
  - **Read-modify-write**: reads current instructions first, then overwrites only the fields you supply. Unspecified fields are preserved.
  - At least one parameter must be supplied; pass either field alone to preserve the other.
  - Concurrent updates are serialized so partial writes cannot overwrite one another with stale preserved values.
  - If the current state cannot be read, the tool refuses to write rather than risk clearing an unspecified field.
  - The acknowledgement never echoes instruction content or the backend response.
  - Async handler; offloads both the GET and POST to the synchronous backend client.

---

## Codex

### list_codex_envs

- **Purpose**: Return Codex environments with their configuration.
- **Parameters**: None.
- **Returns**: `list[dict]` -- each dict contains:
  - `id` (str) -- environment ID (pass to `codex_task_create`)
  - `label` (str) -- human-readable label (use with `repo_label` parameter)
  - `workspace_dir` (str)
  - `agent_network_access` (bool) -- whether the agent has network access
  - `repo_count` (int) -- number of repos in this environment
- **When to use**: Discover available Codex environments before creating tasks. Get environment IDs.
- **Example**:
  ```python
  envs = list_codex_envs()
  # [{'id': 'env-abc', 'label': 'my-project', 'workspace_dir': '/workspace', ...}]
  ```
- **Notes**:
  - Async handler; offloads `GET /backend-api/codex/environments` to the synchronous backend client.
  - The `label` field is what you pass as `repo_label` to `codex_task_create`.

---

### list_codex_tasks

- **Purpose**: Return recent Codex tasks with status.
- **Parameters**:
  - `limit` (int, default: `10`) -- maximum number of tasks to return.
- **Returns**: `list[dict]` -- each dict contains:
  - `id` (str) -- task ID
  - `title` (str) -- PII-redacted task title
  - `status` (str) -- task status from `turn_status`
- **When to use**: Check Codex task status, review recent tasks, monitor running tasks.
- **Example**:
  ```python
  tasks = list_codex_tasks(limit=5)
  # [{'id': 'task-xyz', 'title': 'Fix <TOPIC> bug', 'status': 'completed'}]
  ```
- **Notes**:
  - Titles are PII-redacted.
  - Status comes from the `turn.turn_status` field in the response.
  - Async handler; offloads the REST call to the synchronous backend client.

---

### codex_task_create

- **Purpose**: Create a new Codex task in a specified environment.
- **Parameters**:
  - `repo_label` (str, required) -- the environment label (from `list_codex_envs`). Used to resolve `environment_id` if not supplied.
  - `prompt` (str, required) -- the task description / instruction.
  - `environment_id` (str or None, default: `None`) -- explicit environment ID. If `None`, resolved from `repo_label`.
  - `branch` (str, default: `"main"`) -- the git branch to work on.
- **Returns**: `dict` -- the server's response from `POST /backend-api/codex/tasks`.
- **When to use**: Dispatch a coding task to Codex (bug fix, feature implementation, refactoring).
- **Example**:
  ```python
  codex_task_create(
      repo_label="my-project",
      prompt="Add unit tests for the parse_config function in utils.py. "
             "Cover edge cases: missing keys, empty file, malformed TOML.",
      branch="feature/add-tests"
  )
  ```
- **Notes**:
  - If `environment_id` is not provided, the tool fetches all environments and matches by `repo_label`.
  - Raises `ValueError` if no environment matches the label, or if the label is ambiguous (matches multiple environments).
  - The payload shape is: `new_task={environment_id, branch}` + `input_items=[{type: "message", role: "user", content: [{content_type: "text", text: prompt}]}]`.
  - Async handler; offloads the REST calls to the synchronous backend client.

---

## GPT-Live Mode B export

Experimental, optional. Audio stays in a headed browser sidecar; MCP is
text/control only. Cloudflare Turnstile bypass is **out of scope**. Start with:

```bash
cd sidecar && node browser/sidecar.mjs --profile ./.chrome-gptlive --audio q.wav
```

Control base: `http://127.0.0.1:8741` (override with `GPT2AGENT_LIVE_CONTROL`).

### voice_live_export_help

- **Purpose**: Document how to export GPT-Live to an agent (Mode B) and the Turnstile boundary.
- **Parameters**: none.
- **Returns**: `str` — start steps, tool list, boundary notes.
- **When to use**: First call before using the other `voice_live_*` tools.

### voice_live_status

- **Purpose**: Status of the local export control plane (no audio/secrets).
- **Parameters**: none.
- **Returns**: `dict` — state, transcript counts, boundary flags (redacted).

### voice_live_get_transcript

- **Purpose**: Drain buffered human/agent transcript text from the export plane.
- **Parameters**:
  - `clear` (bool, default: `False`) — when `True`, clears the buffer after read.
- **Returns**: `dict` with `transcripts: [{role, text, at}, ...]`.

### voice_live_send_text

- **Purpose**: Make GPT-Live speak agent reply text (TTS via browser; no audio on MCP).
- **Parameters**:
  - `text` (str, required) — non-empty reply text (bounded length).
- **Returns**: `dict` — `{ok, delivered, ...}` without raw wire/audio payloads.

### voice_live_end

- **Purpose**: End the GPT-Live export session via the local control plane.
- **Parameters**: none.
- **Returns**: `dict` — `{ok, state}` or an unreachable-control error with a start hint.

---

## Common Patterns

### Checking model availability before chat
```python
models = list_models()
slugs = [m["slug"] for m in models]
if "o3-pro" in slugs:
    response = chat("Complex analysis task", model="o3-pro")
```

### Image generation + download workflow
```python
result = generate_image("Diagram of neural circuit")
for asset in result.get("assets", []):
    print(f"Download: {asset.get('download_url')}")
    # URL expires in ~1 hour -- save immediately
```

### Research with citation preservation
```python
# Light DR (30-120s, citations included)
report = deep_research("Recent advances in two-photon calcium imaging")

# Heavy DR (5-30min, long-form, citations may be missing)
report = deep_research_heavy("Comprehensive BCI technology review 2024-2026")
```

### Memory read-modify-write
```python
current = memory_list()
memory_create_via_chat("New fact to remember")
# Verify
results = memory_search("keyword from new fact")
```

---

## Key Gotchas

1. **`temporary=True` blocks features**: Image gen, code interpreter, canvas, and memory persistence all require `temporary=False`. The `chat` tool defaults to `True`; all other SSE-based tools force `False`.

2. **PII redaction**: `list_conversations`, `get_conversation`, `list_tasks`, `memory_list`, `memory_search`, `custom_instructions_get`, `list_custom_gpts`, and `list_codex_tasks` all redact emails and phone numbers from returned text.

3. **Sync vs async**: Registered REST handlers (account, memory, instructions, codex, conversations, apps, gpts) are async and offload the synchronous backend client. SSE-based tools (chat, agent, deep_research, deep_research_heavy, gpt_chat, generate_image, code_interpreter, canvas_execute) are async.

4. **Memory creation is model-initiated only**: `POST /backend-api/memories` returns 405. Use `memory_create_via_chat` which asks the model to store the memory through conversation.

5. **Heavy DR quota**: limits and reset timing are account-reported and can change. Check `deep-research/bin/quota.sh` before dispatching.

6. **Heavy DR citations**: Recovered from the connector's hidden widget state (`widget_state.report_message`) since 0.0.4. Grouped source URLs are usually present but not guaranteed; if absent, the model may have cited sources inline in the report body.

7. **Codex label resolution**: `codex_task_create` resolves `environment_id` from `repo_label` by fetching all environments. Raises `ValueError` on no match or ambiguous match.

8. **Download URL expiry**: File download URLs from `get_file_download_url` and `generate_image` expire after approximately 1 hour.
