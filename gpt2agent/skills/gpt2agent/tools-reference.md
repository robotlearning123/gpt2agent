# gpt2agent MCP Tools Reference

Complete parameter reference for all 32 MCP tools and 2 static resources exposed by the gpt2agent server.
Source: `gpt2agent/server.py` and `gpt2agent/tools/*.py`.

Every tool declares all four standard MCP annotations: read-only, destructive,
idempotent, and open-world hints. They help clients present and approve calls;
they are not an authorization boundary.

---

## Table of Contents

- [Chat & Reasoning (5)](#chat--reasoning)
- [Image & File (3)](#image--file)
- [Code Execution (2)](#code-execution)
- [Account Discovery (14)](#account-discovery)
- [Memory & Instructions (5)](#memory--instructions)
- [Codex (3)](#codex)
- [Static Resources (2)](#static-resources)

---

## Chat & Reasoning

### chat

- **Purpose**: Send a single prompt to any ChatGPT model and get a text response.
- **Parameters**:
  - `prompt` (str, required) -- the user message to send.
  - `model` (str, default: value from `config.toml` `[models].chat`, fallback `"gpt-5-3"`) -- model slug. Run `list_models` to see all available slugs.
  - `temporary` (bool, default: `True`) -- when `True`, sets `history_and_training_disabled=True` which prevents the conversation from being saved and **blocks tool-based features** (image gen, code interpreter, canvas, memory persistence). Set `False` to enable those features.
  - `thinking_effort` (str or None, default: `None`) -- optional effort advertised by the selected general model's live `thinking_efforts`. Omitted from the payload when unset.
- **Returns**: `str` -- the assistant's reply text followed by one
  server-appended final `Tool activity receipt`. It names fixed safe categories
  or `none`; only this final footer is authoritative. Private dispatch and
  response payloads are withheld.
- **When to use**: General Q&A, text generation, translation, summarization. Default choice for single-turn queries.
- **Example**:
  ```python
  chat("Explain the difference between LTP and LTD in hippocampal neurons.")
  chat("Summarize this abstract:", model="o3")
  chat("Prove or disprove this conjecture", model="o3-pro", thinking_effort="high")
  chat("Generate a chart of this data", model="gpt-5-3", temporary=False)
  ```
- **Notes**:
  - `temporary=True` (default) means the conversation is ephemeral -- not saved to ChatGPT history, cannot use image gen / code interpreter / canvas.
  - If you need tool-based features (image gen, code interpreter, canvas), you **must** pass `temporary=False`.
  - The selected slug must exist in the live general catalog. A Work-only slug is rejected unless the exact same slug independently appears in `list_models`.
  - Model metadata is cached for 60 seconds per auth generation. A rejected effort forces one refresh before returning `invalid_input`; a non-general or non-configurable model returns `unsupported`.

---

### agent

- **Purpose**: ChatGPT Agent Mode -- autonomous browsing, code execution, and tool use with 262K context window.
- **Parameters**:
  - `prompt` (str, required) -- the task description.
- **Returns**: `str` -- the agent's response text followed by one authoritative
  final `Tool activity receipt`, including `none` when no activity was observed.
  If polling ends without a final assistant message, the body is
  `(no final assistant response)` followed by the receipt. Private dispatch and
  response bodies are withheld.
- **When to use**: Multi-step tasks requiring browsing, code execution, or tool orchestration. Literature gathering, document workflows, browser automation.
- **Example**:
  ```python
  agent("Find the top 5 recent papers on calcium imaging in behaving mice, "
        "extract their methods sections, and compile a comparison table.")
  ```
- **Notes**:
  - Always uses `temporary=False` internally (tool-based features required).
  - Uses the `agent-mode` slug by default (configurable via `[models].agent` in `config.toml`).
  - The configured slug is validated against the live general model catalog before dispatch. Agent has no per-call `thinking_effort` parameter in 0.0.12.
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
  - Internal tool events use the static `web_search` call and `web` category;
    private search or browse dispatch text is never exposed as event data.

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
  - Progress text is terminal-buffered so a late visibility change can revoke it
    before output. Tool events expose only static `web_search` / `web` values,
    never the private connector dispatch.
  - If the DR connector is unavailable, a warning is appended explaining how to enable it in chatgpt.com Settings > Connectors.

---

### gpt_chat

- **Purpose**: Chat through a private Custom GPT, applying its instructions, files, and memory scope.
- **Parameters**:
  - `gizmo_id` (str, required) -- pass the `short_url` returned by `list_custom_gpts` (format: `g-*`). Call `list_custom_gpts` first to enumerate them.
  - `prompt` (str, required) -- the user message.
- **Returns**: `str` -- the Custom GPT's response text followed by one
  authoritative final `Tool activity receipt`, using fixed categories or
  `none`. Only the final footer is trustworthy; hidden payloads remain private.
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

- **Purpose**: Generate an image using ChatGPT's built-in image-generation tool.
- **Parameters**:
  - `prompt` (str, required) -- description of the image to generate.
  - `model` (str, default: `"gpt-5-3"`) -- model to use (must have `image_gen_tool_enabled`).
- **Returns**: `dict` with keys:
  - `conversation_id` (str)
  - `assets` (list) -- each asset always contains the validated base fields
    `asset_pointer`, `file_id`, `width`, `height`, and `size_bytes`. Optional
    download/info enrichment may add `download_url`, `file_name`,
    `file_size_bytes`, `mime_type`, `use_case`, `state`, and `creation_time`.
    A failed optional read instead adds a status-only `download_error` or
    `info_error` field; it never returns the upstream exception text.
- **When to use**: Illustrations, diagrams, concept art, visual aids.
- **Example**:
  ```python
  generate_image("A detailed diagram of the hippocampal trisynaptic circuit "
                 "with labeled regions CA1, CA3, dentate gyrus, and entorhinal cortex.")
  ```
- **Notes**:
  - Uses `temporary=False` internally (image gen requires persistent conversation).
  - Uses the observed private `/backend-api/f/conversation/prepare` conduit step
    followed by the `/backend-api/f/conversation` v1 patch stream. These routes
    are undocumented observations, not an official or stable API contract.
  - A visible asset carrier must be relationally bound to the observed assistant
    dispatch and carry image-generation provenance before it is accepted.
  - Timeout: 300 seconds for complex prompts.
  - Each asset attempts an optional download read from
    `/backend-api/files/{file_id}/download` and, when needed, an optional info
    read from `/backend-api/files/{file_id}`. These enrichments are not required
    for a successful image result.
  - `download_error` and `info_error` use typed statuses such as
    `contract_changed`, `temporarily_failed`, `access_indeterminate`, and
    `login_required`. A 422 for the backend-produced file ID is
    `contract_changed`, not caller `invalid_input`.
  - Download URLs are temporary (~1 hour expiry). Use `get_file_download_url` to refresh.
  - If `conv` is not injected, the tool creates its own `ConversationClient` instance.

---

### get_file_info

- **Purpose**: Get metadata for any ChatGPT file (images, uploads, etc.).
- **Parameters**:
  - `file_id` (str, required) -- the file ID (e.g., `file_00000000c02471f88295cda5f3b8c66b`).
- **Returns**: `dict` with exactly: `id`, `name`, `size`, `file_size_bytes`, `use_case`, `state`, `creation_time`, and `mime_type`.
- **When to use**: Inspect a file's metadata before downloading, check file state, get file name.
- **Example**:
  ```python
  get_file_info("file_00000000c02471f88295cda5f3b8c66b")
  ```
- **Notes**:
  - Async handler; offloads `GET /backend-api/files/{file_id}` to the synchronous backend client.
  - Unknown backend fields and private URLs are never returned. A malformed or
    blank 2xx response fails as `contract_changed`.

---

### get_file_download_url

- **Purpose**: Get a temporary download URL for a ChatGPT file.
- **Parameters**:
  - `file_id` (str, required) -- the file ID.
- **Returns**: `str` -- an absolute public HTTPS download URL, with a valid
  signed query preserved byte-for-byte. Empty string if not found; unsafe,
  internal, malformed, or non-HTTPS destinations fail as changed contracts.
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

## Account Discovery

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

### account_capabilities

- **Purpose**: Return live, shape-only capability truth without exposing account content or raw private responses.
- **Parameters**: None.
- **Returns**: `dict` with:
  - `schema_version` (`"1"`)
  - `observed_at` (UTC ISO-8601 timestamp)
  - `capabilities` (list), where every record contains `id`, `surface`,
    `entitled`, `reachable_now`, `reachability_scope`, `exposed_by_mcp`,
    `officially_supported`, `evidence_source`, `observed_at`, `status`, `reason`,
    and `item_contract_status`.
- **When to use**: Decide what the current account can prove now, distinguish an
  unavailable feature from a temporarily failed or changed private adapter, and
  audit rollout drift without fetching account content.
- **Example**:
  ```python
  truth = account_capabilities()
  available = [
      item["id"] for item in truth["capabilities"]
      if item["entitled"] is True and item["reachable_now"] is True
  ]
  ```
- **Notes**:
  - `entitled` and `reachable_now` are independent tri-state values: `True`,
    `False`, or `None` when the release cannot prove an answer safely.
  - `status` uses typed outcomes such as `ok`, `unavailable`, `unsupported`,
    `contract_changed`, `temporarily_failed`, `access_indeterminate`,
    `login_required`, and `unverified`.
  - Collection item evidence is `live_verified`, `public_bundle_only`,
    `unverified_live`, or `not_applicable`.
  - The tool shares one auth snapshot and a 90-second total budget across an
    explicit GET-only route table. It never probes Voice/realtime, transcripts,
    conversation bodies, writes, unknown routes, or off-host redirects.
  - Conversation summaries, memories, and custom instructions stay
    `unverified` because probing their routes would read private account
    content. Use their explicit tools only when the caller wants that content.
  - `officially_supported=False` describes this project's private adapter path;
    an underlying ChatGPT product may still be officially documented.

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
  - General metadata shares a 60-second cache keyed by auth generation with
    `chat` validation, but never merges the separate Work namespace.
  - Async handler; offloads the REST call to the synchronous backend client.

---

### list_work_models

- **Purpose**: Return account-visible Work model metadata without treating Work-only identifiers as general chat models.
- **Parameters**: None.
- **Returns**: `dict` with `items`; every item contains `surface: "work"`, `slug`,
  `title`, `max_tokens`, `reasoning_type`, `configurable_thinking_effort`,
  `default_thinking_effort`, and bounded `thinking_efforts` entries.
- **When to use**: Discover the Work product catalog or compare Work reasoning metadata separately from `list_models`.
- **Example**:
  ```python
  work = list_work_models()
  for model in work["items"]:
      print(model["slug"], model["default_thinking_effort"])
  ```
- **Notes**:
  - Uses `/backend-api/tpp/models/` and a Work-only 60-second cache keyed by auth generation.
  - A returned slug is opaque and Work-only unless the exact slug also appears independently in `list_models`.
  - Do not pass a Work-only slug to `chat` or configure it for `agent`.

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

- **Purpose**: Get a bounded, redacted projection of a ChatGPT conversation's visible messages.
- **Parameters**:
  - `conversation_id` (str, required) -- the conversation ID from `list_conversations`.
  - `max_messages` (int, default: `100`) -- maximum number of messages to return, from 1 through 100.
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
  - IDs, recipients, status, timestamps, and image dimensions are validated as
    bounded scalars; unknown nested backend fields are never returned.
  - Only includes messages with role `user`, `assistant`, or `tool` (skips system messages).
  - Async handler; offloads the REST call to the synchronous backend client. Large conversations can still take time to transfer and parse.

---

### list_tasks

- **Purpose**: Return generic background/asynchronous ChatGPT jobs with bounded metadata. This is not the scheduled-automation surface.
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
- **When to use**: Check generic background-job status or review completed asynchronous jobs. Use `list_scheduled_tasks` for scheduled automations.
- **Example**:
  ```python
  tasks = list_tasks(limit=10)
  running = [t for t in tasks if t["status"] == "running"]
  ```
- **Notes**:
  - Titles and prompts are PII-redacted.
  - `limit` accepts integers from 1 through 100. Booleans and other types are rejected.
  - Reads `/backend-api/tasks`; it does not claim to enumerate `/backend-api/automations`.
  - Async handler; offloads the REST call to the synchronous backend client.

---

### list_scheduled_tasks

- **Purpose**: Return one page of scheduled ChatGPT automations from the dedicated automation surface.
- **Parameters**:
  - `cursor` (str or None, default: `None`) -- opaque continuation cursor from a previous page.
- **Returns**: `dict` with:
  - `items` -- records containing `id`, `updated_at`, `next_run_times`,
    `is_enabled`, and `target_time_utc`.
  - `cursor` -- the next opaque cursor or `None`.
- **When to use**: Inspect scheduled automation state and next-run metadata without confusing it with generic jobs.
- **Example**:
  ```python
  page = list_scheduled_tasks()
  if page["cursor"]:
      next_page = list_scheduled_tasks(cursor=page["cursor"])
  ```
- **Notes**:
  - Sends the fixed `filter=scheduled` query to `/backend-api/automations`.
  - Returns one page only. It does not create, mutate, enable, or disable an automation.
  - IDs and cursors are bounded; malformed items fail with `contract_changed` rather than leaking a raw response.

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
  - Accepts the observed string and object entry variants while preserving order and duplicates.
  - Apps/connectors are not Plugins. Use `list_plugins` and `list_installed_plugins` for Plugin state.
  - Async handler; offloads the REST call to the synchronous backend client.

---

### list_plugins

- **Purpose**: Return one bounded page of the account Plugin catalog.
- **Parameters**:
  - `scope` (str, default: `"USER"`) -- `USER` or `WORKSPACE`.
  - `limit` (int, default: `50`) -- page size from 1 through 50.
  - `cursor` (str or None, default: `None`) -- opaque backend or gpt2agent local cursor.
- **Returns**: `dict` with `items` and `cursor`. Items contain only bounded
  allowlisted scalar/list fields such as IDs, redacted names, scope/status,
  `release_version`, skill/app/connector IDs, MCP server keys, and capability names.
- **When to use**: Browse Plugins available to the account or workspace without inspecting connected-App state.
- **Example**:
  ```python
  page = list_plugins(scope="USER", limit=20)
  while page["cursor"]:
      page = list_plugins(scope="USER", limit=20, cursor=page["cursor"])
  ```
- **Notes**:
  - Supports both the observed root-array catalog and the current
    `plugins`/`pagination.next_page_token` envelope.
  - Root-array pagination uses a process-local keyed fingerprint in its local
    cursor and fails closed if the ordered Plugin identities change between pages.
  - Unknown nested data is not returned. Malformed known envelopes are `contract_changed`.

---

### list_installed_plugins

- **Purpose**: Return installed Plugins without Plugin bodies; configured
  secret/PII shapes in bounded allowlisted fields are redacted.
- **Parameters**: None.
- **Returns**: `dict` with `items`, normalized to the same allowlist used by `list_plugins`.
- **When to use**: Determine which Plugins and bounded skill/app identifiers are installed.
- **Example**:
  ```python
  installed = list_installed_plugins()["items"]
  enabled = [item for item in installed if item["enabled"] is True]
  ```
- **Notes**:
  - Accepts the observed root `plugins` list and nested
    `plugins.results`/`plugins.page` variants.
  - A nested page that reports `has_more=True` fails with `contract_changed`;
    the observed installed-plugin contract has no supported continuation request.
  - Marketplace, Apps, and Skills objects are never returned; only bounded scalar IDs/names may be derived.

---

### sites_access

- **Purpose**: Return non-identifying Sites access state for the account.
- **Parameters**: None.
- **Returns**: `dict` with nullable booleans `enabled`,
  `custom_domains_enabled`, and `requires_workspace_slug`.
- **When to use**: Check whether Sites appears available before listing bounded Site metadata.
- **Example**:
  ```python
  access = sites_access()
  if access["enabled"] is True:
      sites = list_sites(limit=20)
  ```
- **Notes**:
  - Does not return a workspace slug, identity, URL, or page content.
  - A missing boolean remains `None`; it is not guessed as false.

---

### list_sites

- **Purpose**: Return one bounded Sites page without content or private URLs.
- **Parameters**:
  - `limit` (int, default: `20`) -- page size from 1 through 100.
  - `cursor` (str or None, default: `None`) -- opaque continuation cursor.
- **Returns**: `dict` with `items` and `cursor`. Items contain bounded `id`,
  redacted `title`/`slug`, `status`, `updated_at`, redacted `disabled_by`, sharing
  mode/counts, and booleans indicating whether live, preview, or screenshot URLs exist.
- **When to use**: Inventory Sites and sharing counts without retrieving content or signed/private URLs.
- **Example**:
  ```python
  page = list_sites(limit=20)
  visible = [site for site in page["items"] if site["has_live_url"]]
  ```
- **Notes**:
  - URL values are reduced to presence booleans; their strings are never returned.
  - The tool is read-only and does not publish, mutate, share, or create a Site.

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
- **Returns**: `dict` with exactly `id` and `status`; the opaque server response is not exposed.
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
  - Returns `invalid_input` if no environment matches the label, or if the label is ambiguous (matches multiple environments).
  - The payload shape is: `new_task={environment_id, branch}` + `input_items=[{type: "message", role: "user", content: [{content_type: "text", text: prompt}]}]`.
  - Async handler; offloads the REST calls to the synchronous backend client.

---

## Static Resources

Resource reads are deterministic package reads. They perform no account or
network request and contain no cookies, bearer tokens, account identity, prompts,
responses, or private URLs.

### chatgpt://feature-coverage

- **MIME type**: `application/json`
- **Purpose**: Versioned 0.0.12 contract snapshot containing the exact 32-tool
  manifest plus bounded feature status. Voice catalog/transcript/GPT-Live are
  deferred and Projects is unsupported; those records do not claim live reachability.
- **Use instead of**: Hard-coding a tool count or inferring that every ChatGPT
  product is exposed by this adapter.

### chatgpt://update-evidence

- **MIME type**: `application/json`
- **Purpose**: Public-surface evidence snapshot with source URLs and check time.
  `account_contract_status` and `private_adapter_status` remain `not_checked`
  because a public no-secret radar cannot verify a private account adapter.
- **Use instead of**: Treating a documentation or web-bundle check as live
  account proof.

For current account state, call `account_capabilities`; do not reinterpret a
static resource as a live receipt.

---

## Common Patterns

### Checking model availability before chat
```python
models = list_models()
slugs = [m["slug"] for m in models]
if "o3-pro" in slugs:
    selected = next(m for m in models if m["slug"] == "o3-pro")
    efforts = [e["thinking_effort"] for e in selected.get("thinking_efforts", [])]
    response = chat(
        "Complex analysis task",
        model="o3-pro",
        thinking_effort="high" if "high" in efforts else None,
    )
```

### Separating static coverage from live truth
```python
# Read chatgpt://feature-coverage through MCP resource discovery for the
# deterministic release contract. Then call the tool for current account state:
truth = account_capabilities()
by_id = {item["id"]: item for item in truth["capabilities"]}
if by_id["sites"]["entitled"] is True:
    page = list_sites(limit=20)
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

2. **Redaction is bounded, not anonymization**: conversation, job, memory,
instruction, Custom GPT, Codex, Plugin, and Site text fields redact configured
PII/secret shapes. Names, addresses, identifiers, and other sensitive content
may remain.

3. **Sync vs async**: Registered REST handlers are async and offload the
synchronous backend client. SSE-based tools (`chat`, `agent`, both Deep Research
tools, `gpt_chat`, image generation, code interpreter, and canvas) are async.

4. **Memory creation is model-initiated only**: `POST /backend-api/memories` returns 405. Use `memory_create_via_chat` which asks the model to store the memory through conversation.

5. **Heavy DR quota**: limits and reset timing are account-reported and can change. Check `deep-research/bin/quota.sh` before dispatching.

6. **Heavy DR citations**: Recovered from the connector's hidden widget state (`widget_state.report_message`) since 0.0.4. Grouped source URLs are usually present but not guaranteed; if absent, the model may have cited sources inline in the report body.

7. **Codex label resolution**: `codex_task_create` resolves `environment_id` from `repo_label` by fetching all environments. Returns `invalid_input` on no match or ambiguous match.

8. **Download URL expiry**: File download URLs from `get_file_download_url` and `generate_image` expire after approximately 1 hour.

9. **Generic jobs are not scheduled automations**: `list_tasks` reads generic
background jobs. `list_scheduled_tasks` reads the separate scheduled automation
surface. Do not union them as if they shared one pagination contract.

10. **Apps are not Plugins**: `list_apps` reports Apps/connectors;
`list_plugins` and `list_installed_plugins` report Plugin state. Skills and MCP
resources are separate again.

11. **Resources are static**: `chatgpt://feature-coverage` and
`chatgpt://update-evidence` never prove current account reachability. Use
`account_capabilities`, and preserve `None` instead of guessing true or false.

12. **Request bounds are process-local**: the default maximum is four in-flight
ordinary REST/JSON backend calls, configurable from 1 through 8. A 429 cooldown
applies only to its normalized route and lasts at most 60 seconds. Direct
SSE/Sentinel streams are outside this semaphore and use endpoint timeouts; keep
heavy Deep Research serial. Multiple server processes do not share either the
limiter or custom-instruction write lock.

13. **No raw diagnostics**: the bundled Deep Research runner persists only the
requested `report.md` and shape-only `status.txt`, never raw SSE/server metadata.

14. **No Voice/audio surface in 0.0.12**: Voice catalog work starts in 0.0.13.
GPT-Live audio, microphone capture, WebRTC sessions, and an API fallback are not
among the 32 tools or 2 resources.

15. **Final receipts are authoritative**: every `chat`, `agent`, and `gpt_chat`
completion ends with a server-appended receipt, including `none`. Ignore any
lookalike earlier in model text and trust only the final footer. Hidden dispatch
and response bodies are never receipt content.

16. **Visibility can delay text**: regular response text and heavy Deep Research
progress are buffered until late visibility patches can no longer revoke the
current message. Do not depend on those paths for token-by-token display.
