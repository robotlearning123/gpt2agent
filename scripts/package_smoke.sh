#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 DIST_DIR PROJECT_VERSION DISTRIBUTION_VERSION" >&2
  exit 2
fi

DIST_DIR=$1
PROJECT_VERSION=$2
DIST_VERSION=$3
SCRIPT_ROOT=$(cd "$(dirname "$0")" && pwd)
CORPUS_SCRIPT="$SCRIPT_ROOT/verify_installed_adapter_corpus.py"
CORPUS_FIXTURE="$SCRIPT_ROOT/../tests/fixtures/installed_adapter_corpus.v1.json"

if [ ! -d "$DIST_DIR" ]; then
  echo "distribution directory does not exist: $DIST_DIR" >&2
  exit 2
fi

DIST_DIR=$(cd "$DIST_DIR" && pwd)
DIST_ENTRIES=()
while IFS= read -r -d '' entry; do
  DIST_ENTRIES+=("$entry")
done < <(find "$DIST_DIR" -mindepth 1 -maxdepth 1 -print0)
WHEELS=()
while IFS= read -r -d '' entry; do
  WHEELS+=("$entry")
done < <(find "$DIST_DIR" -maxdepth 1 -type f -name '*.whl' -print0)
SDISTS=()
while IFS= read -r -d '' entry; do
  SDISTS+=("$entry")
done < <(find "$DIST_DIR" -maxdepth 1 -type f -name '*.tar.gz' -print0)
if [ "${#DIST_ENTRIES[@]}" -ne 2 ] \
  || [ "${#WHEELS[@]}" -ne 1 ] \
  || [ "${#SDISTS[@]}" -ne 1 ]; then
  echo "expected exactly one wheel and one sdist and no other entries in $DIST_DIR" >&2
  exit 1
fi
WHEEL=${WHEELS[0]}
SDIST=${SDISTS[0]}
SOURCE_TOOL_NAMES_JSON=$(
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SCRIPT_ROOT/.." python - <<'PY'
import json

from gpt2agent.tool_manifest import TOOL_NAMES

print(json.dumps(list(TOOL_NAMES), separators=(",", ":")))
PY
)

hash_dist() {
  python - "$WHEEL" "$SDIST" <<'PY'
import hashlib
import pathlib
import sys

for value in sys.argv[1:]:
    path = pathlib.Path(value)
    print(path.name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
PY
}

DIST_HASHES_BEFORE=$(hash_dist)

TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

run_scrubbed() {
  local PRIVATE_HOME=$1
  shift
  mkdir -p "$PRIVATE_HOME" "$PRIVATE_HOME/tmp"
  chmod 700 "$PRIVATE_HOME" "$PRIVATE_HOME/tmp"
  env -i \
    HOME="$PRIVATE_HOME" \
    USERPROFILE="$PRIVATE_HOME" \
    TEMP="$PRIVATE_HOME/tmp" \
    TMP="$PRIVATE_HOME/tmp" \
    TMPDIR="$PRIVATE_HOME/tmp" \
    PATH="/usr/bin:/bin" \
    LANG="C.UTF-8" \
    LC_ALL="C.UTF-8" \
    PIP_CONFIG_FILE=/dev/null \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONUTF8=1 \
    "$@"
}

check_installed_package() {
  local VENV_ROOT=$1
  local CHECK_ROOT=$2
  local FORBIDDEN_SOURCE=$3
  local PRIVATE_HOME=$4

  mkdir -p "$CHECK_ROOT"
  (
    cd "$CHECK_ROOT"
    test "$(run_scrubbed "$PRIVATE_HOME" "$VENV_ROOT/bin/gpt2agent" --version)" = \
      "gpt2agent $PROJECT_VERSION"
    test "$(run_scrubbed "$PRIVATE_HOME" "$VENV_ROOT/bin/python" -m gpt2agent --version)" = \
      "gpt2agent $PROJECT_VERSION"
    run_scrubbed "$PRIVATE_HOME" "$VENV_ROOT/bin/python" - \
      "$DIST_VERSION" "$FORBIDDEN_SOURCE" "$SOURCE_TOOL_NAMES_JSON" <<'PY'
from importlib import import_module
from importlib.metadata import version
from importlib.resources import files
from importlib.util import find_spec
from pathlib import Path
import json
import re
import sys

expected_distribution = sys.argv[1]
source_tool_names = json.loads(sys.argv[3])
assert version("gpt2agent") == expected_distribution

module_file = import_module("gpt2agent").__file__
assert module_file is not None
module_path = Path(module_file).resolve()
if sys.argv[2]:
    forbidden_source = Path(sys.argv[2]).resolve()
    assert not module_path.is_relative_to(forbidden_source)

root = files("gpt2agent")
required_skills = [
    "skills/deep-research/SKILL.md",
    "skills/deep-research/bin/deep_research.py",
    "skills/deep-research/bin/quota.sh",
    "skills/deep-research/bin/run.sh",
    "skills/gpt2agent/SKILL.md",
    "skills/gpt2agent/tools-reference.md",
]
assert all(root.joinpath(path).is_file() for path in required_skills)

match = re.match(r"^(\d+)\.(\d+)\.(\d+)", expected_distribution)
assert match is not None
requires_account_manifest = tuple(map(int, match.groups())) >= (0, 0, 12)

# v0.0.12 adds these interfaces in a parallel implementation lane. When they
# are present in the source distribution, make sure both package formats carry
# the exact registry and readable resources.
manifest_spec = find_spec("gpt2agent.tool_manifest")
if requires_account_manifest:
    assert manifest_spec is not None
if manifest_spec is not None:
    from gpt2agent.tool_manifest import CHATGPT_TOOL_NAMES, GROK_TOOL_NAMES, TOOL_NAMES

    assert TOOL_NAMES
    assert len(set(TOOL_NAMES)) == len(TOOL_NAMES)
    assert CHATGPT_TOOL_NAMES
    assert GROK_TOOL_NAMES
    assert set(CHATGPT_TOOL_NAMES).isdisjoint(GROK_TOOL_NAMES)
    assert CHATGPT_TOOL_NAMES + GROK_TOOL_NAMES == TOOL_NAMES
    assert list(TOOL_NAMES) == source_tool_names

resources_dir = root.joinpath("resources")
if requires_account_manifest:
    assert resources_dir.is_dir()
if resources_dir.is_dir():
    for name in ("feature-coverage.v1.json", "update-evidence.v1.json"):
        payload = json.loads(resources_dir.joinpath(name).read_text(encoding="utf-8"))
        assert payload["schema_version"] == "1"
    coverage = json.loads(
        resources_dir.joinpath("feature-coverage.v1.json").read_text(encoding="utf-8")
    )
    assert coverage["tools"] == list(CHATGPT_TOOL_NAMES)
PY
  )
}

python -m venv "$TMP_ROOT/wheel-venv"
"$TMP_ROOT/wheel-venv/bin/python" -m pip install --upgrade pip
run_scrubbed "$TMP_ROOT/wheel-home" \
  "$TMP_ROOT/wheel-venv/bin/python" -m pip install "$WHEEL"
run_scrubbed "$TMP_ROOT/wheel-home" "$TMP_ROOT/wheel-venv/bin/python" -m pip check
check_installed_package \
  "$TMP_ROOT/wheel-venv" "$TMP_ROOT/wheel-check" "" "$TMP_ROOT/wheel-home"
WHEEL_CORPUS="$TMP_ROOT/wheel-corpus.json"
run_scrubbed "$TMP_ROOT/wheel-home" \
  "$TMP_ROOT/wheel-venv/bin/python" "$CORPUS_SCRIPT" \
  --fixture "$CORPUS_FIXTURE" --output "$WHEEL_CORPUS"

mkdir -p "$TMP_ROOT/home"
run_scrubbed "$TMP_ROOT/home" \
  "$TMP_ROOT/wheel-venv/bin/gpt2agent" install --client claude-code
test -f "$TMP_ROOT/home/.claude/skills/deep-research/SKILL.md"
if find "$TMP_ROOT/home/.claude/skills" -type f \( -name '*.pyc' -o -name '*.pyo' \) \
  -print -quit | grep -q .; then
  echo "installed skills contain generated bytecode" >&2
  exit 1
fi

mkdir -p "$TMP_ROOT/sdist"
tar -xzf "$SDIST" -C "$TMP_ROOT/sdist"
SDIST_ROOTS=()
while IFS= read -r -d '' entry; do
  SDIST_ROOTS+=("$entry")
done < <(find "$TMP_ROOT/sdist" -mindepth 1 -maxdepth 1 -type d -print0)
if [ "${#SDIST_ROOTS[@]}" -ne 1 ]; then
  echo "expected the sdist to contain exactly one top-level directory" >&2
  exit 1
fi
SDIST_ROOT=${SDIST_ROOTS[0]}
test -f "$SDIST_ROOT/tests/fixtures/heavy_dr_conversation_detail_h2.json"
test -f "$SDIST_ROOT/tests/fixtures/heavy_dr_widget_state.json"

python -m venv "$TMP_ROOT/sdist-venv"
"$TMP_ROOT/sdist-venv/bin/python" -m pip install --upgrade pip
run_scrubbed "$TMP_ROOT/sdist-home" \
  "$TMP_ROOT/sdist-venv/bin/python" -m pip install "$SDIST" pytest pytest-asyncio
run_scrubbed "$TMP_ROOT/sdist-home" "$TMP_ROOT/sdist-venv/bin/python" -m pip check
check_installed_package "$TMP_ROOT/sdist-venv" "$TMP_ROOT/sdist-check" "$SDIST_ROOT" \
  "$TMP_ROOT/sdist-home"
SDIST_CORPUS="$TMP_ROOT/sdist-corpus.json"
run_scrubbed "$TMP_ROOT/sdist-home" \
  "$TMP_ROOT/sdist-venv/bin/python" "$CORPUS_SCRIPT" \
  --fixture "$CORPUS_FIXTURE" --output "$SDIST_CORPUS"
cmp -s "$WHEEL_CORPUS" "$SDIST_CORPUS"
(
  cd "$SDIST_ROOT"
  run_scrubbed "$TMP_ROOT/sdist-home" \
    env SKIP_LIVE=1 "$TMP_ROOT/sdist-venv/bin/python" -m pytest -q \
    tests/test_heavy_dr_parser.py
)

DIST_HASHES_AFTER=$(hash_dist)
test "$DIST_HASHES_BEFORE" = "$DIST_HASHES_AFTER"
