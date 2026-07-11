# gpt2agent docs

User-facing documentation. (Project/contributor internals live in
[CONTRIBUTING.md](../CONTRIBUTING.md); security policy in [SECURITY.md](../SECURITY.md).)

- **[Quickstart](./quickstart.md)** — prereqs → one-line install → verify → first call.
- **[Client setup](./clients.md)** — per-host config (Claude Code, Codex, Cursor,
  Windsurf, Claude Desktop, Zed, VS Code, Cline, generic stdio).
- **[Configuration](./configuration.md)** — config file paths, `[server]`/`[models]`
  keys, and env vars (`CODEX_HOME`, `GPT2AGENT_ALLOW_REMOTE`, `GPT2AGENT_RAW_DUMP`).
- **[Troubleshooting](./troubleshooting.md)** — token/401/403/429, tools not
  appearing, temporary-chat feature blocks, pipx/PEP-668.
- **[FAQ](./faq.md)** — official? ban risk? stdio vs HTTP? Plus vs Pro? quota?
- **[How it works](./how-it-works.md)** — the no-proxy architecture.
- **[Roadmap](./roadmap.md)** — version lanes, GPT-Live boundary, language policy,
  and release gates.

For the full per-tool reference (every argument, return shape, and gotcha for all
26 tools), see [`gpt2agent/skills/gpt2agent/tools-reference.md`](../gpt2agent/skills/gpt2agent/tools-reference.md).

Security model and ToS/account-ban risk are covered in the main
[README](../README.md#security--risk--read-before-you-run-this).
