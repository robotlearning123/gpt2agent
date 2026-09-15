#!/usr/bin/env bash
# Simulated agent-user journey gate: build the artifact, install the wheel
# into a clean venv (NO playwright extra — J13 depends on its absence),
# then drive the installed server over stdio like a real agent user.
# Read-only/offline only: no quota spend, no writes, no browser.
set -uo pipefail
W="${1:?usage: agent-user-journey.sh <worktree>}"
OUT="$W/artifacts/verify/agent-journey-$(date +%Y%m%d)"
mkdir -p "$OUT"
cd -P "$W" || exit 2

rm -rf dist && python -m build > "$OUT/build.log" 2>&1
WHEEL=$(find dist -maxdepth 1 -name '*.whl' | head -1)
[ -n "$WHEEL" ] || { echo "FAIL  build (no wheel)"; exit 1; }

VENV=/tmp/gpt2agent-journey-venv
rm -rf "$VENV" && python -m venv "$VENV" > /dev/null 2>&1
"$VENV/bin/pip" install --quiet "$WHEEL" > "$OUT/pip.log" 2>&1 || { echo "FAIL  venv install"; exit 1; }

python scripts/agent-user-journey.py "$VENV/bin/gpt2agent" 2> "$OUT/journey.err" | tee "$OUT/journey.log"
RC=${PIPESTATUS[0]}
rm -rf "$VENV"
exit "$RC"
