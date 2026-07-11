#!/bin/bash -p
# Verify retained private account evidence against the exact public tag object.

set -euo pipefail
set +x
PATH=/usr/bin:/bin
export PATH
hash -r
umask 077

if [[ ${GPT2AGENT_RECEIPT_AUDIT_CLEAN_ENV-} != 1 ]]; then
  exec /usr/bin/env -i \
    GPT2AGENT_RECEIPT_AUDIT_CLEAN_ENV=1 \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
    /bin/bash -p "$0" "$@"
fi
unset GPT2AGENT_RECEIPT_AUDIT_CLEAN_ENV

usage() {
  printf '%s\n' \
    'usage: audit_retained_receipt.sh --repository OWNER/REPO --tag vX.Y.Z' \
    '       --operator-home PATH --evidence-directory PATH' \
    '       --trusted-python-archive PATH --git /usr/bin/git' >&2
  exit 2
}

die() {
  printf 'retained receipt audit: %s\n' "$*" >&2
  exit 1
}

REPOSITORY=
TAG=
OPERATOR_HOME=
EVIDENCE_DIRECTORY=
TRUSTED_PYTHON_ARCHIVE=
GIT_BIN=
SYSTEM_PYTHON=/usr/bin/python3.12
TRUSTED_PYTHON_SHA256=f7014f68e3c8f180811740735cf1dd5c28be6cff84db11d0ced2a8cd039670a0
TRUSTED_PYTHON_TREE_SHA256=d3a6bd32b73612fce20dbfe1eebd33f2b6ebd1b42b13aa8b1fd1549065be2cc0
TAGGED_EXTRACTOR_SHA256=eeab65a476d1b61b537962819599c839dd05967e6742a591a94acbb1f7000b84
TAGGED_HASHER_SHA256=a53e38f11121dd8b29ce4bf89f502d16c1d717ad835a78563155138dc6e77c36
TAGGED_TAG_VERIFIER_SHA256=b187dd4e7646b9798561410f20607f4802d4ba0b6d2dd1662f223d80964fad02

while (($#)); do
  case "$1" in
    --repository) REPOSITORY=${2-}; shift 2 ;;
    --tag) TAG=${2-}; shift 2 ;;
    --operator-home) OPERATOR_HOME=${2-}; shift 2 ;;
    --evidence-directory) EVIDENCE_DIRECTORY=${2-}; shift 2 ;;
    --trusted-python-archive) TRUSTED_PYTHON_ARCHIVE=${2-}; shift 2 ;;
    --git) GIT_BIN=${2-}; shift 2 ;;
    *) usage ;;
  esac
done

for required in \
  REPOSITORY TAG OPERATOR_HOME EVIDENCE_DIRECTORY \
  TRUSTED_PYTHON_ARCHIVE GIT_BIN; do
  [[ -n ${!required} ]] || usage
done
[[ $REPOSITORY =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || usage
[[ $TAG =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-(alpha|beta|rc)[0-9]+)?$ ]] || usage

CURRENT_UID=$(/usr/bin/id -u)

trusted_system_executable() {
  local supplied=$1
  local expected=$2
  local current owner mode links
  [[ $supplied == "$expected" && -f $supplied && -x $supplied && ! -L $supplied ]] \
    || die "trusted system executable must be $expected"
  [[ $(/usr/bin/realpath -e -- "$supplied") == "$expected" ]] \
    || die "trusted system executable must be $expected"
  owner=$(/usr/bin/stat -c '%u' -- "$expected")
  mode=$(/usr/bin/stat -c '%a' -- "$expected")
  links=$(/usr/bin/stat -c '%h' -- "$expected")
  [[ $owner == 0 && $links == 1 ]] \
    || die "trusted system executable metadata is invalid"
  (( (8#$mode & 07022) == 0 )) \
    || die "trusted system executable mode is invalid"
  current=$(/usr/bin/dirname -- "$expected")
  while :; do
    [[ -d $current && ! -L $current ]] \
      || die "trusted system executable ancestry is invalid"
    owner=$(/usr/bin/stat -c '%u' -- "$current")
    mode=$(/usr/bin/stat -c '%a' -- "$current")
    [[ $owner == 0 ]] || die "trusted system executable ancestry is not root-owned"
    (( (8#$mode & 0022) == 0 )) \
      || die "trusted system executable ancestry is writable"
    [[ $current == / ]] && break
    current=$(/usr/bin/dirname -- "$current")
  done
  printf '%s\n' "$expected"
}

validate_protected_ancestry() {
  local current=$1
  local owner mode
  current=$(/usr/bin/realpath -e -- "$current") \
    || die "trusted path ancestry is unavailable"
  while :; do
    [[ -d $current && ! -L $current ]] || die "trusted path ancestry is invalid"
    owner=$(/usr/bin/stat -c '%u' -- "$current")
    mode=$(/usr/bin/stat -c '%a' -- "$current")
    [[ $owner == 0 || $owner == "$CURRENT_UID" ]] \
      || die "trusted path ancestry owner is invalid"
    if (( (8#$mode & 0022) != 0 )); then
      if [[ $owner != 0 ]] || (( (8#$mode & 01000) == 0 )); then
        die "trusted path ancestry is writable"
      fi
    fi
    [[ $current == / ]] && break
    current=$(/usr/bin/dirname -- "$current")
  done
}

canonical_directory() {
  local supplied=$1
  local canonical owner mode
  [[ $supplied == /* && -d $supplied && ! -L $supplied ]] \
    || die "trusted directory is unavailable"
  canonical=$(/usr/bin/realpath -e -- "$supplied") \
    || die "trusted directory is unavailable"
  [[ $canonical == "$supplied" ]] || die "trusted directory path is not canonical"
  owner=$(/usr/bin/stat -c '%u' -- "$canonical")
  mode=$(/usr/bin/stat -c '%a' -- "$canonical")
  [[ $owner == "$CURRENT_UID" ]] || die "trusted directory owner is invalid"
  (( (8#$mode & 0022) == 0 )) \
    || die "trusted directory is group- or world-writable"
  validate_protected_ancestry "$canonical"
  printf '%s\n' "$canonical"
}

canonical_private_file() {
  local supplied=$1
  local description=$2
  local canonical owner mode links
  [[ $supplied == /* && -f $supplied && ! -L $supplied ]] \
    || die "$description is unavailable"
  canonical=$(/usr/bin/realpath -e -- "$supplied") \
    || die "$description is unavailable"
  [[ $canonical == "$supplied" ]] || die "$description path is not canonical"
  owner=$(/usr/bin/stat -c '%u' -- "$canonical")
  mode=$(/usr/bin/stat -c '%a' -- "$canonical")
  links=$(/usr/bin/stat -c '%h' -- "$canonical")
  [[ $owner == "$CURRENT_UID" && $links == 1 ]] \
    || die "$description metadata is invalid"
  (( (8#$mode & 0022) == 0 )) || die "$description is group- or world-writable"
  validate_protected_ancestry "$(/usr/bin/dirname -- "$canonical")"
  printf '%s\n' "$canonical"
}

GIT_BIN=$(trusted_system_executable "$GIT_BIN" /usr/bin/git)
SYSTEM_PYTHON=$(trusted_system_executable "$SYSTEM_PYTHON" /usr/bin/python3.12)
OPERATOR_HOME=$(canonical_directory "$OPERATOR_HOME")
EVIDENCE_DIRECTORY=$(canonical_directory "$EVIDENCE_DIRECTORY")
TRUSTED_PYTHON_ARCHIVE=$(canonical_private_file \
  "$TRUSTED_PYTHON_ARCHIVE" "reviewed CPython archive")

run_git() {
  /usr/bin/env -i \
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_SYSTEM=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
    GIT_TERMINAL_PROMPT=0 HOME=/nonexistent \
    LANG=C LC_ALL=C PATH=/usr/bin:/bin \
    "$GIT_BIN" -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"
}

AUDIT_ROOT=$(/usr/bin/mktemp -d -- "$OPERATOR_HOME/.gpt2agent-receipt-audit.XXXXXXXX")
[[ $AUDIT_ROOT == "$OPERATOR_HOME"/.gpt2agent-receipt-audit.* ]] \
  || die "private audit path is invalid"
/usr/bin/chmod 700 -- "$AUDIT_ROOT"

cleanup_receipt_audit() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ -d $AUDIT_ROOT && ! -L $AUDIT_ROOT && \
    $AUDIT_ROOT == "$OPERATOR_HOME"/.gpt2agent-receipt-audit.* ]]; then
    /usr/bin/chmod -R u+w -- "$AUDIT_ROOT" || status=1
    /usr/bin/rm -rf -- "$AUDIT_ROOT" || status=1
  else
    status=1
  fi
  return "$status"
}
trap cleanup_receipt_audit EXIT
trap 'exit 130' HUP INT TERM

FETCH_REPOSITORY="$AUDIT_ROOT/source.git"
TAG_REF=refs/release-verification/tag
MAIN_REF=refs/release-verification/main
run_git init --bare --quiet "$FETCH_REPOSITORY"
run_git --git-dir="$FETCH_REPOSITORY" \
  -c credential.helper= -c core.askPass= \
  -c http.followRedirects=false -c http.sslVerify=true \
  -c protocol.file.allow=never -c protocol.ext.allow=never \
  -c protocol.version=2 -c transfer.fsckObjects=true \
  fetch --no-tags --no-write-fetch-head \
  "https://github.com/$REPOSITORY.git" \
  "refs/tags/$TAG:$TAG_REF" "refs/heads/main:$MAIN_REF"
run_git --git-dir="$FETCH_REPOSITORY" fsck --strict --full --no-reflogs
[[ $(run_git --git-dir="$FETCH_REPOSITORY" cat-file -t "$TAG_REF") == tag ]] \
  || die "public release ref is not an annotated tag"
[[ $(run_git --git-dir="$FETCH_REPOSITORY" cat-file -t "$TAG_REF^{}") == commit ]] \
  || die "public release tag does not peel to a commit"
COMMIT=$(run_git --git-dir="$FETCH_REPOSITORY" rev-parse "$TAG_REF^{}")
TREE=$(run_git --git-dir="$FETCH_REPOSITORY" rev-parse "$TAG_REF^{tree}")
[[ $COMMIT =~ ^[0-9a-f]{40}$ && $TREE =~ ^[0-9a-f]{40}$ ]] \
  || die "public release object identity is invalid"
run_git --git-dir="$FETCH_REPOSITORY" merge-base --is-ancestor "$COMMIT" "$MAIN_REF" \
  || die "public release commit is not on public main"

extract_tagged_file() {
  local repository_path=$1
  local output=$2
  local output_mode=$3
  local expected_size=$4
  local expected_sha256=$5
  local entry entry_mode entry_type entry_oid entry_path observed_size observed_sha256
  entry=$(run_git --git-dir="$FETCH_REPOSITORY" ls-tree "$TAG_REF" -- "$repository_path")
  IFS=$' \t' read -r entry_mode entry_type entry_oid entry_path <<<"$entry"
  [[ $entry_mode == 100644 || $entry_mode == 100755 ]] \
    || die "tagged verifier mode is invalid"
  [[ $entry_type == blob && $entry_oid =~ ^[0-9a-f]{40}$ && \
    $entry_path == "$repository_path" ]] || die "tagged verifier object is invalid"
  observed_size=$(run_git --git-dir="$FETCH_REPOSITORY" cat-file -s "$entry_oid")
  [[ $observed_size == "$expected_size" ]] || die "tagged verifier size is not pinned"
  run_git --git-dir="$FETCH_REPOSITORY" cat-file blob "$entry_oid" >"$output"
  observed_sha256=$(/usr/bin/sha256sum -- "$output")
  observed_sha256=${observed_sha256%% *}
  [[ $observed_sha256 == "$expected_sha256" ]] \
    || die "tagged verifier digest is not pinned"
  /usr/bin/chmod "$output_mode" -- "$output"
}

TAGGED_EXTRACTOR="$AUDIT_ROOT/extract_trusted_python.py"
TAGGED_HASHER="$AUDIT_ROOT/hash_runtime_tree.sh"
TAGGED_TAG_VERIFIER="$AUDIT_ROOT/release_tag_metadata.py"
extract_tagged_file scripts/extract_trusted_python.py \
  "$TAGGED_EXTRACTOR" 600 19030 "$TAGGED_EXTRACTOR_SHA256"
extract_tagged_file scripts/hash_runtime_tree.sh \
  "$TAGGED_HASHER" 700 3976 "$TAGGED_HASHER_SHA256"
extract_tagged_file scripts/release_tag_metadata.py \
  "$TAGGED_TAG_VERIFIER" 600 17404 "$TAGGED_TAG_VERIFIER_SHA256"

TRUSTED_PYTHON_BASE="$AUDIT_ROOT/cpython-3.12.13-linux-x86_64"
/usr/bin/env -i \
  HOME=/nonexistent LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
  "$SYSTEM_PYTHON" -I -S -B "$TAGGED_EXTRACTOR" \
  --archive "$TRUSTED_PYTHON_ARCHIVE" --destination "$TRUSTED_PYTHON_BASE" \
  || die "reviewed CPython archive could not be installed"
RUNTIME_TREE_SHA256=$(/usr/bin/env -i \
  LANG=C LC_ALL=C PATH=/usr/bin:/bin \
  /bin/bash -p "$TAGGED_HASHER" "$TRUSTED_PYTHON_BASE") \
  || die "installed CPython runtime tree could not be verified"
[[ $RUNTIME_TREE_SHA256 == "$TRUSTED_PYTHON_TREE_SHA256" ]] \
  || die "installed CPython runtime tree digest does not match"
AUDIT_PYTHON="$TRUSTED_PYTHON_BASE/bin/python3.12"
[[ $AUDIT_PYTHON == "$(/usr/bin/realpath -e -- "$AUDIT_PYTHON")" ]] \
  || die "installed CPython path is invalid"
AUDIT_PYTHON_SHA256=$(/usr/bin/sha256sum -- "$AUDIT_PYTHON")
AUDIT_PYTHON_SHA256=${AUDIT_PYTHON_SHA256%% *}
[[ $AUDIT_PYTHON_SHA256 == "$TRUSTED_PYTHON_SHA256" ]] \
  || die "installed CPython executable digest does not match"

run_python_clean() {
  /usr/bin/env -i \
    HOME=/nonexistent LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
    "$AUDIT_PYTHON" -I -S -B "$@"
}

run_python_clean -c \
  'import os,sys; assert (sys.implementation.name,sys.version_info[:3],sys.platform,os.uname().machine)==("cpython",(3,12,13),"linux","x86_64")' \
  || die "installed CPython identity does not match"

RECEIPT="$EVIDENCE_DIRECTORY/gpt2agent-$TAG-$COMMIT.account-receipt.json"
RECEIPT=$(canonical_private_file "$RECEIPT" "retained account receipt")
[[ $(/usr/bin/stat -c '%a' -- "$RECEIPT") == 600 ]] \
  || die "retained account receipt mode must be 600"

TAG_OBJECT="$AUDIT_ROOT/tag-object"
TAG_OUTPUT="$AUDIT_ROOT/tag-output"
run_git --git-dir="$FETCH_REPOSITORY" cat-file tag "$TAG_REF" >"$TAG_OBJECT"
/usr/bin/env -i \
  GITHUB_OUTPUT="$TAG_OUTPUT" HOME=/nonexistent \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
  "$AUDIT_PYTHON" -I -S -B "$TAGGED_TAG_VERIFIER" verify-tag-object \
  --tag-object-file "$TAG_OBJECT" --repository "$REPOSITORY" --tag "$TAG" \
  --commit "$COMMIT" --tree "$TREE"
TAG_RECEIPT_SHA256=$(run_python_clean -c \
  'import pathlib,re,sys; values=[line.split("=",1)[1] for line in pathlib.Path(sys.argv[1]).read_text(encoding="ascii").splitlines() if line.startswith("receipt_sha256=")]; assert len(values)==1 and re.fullmatch(r"[0-9a-f]{64}",values[0]); print(values[0])' \
  "$TAG_OUTPUT") || die "tag receipt digest is invalid"
LOCAL_RECEIPT_SHA256=$(run_python_clean -c \
  'import hashlib,os,stat,sys; p=sys.argv[1]; uid=int(sys.argv[2]); fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW); before=os.fstat(fd); assert stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode)==0o600 and before.st_uid==uid and before.st_nlink==1; digest=hashlib.sha256(); [digest.update(chunk) for chunk in iter(lambda: os.read(fd,1024*1024),b"")]; after=os.fstat(fd); os.close(fd); assert (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns); print(digest.hexdigest())' \
  "$RECEIPT" "$CURRENT_UID") || die "retained account receipt could not be hashed"
[[ $LOCAL_RECEIPT_SHA256 == "$TAG_RECEIPT_SHA256" ]] \
  || die "retained account receipt does not match the public tag commitment"

printf 'retained account receipt verified: %s\ntag: %s\ncommit: %s\nreceipt sha256: %s\n' \
  "$RECEIPT" "$TAG" "$COMMIT" "$LOCAL_RECEIPT_SHA256"

trap - EXIT HUP INT TERM
cleanup_receipt_audit
