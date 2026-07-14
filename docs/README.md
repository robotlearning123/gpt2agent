# gpt2agent docs

User-facing documentation. (Project/contributor internals live in
[CONTRIBUTING.md](../CONTRIBUTING.md); security policy in [SECURITY.md](../SECURITY.md).)

- **[Quickstart](./quickstart.md)** — prereqs → one-line install → verify → first call.
- **[Client setup](./clients.md)** — per-host config (Claude Code, Codex, Cursor,
  Windsurf, Claude Desktop, Zed, VS Code, Cline, generic stdio).
- **[Configuration](./configuration.md)** — config file paths, ChatGPT models,
  fail-closed Grok Build roots, local transports, and bounded execution.
- **[Migrating to 0.0.12](./migration-0.0.12.md)** — new account discovery,
  removed legacy escape hatches, Deep Research artifacts, Voice boundary, and
  the exact-commit release receipt.
- **[Troubleshooting](./troubleshooting.md)** — token/401/403/429, tools not
  appearing, temporary-chat feature blocks, pipx/PEP-668.
- **[FAQ](./faq.md)** — official? ban risk? stdio vs HTTP? Plus vs Pro? quota?
- **[How it works](./how-it-works.md)** — the no-proxy architecture.

For the full per-tool and resource reference (every argument, return shape, and
gotcha for all registered tools and both resources), see
[`gpt2agent/skills/gpt2agent/tools-reference.md`](../gpt2agent/skills/gpt2agent/tools-reference.md).

Security model and ToS/account-ban risk are covered in the main
[README](../README.md#security--risk--read-before-you-run-this).
