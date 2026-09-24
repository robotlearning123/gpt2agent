#!/usr/bin/env bash
# Canonical verify command for this repo (honored by the pre-commit test gate).
# uv's default sync skips optional-dependencies, so the dev extra (pytest, ruff)
# must be requested explicitly — plain `uv run python -m pytest` fails with
# "No module named pytest" (measured 2026-09-23). Lint scope matches CI.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --extra dev python -m pytest -x --maxfail=3 --tb=line -q --no-header "$@"
uv run --extra dev python -m ruff check gpt2agent tests scripts
