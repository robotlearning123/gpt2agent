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
  the whole account. It binds `127.0.0.1` by default and refuses non-loopback
  binds unless `GPT2AGENT_ALLOW_REMOTE=1` is set. Prefer the stdio transport.
- **Limited PII redaction.** Returned conversation/memory text strips only emails
  and phone numbers; treat output as non-anonymized.

In-scope reports we want to hear about: token/secret leakage in logs or errors,
ways to bypass the loopback/`GPT2AGENT_ALLOW_REMOTE` bind guard, injection that
makes a tool act on attacker-controlled data, or unsafe file/permission handling.

## Supported versions

Only the latest released version is supported. Please reproduce against `main`
or the newest release before reporting.
