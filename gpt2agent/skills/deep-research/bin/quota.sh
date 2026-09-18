#!/usr/bin/env bash
# Print remaining ChatGPT Deep Research quota for the selected account.
#
# NOTE: `limits_progress[].feature_name == "deep_research"` is the GENERIC
# counter — it gates the light `deep_research` (model=research) path only.
# `deep_research_heavy` (connector_openai_deep_research) has an independent
# monthly cap reported under a different feature name; it is NOT reflected
# here (verified live 2026-09-19: heavy dispatch succeeded with
# deep_research remaining=0).
set -euo pipefail

if command -v gpt2agent >/dev/null 2>&1; then
  PYTHON="$(head -1 "$(command -v gpt2agent)" | sed 's|^#!||' | awk '{print $1}')"
fi
PYTHON="${PYTHON:-$HOME/.local/share/pipx/venvs/gpt2agent/bin/python}"

if [ ! -x "$PYTHON" ]; then
  echo "error: cannot find a Python with gpt2agent installed" >&2
  echo "fix:   pipx install gpt2agent" >&2
  exit 1
fi

exec "$PYTHON" - <<'PY'
import sys

from gpt2agent.backend import BackendClient


def main():
    try:
        b = BackendClient()
        data = b.post(
            "/backend-api/conversation/init",
            json={"conversation_mode_kind": "primary_assistant"},
        )
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print("deep_research quota response is malformed", file=sys.stderr)
        return 1
    limits = data.get("limits_progress") or []
    if not isinstance(limits, list):
        print("deep_research quota response is malformed", file=sys.stderr)
        return 1
    for lim in limits:
        if not isinstance(lim, dict) or lim.get("feature_name") != "deep_research":
            continue
        remaining = lim.get("remaining")
        try:
            if isinstance(remaining, bool):
                raise ValueError
            remaining = int(remaining)
        except (TypeError, ValueError):
            print(
                "deep_research quota remaining is missing or invalid",
                file=sys.stderr,
            )
            return 1
        print(
            f"deep_research (light) remaining = {remaining}  "
            f"reset = {lim.get('reset_after')}"
        )
        print(
            "note: heavy DR (deep_research_heavy) has a separate monthly "
            "quota not shown by this counter"
        )
        return 0

    print("deep_research quota entry not found", file=sys.stderr)
    return 1


raise SystemExit(main())
PY
