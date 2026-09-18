#!/usr/bin/env bash
# fleet-sync.sh — update every place the fleet actually runs gpt2agent from.
#
# WHY: the fleet does NOT run the dev worktree. MCP servers resolve the
# `gpt2agent` binary, which here is ~/.local/bin/gpt2agent →
# ~/.local/share/gpt2agent-venv (editable install → the clone at
# /home/robot/workspace/47-chatgpt2agent/gpt2agent). Shipping code to main
# without syncing this clone leaves the fleet on the OLD version — this is
# exactly how the 2026-09-18 "both accounts Turnstile-blocked" incident
# happened (fleet was on ~v0.0.14 while the fix lived only in a worktree).
#
# Usage: scripts/fleet-sync.sh [ref]        (default: origin/main)
set -euo pipefail

CLONE=/home/robot/workspace/47-chatgpt2agent/gpt2agent
REF="${1:-origin/main}"

echo "── fleet clone sync ──"
git -C "$CLONE" fetch origin -q
if [ -n "$(git -C "$CLONE" status --porcelain)" ]; then
  echo "WARN: $CLONE has uncommitted changes — not touching it:"
  git -C "$CLONE" status --short | head
  exit 1
fi
git -C "$CLONE" checkout -q --detach "$REF"
NEWVER=$(python3 -c "import tomllib;print(tomllib.load(open('$CLONE/pyproject.toml','rb'))['project']['version'])")
echo "clone now at $(git -C "$CLONE" rev-parse --short HEAD)  version $NEWVER"

echo
echo "── importable copy sanity ──"
VENV=/home/robot/.local/share/gpt2agent-venv
"$VENV/bin/python3" -c "import gpt2agent; print('venv resolves to', gpt2agent.__file__)"

echo
echo "── running MCP servers still on old code (restart these) ──"
pgrep -af "gpt2agent run" || echo "(none running)"

echo
echo "── miniconda copy (secondary install) ──"
MC=/home/robot/miniconda3/bin/python3
[ -x "$MC" ] && "$MC" -c "import gpt2agent,importlib.metadata as m;print('miniconda:', m.version('gpt2agent'), gpt2agent.__file__)" || true
