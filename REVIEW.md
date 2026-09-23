# Code review rules (gpt2agent)

Repo-specific always-check rules for `/my-review`, `/code-review`, `/simplify`,
and any independent reviewer. Every line here traces to a confirmed incident.

## Always check

- **A dependency pin is a claim about the environment.** Code must not assume the
  installed version of a pinned dependency: an out-of-range install has to fail
  loudly or be adapted to — never crash at startup, never be silently ignored.
  *(2026-09-23: `mcp` 2.2.0 left in a venv outside the `mcp>=1.27,<2` pin made
  `gpt2agent run` die at startup for every stdio MCP client, for days.)*
- **Optional kwargs of third-party constructors are version-dependent.** Gate
  `host`/`port`-style arguments on the installed signature instead of passing
  them unconditionally; if the SDK moved them elsewhere, refuse loudly rather
  than silently serving a default.
- **"Works with X and Y" requires an executed check per variant.** Import-level
  compatibility is not behaviour compatibility — the same incident had a working
  import shim and a crashing server path.
- **Test the call site, not only the helper.** A fully tested helper does not
  stop a regression at its caller.
- **Hand-written docstrings can claim mechanisms that do not exist** ("the skill
  pre-approves all N MCP tools" claimed an allowlist no code implements).
  Verify the mechanism, then the wording.

## Verification bar

- Blocking findings must be reproduced by execution on a scratch copy before
  being confirmed; prose-only reviews are drafts.
- Live-network tests flake on this host (DNS): rerun once before treating a
  failure as real, and record which run the verdict came from.
- Power-test every negative claim: show the check can produce a positive
  (disabled-vs-enabled, or a known-positive input).

## Skip lists

- Lockfiles, `dist/`, `build/`, `gpt2agent/_vendored/`, the generated
  `QA_REPORT.html`, and anything CI already gates (`ci.yml`, `release.yml`).

## Nits

- Cap nit-level findings at five per review; wording nits in docs are optional.
- `docs/dev/specs/**` is a historical design record — do not request rewrites there.
