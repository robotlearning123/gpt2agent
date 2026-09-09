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
- **Network transport is disabled in 0.0.12.** Loopback TCP cannot isolate a
  full account from other users and processes on the same machine. The legacy
  HTTP flag and URL installer fail closed; use stdio. Any future network
  transport must authenticate every request with a per-launch secret or an
  equivalent per-user boundary.
- **Run the account transport in its dedicated MCP process.** Account sessions
  ignore ambient proxy and CA-bundle variables, use the directly declared
  `certifi` trust bundle, and fail before account I/O when `SSLKEYLOGFILE` is
  present. Libcurl can retain a TLS key-log file that trusted embedded code
  opened and then removed from the environment; that prior in-process state is
  not detectable. Do not embed gpt2agent after unrelated curl traffic or load
  untrusted code in its process.
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
  on a trusted local machine. The verifier owns the bearer and direct bounded
  `curl_cffi` transport; downloaded wheel/sdist candidates remain inert during
  that live gate. Candidate imports and adapter execution happen only in the
  credential-free main-CI package job against a closed synthetic corpus, or in
  an OS-isolated no-auth environment. Cookies, bearer tokens, raw responses,
  unsanitized receipts, and the sanitized receipt itself must never be uploaded
  to hosted CI or a public release. Hosted automation receives only its SHA-256
  commitment. Safe publication also requires independent live tag-creation and
  protected-environment approval controls, with self-review and administrator
  bypass disabled; without them, do not tag or publish.
- **Grok Build is a separate official CLI boundary.** It uses the CLI's own
  subscription/OAuth login and never falls back to ChatGPT authentication.
  gpt2agent removes `XAI_API_KEY` and `GROK_CODE_XAI_API_KEY` from the child
  environment, fails closed while `[grok_build].roots` is empty, constrains the
  working directory to configured roots, and bounds execution time and output.
  Plan mode is read-only; apply mode is an explicit destructive choice. The CLI
  retains its own session history, while gpt2agent returns only a sanitized
  session ID and does not copy transcripts.

In-scope reports we want to hear about: token/secret leakage in logs or errors,
ways to expose a network transport or cross the stdio process boundary, injection that makes a
tool act on attacker-controlled data, unsafe file/permission handling, or a
capability/resource adapter that returns account content outside its allowlist.

## Supported versions

Only the latest released version is supported. Please reproduce against `main`
or the newest release before reporting.
