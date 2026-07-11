#!/usr/bin/env bash
set -euo pipefail

umask 077

usage() {
  echo "usage: $0 --python /absolute/path/to/python3.12 --python-sha256 REVIEWED_SHA256 --venv /absolute/new/venv" >&2
  exit 2
}

die() {
  echo "account-gate bootstrap: $*" >&2
  exit 1
}

PYTHON=
PYTHON_SHA256=
VENV=
while (( $# > 0 )); do
  case "$1" in
    --python)
      (( $# >= 2 )) || usage
      PYTHON=$2
      shift 2
      ;;
    --python-sha256)
      (( $# >= 2 )) || usage
      PYTHON_SHA256=$2
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
[[ -n "$PYTHON" && -n "$PYTHON_SHA256" && -n "$VENV" ]] || usage

SCRIPT_ROOT=$(cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_ROOT/.." && pwd -P)
LOCK="$REPO_ROOT/requirements-account-gate.txt"
VERIFIER="$SCRIPT_ROOT/verify_account_receipt.py"
CURRENT_UID=$(id -u)

require_secure_ancestor_chain() {
  local target=$1
  local target_kind=$2
  local current=$target
  local first=1
  local mode uid parent

  while :; do
    [[ ! -L "$current" ]] \
      || die "trusted runtime ancestor path must not contain symlinks"
    if (( first == 1 )); then
      if [[ "$target_kind" == file ]]; then
        [[ -f "$current" ]] \
          || die "trusted runtime target must be a regular file"
      else
        [[ -d "$current" ]] \
          || die "trusted runtime target must be a directory"
      fi
      first=0
    else
      [[ -d "$current" ]] \
        || die "trusted runtime ancestor path must contain only directories"
    fi
    uid=$(stat -c '%u' -- "$current")
    mode=$(stat -c '%a' -- "$current")
    [[ "$uid" == 0 || "$uid" == "$CURRENT_UID" ]] \
      || die "trusted runtime ancestor path must be owned by root or the current user"
    (( (8#$mode & 0022) == 0 )) \
      || die "trusted runtime ancestor path must not be group- or world-writable; install or copy CPython into an owner-private base"
    [[ "$current" != / ]] || break
    parent=$(dirname -- "$current")
    [[ "$parent" != "$current" ]] \
      || die "trusted runtime ancestor path is invalid"
    current=$parent
  done
}

[[ "$PYTHON" == /* ]] || die "--python must be an absolute path"
[[ "$VENV" == /* && "$VENV" != / ]] || die "--venv must be an absolute non-root path"
[[ -f "$PYTHON" && -x "$PYTHON" && ! -L "$PYTHON" ]] \
  || die "--python must name a regular, executable, non-symlink file"
[[ "$PYTHON" == "$(realpath -e -- "$PYTHON")" ]] \
  || die "--python must use its canonical path"
[[ "$PYTHON_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || die "--python-sha256 must be one reviewed lowercase SHA-256 checksum"
[[ ! -e "$VENV" && ! -L "$VENV" ]] || die "--venv must not already exist"
[[ -f "$LOCK" && ! -L "$LOCK" ]] || die "the account-gate lock file is unavailable"
[[ -f "$VERIFIER" && ! -L "$VERIFIER" ]] || die "the account-gate verifier is unavailable"

PYTHON_UID=$(stat -Lc '%u' -- "$PYTHON")
PYTHON_MODE=$(stat -Lc '%a' -- "$PYTHON")
[[ "$PYTHON_UID" == 0 || "$PYTHON_UID" == "$CURRENT_UID" ]] \
  || die "--python must be owned by root or the current user"
(( (8#$PYTHON_MODE & 0022) == 0 )) \
  || die "--python must not be group- or world-writable; install or copy CPython into an owner-private base"
require_secure_ancestor_chain "$PYTHON" file
ACTUAL_PYTHON_SHA256=$(sha256sum -- "$PYTHON")
ACTUAL_PYTHON_SHA256=${ACTUAL_PYTHON_SHA256%% *}
[[ "$ACTUAL_PYTHON_SHA256" == "$PYTHON_SHA256" ]] \
  || die "--python does not match the reviewed executable checksum"

VENV_PARENT=$(dirname -- "$VENV")
[[ -d "$VENV_PARENT" && ! -L "$VENV_PARENT" ]] \
  || die "the --venv parent must be an existing regular directory"
[[ "$VENV_PARENT" == "$(realpath -e -- "$VENV_PARENT")" ]] \
  || die "the --venv parent must use its canonical path without symlinks"
[[ "$(stat -Lc '%u' -- "$VENV_PARENT")" == "$CURRENT_UID" ]] \
  || die "the --venv parent must be owned by the current user"
[[ "$(stat -Lc '%a' -- "$VENV_PARENT")" == 700 ]] \
  || die "the --venv parent must have mode 0700"
require_secure_ancestor_chain "$VENV_PARENT" directory

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

PYTHON_BASE=$(run_scrubbed "$PYTHON" -I -S -B -c '
import os
import sys

identity = (
    sys.implementation.name,
    sys.version_info[:3],
    sys.platform,
    os.uname().machine,
)
if identity != ("cpython", (3, 12, 13), "linux", "x86_64"):
    raise SystemExit("unexpected interpreter target")
base = sys.base_prefix
if not isinstance(base, str) or not base or "\n" in base or "\r" in base:
    raise SystemExit("invalid interpreter base")
print(base, end="")
') || die "--python must be CPython 3.12.13 for Linux x86_64"

[[ "$PYTHON_BASE" == /* && "$PYTHON_BASE" != / ]] \
  || die "--python reported an invalid base installation"
[[ -d "$PYTHON_BASE" && ! -L "$PYTHON_BASE" ]] \
  || die "--python base must be a regular directory"
[[ "$PYTHON_BASE" == "$(realpath -e -- "$PYTHON_BASE")" ]] \
  || die "--python base must use its canonical path"
case "$PYTHON" in
  "$PYTHON_BASE"/*) ;;
  *) die "--python must be contained by its base installation" ;;
esac

PYTHON_BASE_UID=$(stat -Lc '%u' -- "$PYTHON_BASE")
[[ "$PYTHON_BASE_UID" == 0 || "$PYTHON_BASE_UID" == "$CURRENT_UID" ]] \
  || die "--python base must be owned by root or the current user"
[[ "$PYTHON_UID" == "$PYTHON_BASE_UID" ]] \
  || die "--python and its base installation must have the same owner"

require_secure_installation_chain() {
  local root=$1
  local target=$2
  local owner=$3
  local current=$target
  local first=1
  local mode uid parent

  while :; do
    [[ ! -L "$current" ]] \
      || die "--python base/tool path chain must not contain symlinks"
    if (( first == 1 )); then
      [[ -f "$current" ]] \
        || die "--python tool path must remain a regular file"
      first=0
    else
      [[ -d "$current" ]] \
        || die "--python base/tool path chain must contain only directories"
    fi
    uid=$(stat -Lc '%u' -- "$current")
    mode=$(stat -Lc '%a' -- "$current")
    [[ "$uid" == "$owner" ]] \
      || die "--python base/tool path chain ownership is inconsistent"
    (( (8#$mode & 0022) == 0 )) \
      || die "--python base/tool path chain must not be group- or world-writable; install or copy CPython into an owner-private base"
    [[ "$current" != "$root" ]] || break
    parent=$(dirname -- "$current")
    [[ "$parent" != "$current" && ( "$parent" == "$root" || "$parent" == "$root"/* ) ]] \
      || die "--python escaped its base installation"
    current=$parent
  done
}

require_secure_installation_chain "$PYTHON_BASE" "$PYTHON" "$PYTHON_BASE_UID"
ACTUAL_PYTHON_SHA256=$(sha256sum -- "$PYTHON")
ACTUAL_PYTHON_SHA256=${ACTUAL_PYTHON_SHA256%% *}
[[ "$ACTUAL_PYTHON_SHA256" == "$PYTHON_SHA256" ]] \
  || die "--python changed after its reviewed checksum was verified"

run_scrubbed "$PYTHON" -I -S -B - "$LOCK_SNAPSHOT" <<'PY'
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
run_scrubbed "$PYTHON" -I -S -B -m venv --copies "$VENV"
chmod 700 -- "$VENV"
VENV_PYTHON="$VENV/bin/python"
SITE_PACKAGES="$VENV/lib/python3.12/site-packages"
[[ -f "$VENV_PYTHON" && -x "$VENV_PYTHON" && ! -L "$VENV_PYTHON" ]] \
  || die "the virtual environment did not create a copied Python executable"
[[ -d "$SITE_PACKAGES" && ! -L "$SITE_PACKAGES" ]] \
  || die "the virtual environment has an unexpected site-packages layout"
require_secure_ancestor_chain "$VENV" directory
require_secure_ancestor_chain "$VENV_PYTHON" file
require_secure_ancestor_chain "$SITE_PACKAGES" directory
VENV_PYTHON_SHA256=$(sha256sum -- "$VENV_PYTHON")
VENV_PYTHON_SHA256=${VENV_PYTHON_SHA256%% *}
[[ "$VENV_PYTHON_SHA256" == "$PYTHON_SHA256" ]] \
  || die "the copied virtual-environment interpreter changed provenance"

run_scrubbed "$VENV_PYTHON" -I -B -m pip --isolated --disable-pip-version-check install \
  --index-url https://pypi.org/simple \
  --require-hashes \
  --only-binary=:all: \
  --no-deps \
  --no-compile \
  --no-cache-dir \
  --no-input \
  --requirement "$LOCK_SNAPSHOT" 1>&2
run_scrubbed "$VENV_PYTHON" -I -B -m pip --isolated --disable-pip-version-check check 1>&2
run_scrubbed "$VENV_PYTHON" -I -S -B "$VERIFIER" check-runtime \
  --trusted-site-packages "$SITE_PACKAGES" 1>&2

printf '%s\n' "$SITE_PACKAGES"
