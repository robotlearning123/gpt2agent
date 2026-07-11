#!/usr/bin/bash -p
set +o posix
unset POSIXLY_CORRECT
set -euo pipefail

PATH=/usr/bin:/bin
export PATH
readonly PATH
LANG=C
LC_ALL=C
export LANG LC_ALL
readonly LANG LC_ALL
unset BASH_ENV ENV CDPATH GLOBIGNORE TAR_OPTIONS GZIP BZIP2 XZ_OPT POSIXLY_CORRECT
umask 077

readonly RELEASE_TAG=20260623
readonly ASSET_NAME=cpython-3.12.13+20260623-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
readonly EXPECTED_ARCHIVE_SIZE=34159178
readonly EXPECTED_ARCHIVE_SHA256=10a452caac7041357805f0c19a60576df53f1ab06d1abfc9200f1f0157cb3bd1
readonly EXPECTED_PYTHON_SHA256=9544d2a29138833e6177d45dbc57468d37710b5080c901fbb579d53f251cdd6f

usage() {
  echo "usage: $0 --archive /absolute/path/$ASSET_NAME --destination /absolute/new/runtime" >&2
  exit 2
}

die() {
  echo "account-gate runtime installer: $*" >&2
  exit 1
}

contains_ascii_control() {
  local value=$1
  [[ "$value" =~ [[:cntrl:]] ]]
}

[[ "$(/usr/bin/uname -s)" == Linux ]] \
  || die "the reviewed $RELEASE_TAG runtime is supported only on Linux"

ARCHIVE=
DESTINATION=
while (( $# > 0 )); do
  case "$1" in
    --archive)
      (( $# >= 2 )) || usage
      ARCHIVE=$2
      shift 2
      ;;
    --destination)
      (( $# >= 2 )) || usage
      DESTINATION=$2
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done
[[ -n "$ARCHIVE" && -n "$DESTINATION" ]] || usage

CURRENT_UID=$(/usr/bin/id -u)

require_secure_ancestor_chain() {
  local target=$1
  local target_kind=$2
  local current=$target
  local first=1
  local mode uid parent

  while :; do
    [[ ! -L "$current" ]] \
      || die "secure runtime ancestry must not contain symlinks"
    if (( first == 1 )); then
      if [[ "$target_kind" == file ]]; then
        [[ -f "$current" ]] || die "secure runtime target must be a regular file"
      else
        [[ -d "$current" ]] || die "secure runtime target must be a directory"
      fi
      first=0
    else
      [[ -d "$current" ]] \
        || die "secure runtime ancestry must contain only directories"
    fi
    uid=$(/usr/bin/stat -c '%u' -- "$current")
    mode=$(/usr/bin/stat -c '%a' -- "$current")
    [[ "$uid" == 0 || "$uid" == "$CURRENT_UID" ]] \
      || die "secure runtime ancestry must be owned by root or the current user"
    (( (8#$mode & 0022) == 0 )) \
      || die "secure runtime ancestry must not be group- or world-writable"
    [[ "$current" != / ]] || break
    parent=$(/usr/bin/dirname -- "$current")
    [[ "$parent" != "$current" ]] || die "secure runtime ancestry is invalid"
    current=$parent
  done
}

[[ "$ARCHIVE" == /* ]] \
  || die "--archive must be an absolute path without ASCII control characters"
! contains_ascii_control "$ARCHIVE" \
  || die "--archive must be an absolute path without ASCII control characters"
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] \
  || die "--archive must name a regular, non-symlink file"
[[ "$ARCHIVE" == "$(/usr/bin/realpath -e -- "$ARCHIVE")" ]] \
  || die "--archive must use its canonical path"

[[ "$DESTINATION" == /* && "$DESTINATION" != / && "$DESTINATION" != */ ]] \
  || die "--destination must be an absolute, non-root path without ASCII control characters"
! contains_ascii_control "$DESTINATION" \
  || die "--destination must be an absolute, non-root path without ASCII control characters"
[[ ! -e "$DESTINATION" && ! -L "$DESTINATION" ]] \
  || die "--destination must not already exist"
DESTINATION_PARENT=$(/usr/bin/dirname -- "$DESTINATION")
DESTINATION_NAME=${DESTINATION##*/}
[[ -n "$DESTINATION_NAME" && "$DESTINATION_NAME" != . && "$DESTINATION_NAME" != .. ]] \
  || die "--destination has an invalid final component"
[[ -d "$DESTINATION_PARENT" && ! -L "$DESTINATION_PARENT" ]] \
  || die "the --destination parent must be an existing regular directory"
[[ "$DESTINATION_PARENT" == "$(/usr/bin/realpath -e -- "$DESTINATION_PARENT")" ]] \
  || die "the --destination parent must use its canonical path without symlinks"
[[ "$DESTINATION" == "$DESTINATION_PARENT/$DESTINATION_NAME" ]] \
  || die "--destination must use its canonical parent path"
[[ "$(/usr/bin/stat -c '%u' -- "$DESTINATION_PARENT")" == "$CURRENT_UID" ]] \
  || die "the --destination parent must be owned by the current user"
[[ "$(/usr/bin/stat -c '%a' -- "$DESTINATION_PARENT")" == 700 ]] \
  || die "the --destination parent must have mode 0700"
require_secure_ancestor_chain "$DESTINATION_PARENT" directory
ACTUAL_ARCHIVE_SIZE=$(/usr/bin/stat -c '%s' -- "$ARCHIVE")
[[ "$ACTUAL_ARCHIVE_SIZE" == "$EXPECTED_ARCHIVE_SIZE" ]] \
  || die "archive size does not match the reviewed $RELEASE_TAG $ASSET_NAME"

SCRATCH_PREFIX="$DESTINATION_PARENT/.${DESTINATION_NAME}.gpt2agent-runtime-install."
SCRATCH=
SCRATCH_IDENTITY=
PUBLICATION_ARMED=0
PUBLISHED_IDENTITY=
cleanup() {
  local status=$?
  local current_identity=
  trap - EXIT HUP INT TERM
  if (( status != 0 && PUBLICATION_ARMED == 1 )); then
    if [[ -d "$DESTINATION" && ! -L "$DESTINATION" ]] \
      && current_identity=$(/usr/bin/stat -c '%d:%i' -- "$DESTINATION") \
      && [[ "$current_identity" == "$PUBLISHED_IDENTITY" ]]; then
      /usr/bin/chmod -R u+w -- "$DESTINATION" 2>/dev/null || status=1
      /usr/bin/rm -rf -- "$DESTINATION" || status=1
    fi
  fi
  if [[ -n "$SCRATCH" ]]; then
    if [[ "$SCRATCH" == "$SCRATCH_PREFIX"* && -d "$SCRATCH" && ! -L "$SCRATCH" ]] \
      && current_identity=$(/usr/bin/stat -c '%d:%i' -- "$SCRATCH") \
      && [[ "$current_identity" == "$SCRATCH_IDENTITY" ]]; then
      /usr/bin/chmod -R u+w -- "$SCRATCH" 2>/dev/null || status=1
      /usr/bin/rm -rf -- "$SCRATCH" || status=1
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

SCRATCH=$(/usr/bin/mktemp -d -- "${SCRATCH_PREFIX}XXXXXXXX")
/usr/bin/chmod 700 -- "$SCRATCH"
SCRATCH_IDENTITY=$(/usr/bin/stat -c '%d:%i' -- "$SCRATCH")
require_secure_ancestor_chain "$SCRATCH" directory
ARCHIVE_SNAPSHOT="$SCRATCH/$ASSET_NAME"
/usr/bin/cp --reflink=never -- "$ARCHIVE" "$ARCHIVE_SNAPSHOT"
/usr/bin/chmod 600 -- "$ARCHIVE_SNAPSHOT"
ACTUAL_ARCHIVE_SIZE=$(/usr/bin/stat -c '%s' -- "$ARCHIVE_SNAPSHOT")
[[ "$ACTUAL_ARCHIVE_SIZE" == "$EXPECTED_ARCHIVE_SIZE" ]] \
  || die "archive size changed while copying the reviewed $RELEASE_TAG $ASSET_NAME"
ACTUAL_ARCHIVE_SHA256=$(/usr/bin/sha256sum -- "$ARCHIVE_SNAPSHOT")
ACTUAL_ARCHIVE_SHA256=${ACTUAL_ARCHIVE_SHA256%% *}
[[ "$ACTUAL_ARCHIVE_SHA256" == "$EXPECTED_ARCHIVE_SHA256" ]] \
  || die "archive checksum does not match the reviewed $RELEASE_TAG $ASSET_NAME"

STAGED_RUNTIME="$SCRATCH/runtime"
/usr/bin/install -d -m 700 -- "$STAGED_RUNTIME"
/usr/bin/env -i \
  PATH=/usr/bin:/bin \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  /usr/bin/tar \
  --extract \
  --gzip \
  --file "$ARCHIVE_SNAPSHOT" \
  --directory "$STAGED_RUNTIME" \
  --strip-components=1 \
  --no-same-owner \
  --no-same-permissions
/usr/bin/rm -f -- "$ARCHIVE_SNAPSHOT"
/usr/bin/chmod 700 -- "$STAGED_RUNTIME"
/usr/bin/chmod -R go-w -- "$STAGED_RUNTIME"

verify_runtime() {
  local runtime=$1
  local python="$runtime/bin/python3.12"
  local actual_python_sha256 identity expected_identity private_home

  [[ "$(/usr/bin/stat -c '%u' -- "$runtime")" == "$CURRENT_UID" ]] \
    || die "runtime root must be owned by the current user"
  [[ "$(/usr/bin/stat -c '%a' -- "$runtime")" == 700 ]] \
    || die "runtime root must have mode 0700"
  require_secure_ancestor_chain "$runtime" directory
  [[ -f "$python" && -x "$python" && ! -L "$python" ]] \
    || die "the reviewed archive did not contain a regular executable bin/python3.12"
  [[ "$python" == "$(/usr/bin/realpath -e -- "$python")" ]] \
    || die "bin/python3.12 must use its canonical non-symlink path"
  [[ "$(/usr/bin/stat -c '%u' -- "$python")" == "$CURRENT_UID" ]] \
    || die "bin/python3.12 must be owned by the current user"
  require_secure_ancestor_chain "$python" file
  actual_python_sha256=$(/usr/bin/sha256sum -- "$python")
  actual_python_sha256=${actual_python_sha256%% *}
  [[ "$actual_python_sha256" == "$EXPECTED_PYTHON_SHA256" ]] \
    || die "Python executable checksum does not match the reviewed $ASSET_NAME"

  private_home="$SCRATCH/home"
  if [[ ! -d "$private_home" ]]; then
    /usr/bin/install -d -m 700 -- "$private_home"
  fi
  identity=$(
    /usr/bin/env -i \
      HOME="$private_home" \
      PATH=/usr/bin:/bin \
      LANG=C.UTF-8 \
      LC_ALL=C.UTF-8 \
      PYTHONDONTWRITEBYTECODE=1 \
      PYTHONNOUSERSITE=1 \
      PYTHONUTF8=1 \
      "$python" -I -S -B -c '
import os
import sys

expected = ("cpython", (3, 12, 13), "linux", "x86_64")
actual = (
    sys.implementation.name,
    sys.version_info[:3],
    sys.platform,
    os.uname().machine,
)
for value in (
    actual[0],
    ".".join(str(part) for part in actual[1]),
    actual[2],
    actual[3],
    sys.base_prefix,
    sys.executable,
):
    print(value)
raise SystemExit(0 if actual == expected else 86)
'
  ) || die "reviewed asset is not CPython 3.12.13 for Linux x86_64"
  expected_identity=$'cpython\n3.12.13\nlinux\nx86_64\n'"$runtime"$'\n'"$python"
  [[ "$identity" == "$expected_identity" ]] \
    || die "reviewed asset is not canonical CPython 3.12.13 for Linux x86_64"
}

verify_runtime "$STAGED_RUNTIME"
PUBLISHED_IDENTITY=$(/usr/bin/stat -c '%d:%i' -- "$STAGED_RUNTIME")
PUBLICATION_ARMED=1
if ! /usr/bin/mv -T --no-clobber -- "$STAGED_RUNTIME" "$DESTINATION"; then
  if [[ (-e "$STAGED_RUNTIME" || -L "$STAGED_RUNTIME") \
    && (-e "$DESTINATION" || -L "$DESTINATION") ]]; then
    die "--destination appeared during installation"
  fi
  die "could not atomically publish the reviewed runtime"
fi
[[ ! -e "$STAGED_RUNTIME" && ! -L "$STAGED_RUNTIME" ]] \
  || die "--destination appeared during installation"
[[ -d "$DESTINATION" && ! -L "$DESTINATION" ]] \
  || die "published runtime is missing or is not a regular directory"
[[ "$(/usr/bin/stat -c '%d:%i' -- "$DESTINATION")" == "$PUBLISHED_IDENTITY" ]] \
  || die "published runtime identity changed during installation"
verify_runtime "$DESTINATION"
[[ "$(/usr/bin/stat -c '%d:%i' -- "$DESTINATION")" == "$PUBLISHED_IDENTITY" ]] \
  || die "published runtime identity changed during verification"
printf '%s\n' "$DESTINATION"
PUBLICATION_ARMED=0
