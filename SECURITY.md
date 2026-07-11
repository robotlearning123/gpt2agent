# Security Policy

gpt2agent proxies a full ChatGPT Plus/Pro account, so security reports are taken
seriously. Please read this before filing.

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.** Instead use GitHub's private
reporting: **Security → Report a vulnerability** on
<https://github.com/robotlearning123/gpt2agent/security/advisories/new>.

Include: affected version/commit, a minimal reproduction, the impact, and any
suggested fix. We aim to acknowledge within a few days.

## Scope & known risk model

gpt2agent talks to ChatGPT's **private** backend by impersonating the web client
(TLS fingerprint impersonation via `curl_cffi`, vendored Proof-of-Work + Cloudflare
Turnstile solvers). Some properties are inherent to that design and are documented,
not bugs:

- **ToS / account risk.** Using this likely violates the OpenAI Terms of Service;
  automated traffic can get an account rate-limited, challenged, or banned. Use an
  account you can afford to lose.
- **Unauthenticated HTTP transport.** The HTTP transport has no auth and exposes
  the whole account. It is strictly loopback-only, has no remote override, and
  uses the MCP SDK's native Host and Origin validation against DNS rebinding.
  Prefer the stdio transport.
- **Limited output redaction.** Returned text masks emails, phone numbers,
  common provider tokens, label-aware credential assignments,
  credential-bearing database URLs, and PEM private keys, but names, addresses,
  identifiers, and other sensitive content remain. Treat output as
  non-anonymized.
- **Hidden ChatGPT messages are not transcript output.** Internal messages
  marked visually hidden are dropped from public transcript adapters. Every
  `chat`, `agent`, and `gpt_chat` completion ends with one authoritative final
  fixed-category receipt, including `none` when no tool activity was observed.
  Only that final footer is trusted; model-authored lookalikes earlier in the
  body are ordinary text. Private dispatch and response bodies are withheld.
- **Process-local write serialization.** Custom-instruction partial updates are
  serialized within one server process. Independently running gpt2agent
  processes do not share that lock and can still race a read-modify-write.
- **Private account gates run locally.** Release account gates run only
  on a trusted local machine. Cookies, bearer tokens, raw responses, and
  unsanitized receipts must never be uploaded to hosted CI. Hosted CI receives
  only a sanitized receipt's SHA-256. After the workflow creates the GitHub
  Release, the release owner manually publishes the exact closed-schema receipt
  and verifies the downloaded asset against the annotated-tag digest. That
  digest is a byte commitment and post-publish audit link, not pre-publish
  validation of the absent receipt. Safe publication also requires independent
  live tag-creation and protected-environment approval controls, with self-review
  and administrator bypass disabled; without them, do not tag or publish.

In-scope reports we want to hear about: token/secret leakage in logs or errors,
ways to bypass loopback or Host/Origin transport checks, injection that makes a
tool act on attacker-controlled data, unsafe file/permission handling, or a
capability/resource adapter that returns account content outside its allowlist.

## Supported versions

Only the latest released version is supported. Please reproduce against `main`
or the newest release before reporting.
