#!/usr/bin/env bash
set -euo pipefail

umask 077

usage() {
  echo "usage: $0 --python /absolute/path/to/python3.12 --venv /absolute/new/venv" >&2
  exit 2
}

die() {
  echo "account-gate bootstrap: $*" >&2
  exit 1
}

PYTHON=
VENV=
while (( $# > 0 )); do
  case "$1" in
    --python)
      (( $# >= 2 )) || usage
      PYTHON=$2
      shift 2
      ;;
    --venv)
      (( $# >= 2 )) || usage
      VENV=$2
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done
[[ -n "$PYTHON" && -n "$VENV" ]] || usage

SCRIPT_ROOT=$(cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_ROOT/.." && pwd -P)
LOCK="$REPO_ROOT/requirements-account-gate.txt"
VERIFIER="$SCRIPT_ROOT/verify_account_receipt.py"
CURRENT_UID=$(id -u)

[[ "$PYTHON" == /* ]] || die "--python must be an absolute path"
[[ "$VENV" == /* && "$VENV" != / ]] || die "--venv must be an absolute non-root path"
[[ -f "$PYTHON" && -x "$PYTHON" && ! -L "$PYTHON" ]] \
  || die "--python must name a regular, executable, non-symlink file"
[[ "$PYTHON" == "$(realpath -e -- "$PYTHON")" ]] \
  || die "--python must use its canonical path"
[[ ! -e "$VENV" && ! -L "$VENV" ]] || die "--venv must not already exist"
[[ -f "$LOCK" && ! -L "$LOCK" ]] || die "the account-gate lock file is unavailable"
[[ -f "$VERIFIER" && ! -L "$VERIFIER" ]] || die "the account-gate verifier is unavailable"

PYTHON_UID=$(stat -Lc '%u' -- "$PYTHON")
PYTHON_MODE=$(stat -Lc '%a' -- "$PYTHON")
PYTHON_DIR=$(dirname -- "$PYTHON")
[[ "$PYTHON_UID" == 0 || "$PYTHON_UID" == "$CURRENT_UID" ]] \
  || die "--python must be owned by root or the current user"
(( (8#$PYTHON_MODE & 0022) == 0 )) || die "--python must not be group- or world-writable"
if [[ "$PYTHON_UID" == "$CURRENT_UID" ]]; then
  [[ "$(stat -Lc '%u' -- "$PYTHON_DIR")" == "$CURRENT_UID" \
    && "$(stat -Lc '%a' -- "$PYTHON_DIR")" == 700 ]] \
    || die "a user-owned --python must be inside an owner-private 0700 directory"
else
  PYTHON_DIR_MODE=$(stat -Lc '%a' -- "$PYTHON_DIR")
  [[ "$(stat -Lc '%u' -- "$PYTHON_DIR")" == 0 ]] \
    || die "a root-owned --python must be inside a root-owned directory"
  (( (8#$PYTHON_DIR_MODE & 0022) == 0 )) \
    || die "the root-owned --python directory must not be group- or world-writable"
fi

VENV_PARENT=$(dirname -- "$VENV")
[[ -d "$VENV_PARENT" && ! -L "$VENV_PARENT" ]] \
  || die "the --venv parent must be an existing regular directory"
[[ "$VENV_PARENT" == "$(realpath -e -- "$VENV_PARENT")" ]] \
  || die "the --venv parent must use its canonical path without symlinks"
[[ "$(stat -Lc '%u' -- "$VENV_PARENT")" == "$CURRENT_UID" ]] \
  || die "the --venv parent must be owned by the current user"
[[ "$(stat -Lc '%a' -- "$VENV_PARENT")" == 700 ]] \
  || die "the --venv parent must have mode 0700"

SCRATCH=$(mktemp -d -- "$VENV_PARENT/.gpt2agent-account-bootstrap.XXXXXXXX")
PRIVATE_HOME="$SCRATCH/home"
PRIVATE_TMP="$SCRATCH/tmp"
LOCK_SNAPSHOT="$SCRATCH/requirements-account-gate.txt"
VENV_CREATED=0

cleanup() {
  local status=$?
  trap - EXIT
  rm -rf -- "$SCRATCH"
  if (( status != 0 && VENV_CREATED == 1 )); then
    rm -rf -- "$VENV"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

mkdir -m 700 -- "$PRIVATE_HOME" "$PRIVATE_TMP"
cp -- "$LOCK" "$LOCK_SNAPSHOT"
chmod 600 -- "$LOCK_SNAPSHOT"

run_scrubbed() {
  env -i \
    HOME="$PRIVATE_HOME" \
    USERPROFILE="$PRIVATE_HOME" \
    XDG_CACHE_HOME="$PRIVATE_HOME/.cache" \
    XDG_CONFIG_HOME="$PRIVATE_HOME/.config" \
    TEMP="$PRIVATE_TMP" \
    TMP="$PRIVATE_TMP" \
    TMPDIR="$PRIVATE_TMP" \
    PATH=/usr/bin:/bin \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_CONFIG_FILE=/dev/null \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONUTF8=1 \
    "$@"
}

PYTHON_VERSION=$(run_scrubbed "$PYTHON" -I -S -c \
  'import sys; print(sys.implementation.name, f"{sys.version_info.major}.{sys.version_info.minor}")')
[[ "$PYTHON_VERSION" == "cpython 3.12" ]] || die "--python must be CPython 3.12"

run_scrubbed "$PYTHON" -I -S - "$LOCK_SNAPSHOT" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path


EXPECTED = {
    "certifi": "2026.6.17",
    "cffi": "2.1.0",
    "curl-cffi": "0.15.0",
    "markdown-it-py": "4.2.0",
    "mdurl": "0.1.2",
    "pip": "26.1.2",
    "pycparser": "3.0",
    "pygments": "2.20.0",
    "rich": "15.0.0",
}
REQUIRED_HEADERS = (
    "# Resolver: uv 0.9.18",
    "# Index: https://pypi.org/simple",
    "# Target: CPython 3.12.13 on x86_64-unknown-linux-gnu",
)
UNSAFE = re.compile(
    r"(?:^|\s)(?:--(?:index-url|extra-index-url|find-links|trusted-host)|-e|--editable)"
    r"(?:\s|=)|(?:@|://|git\+|\.\.?/)"
)
PINNED_HASHED = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)"
    r"(?:\s+--hash=sha256:[0-9a-f]{64})+$"
)


def fail(message: str) -> None:
    raise SystemExit(f"invalid account-gate lock: {message}")


path = Path(sys.argv[1])
if path.stat().st_size > 1024 * 1024:
    fail("file is too large")
text = path.read_text(encoding="utf-8")
if any(header not in text.splitlines() for header in REQUIRED_HEADERS):
    fail("provenance header is missing or changed")

logical: list[str] = []
current = ""
for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    current = f"{current} {line}".strip()
    if current.endswith("\\"):
        current = current[:-1].rstrip()
        continue
    logical.append(current)
    current = ""
if current:
    fail("unterminated continuation")

parsed: dict[str, str] = {}
for requirement in logical:
    if UNSAFE.search(requirement):
        fail("alternate indexes, URLs, VCS, editable, and local sources are forbidden")
    match = PINNED_HASHED.fullmatch(requirement)
    if match is None:
        fail("every requirement must be exactly pinned and SHA-256 hashed")
    name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    if name in parsed:
        fail(f"duplicate project {name!r}")
    parsed[name] = match.group(2)
if parsed != EXPECTED:
    fail("project closure or versions differ from the reviewed nine-project lock")
PY

VENV_CREATED=1
run_scrubbed "$PYTHON" -I -S -m venv --copies "$VENV"
chmod 700 -- "$VENV"
VENV_PYTHON="$VENV/bin/python"
SITE_PACKAGES="$VENV/lib/python3.12/site-packages"
[[ -f "$VENV_PYTHON" && -x "$VENV_PYTHON" && ! -L "$VENV_PYTHON" ]] \
  || die "the virtual environment did not create a copied Python executable"
[[ -d "$SITE_PACKAGES" && ! -L "$SITE_PACKAGES" ]] \
  || die "the virtual environment has an unexpected site-packages layout"

run_scrubbed "$VENV_PYTHON" -I -m pip --isolated --disable-pip-version-check install \
  --index-url https://pypi.org/simple \
  --require-hashes \
  --only-binary=:all: \
  --no-deps \
  --no-cache-dir \
  --no-input \
  --requirement "$LOCK_SNAPSHOT" 1>&2
run_scrubbed "$VENV_PYTHON" -I -m pip --isolated --disable-pip-version-check check 1>&2
run_scrubbed "$VENV_PYTHON" -I -S -B "$VERIFIER" check-runtime \
  --trusted-site-packages "$SITE_PACKAGES" 1>&2

printf '%s\n' "$SITE_PACKAGES"
