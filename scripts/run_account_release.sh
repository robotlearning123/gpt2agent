#!/bin/bash -p
# Run the private account gate and hand its exact evidence to the tag coordinator.

set -euo pipefail
set +x
PATH=/usr/bin:/bin
export PATH
hash -r
umask 077

# Privileged Bash ignores BASH_ENV and imported functions. Re-exec once more so
# token-bearing children inherit only the values explicitly supplied below.
if [[ ${GPT2AGENT_ACCOUNT_RELEASE_CLEAN_ENV-} != 1 ]]; then
  exec /usr/bin/env -i \
    GPT2AGENT_ACCOUNT_RELEASE_CLEAN_ENV=1 \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
    /bin/bash -p "$0" "$@"
fi
unset GPT2AGENT_ACCOUNT_RELEASE_CLEAN_ENV

usage() {
  printf '%s\n' \
    'usage: run_account_release.sh --repository OWNER/REPO --pr NUMBER' \
    '       --operator-home PATH [--codex-home PATH] --evidence-directory PATH' \
    '       --trusted-python-archive PATH' \
    '       --governance-policy PATH --gh /absolute/gh --git /absolute/git' >&2
  exit 2
}

die() {
  printf 'account release operator: %s\n' "$*" >&2
  exit 1
}

REPOSITORY=
PR_NUMBER=
OPERATOR_HOME=
CODEX_HOME=
EVIDENCE_DIRECTORY=
TRUSTED_PYTHON_ARCHIVE=
GOVERNANCE_POLICY=
GH_BIN=
GIT_BIN=
SYSTEM_PYTHON=/usr/bin/python3.12

# Independently recorded from the pinned Astral 20260510 CPython 3.12.13
# install-only asset. The tagged extractor also pins the complete archive.
TRUSTED_PYTHON_SHA256=f7014f68e3c8f180811740735cf1dd5c28be6cff84db11d0ced2a8cd039670a0
TRUSTED_PYTHON_TREE_SHA256=74e93975be819af02939878b97bafb7aa7961adfa31ef7c47845d25e2b88fc07

while (($#)); do
  case "$1" in
    --repository) REPOSITORY=${2-}; shift 2 ;;
    --pr) PR_NUMBER=${2-}; shift 2 ;;
    --operator-home) OPERATOR_HOME=${2-}; shift 2 ;;
    --codex-home) CODEX_HOME=${2-}; shift 2 ;;
    --evidence-directory) EVIDENCE_DIRECTORY=${2-}; shift 2 ;;
    --trusted-python-archive) TRUSTED_PYTHON_ARCHIVE=${2-}; shift 2 ;;
    --governance-policy) GOVERNANCE_POLICY=${2-}; shift 2 ;;
    --gh) GH_BIN=${2-}; shift 2 ;;
    --git) GIT_BIN=${2-}; shift 2 ;;
    *) usage ;;
  esac
done

for required in \
  REPOSITORY PR_NUMBER OPERATOR_HOME EVIDENCE_DIRECTORY \
  TRUSTED_PYTHON_ARCHIVE GOVERNANCE_POLICY \
  GH_BIN GIT_BIN; do
[[ -n ${!required} ]] || usage
done
[[ $REPOSITORY =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || usage
[[ $PR_NUMBER =~ ^[1-9][0-9]*$ ]] || usage

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
    [[ -d $current && ! -L $current ]] \
      || die "trusted path ancestry is invalid"
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

CURRENT_UID=$(/usr/bin/id -u)
GH_BIN=$(trusted_system_executable "$GH_BIN" /usr/bin/gh)
GIT_BIN=$(trusted_system_executable "$GIT_BIN" /usr/bin/git)
SYSTEM_PYTHON=$(trusted_system_executable "$SYSTEM_PYTHON" /usr/bin/python3.12)
OPERATOR_HOME=$(canonical_directory "$OPERATOR_HOME")
EVIDENCE_DIRECTORY=$(canonical_directory "$EVIDENCE_DIRECTORY")
if [[ -z $CODEX_HOME ]]; then CODEX_HOME="$OPERATOR_HOME/.codex"; fi
CODEX_HOME=$(canonical_directory "$CODEX_HOME")
TRUSTED_PYTHON_ARCHIVE=$(canonical_private_file \
  "$TRUSTED_PYTHON_ARCHIVE" "reviewed CPython archive")
GOVERNANCE_POLICY=$(canonical_private_file \
  "$GOVERNANCE_POLICY" "reviewed governance policy")

run_git() {
  /usr/bin/env -i \
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
    HOME=/nonexistent LANG=C LC_ALL=C PATH=/usr/bin:/bin \
    "$GIT_BIN" -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"
}

run_gh() {
  /usr/bin/env -i \
    GH_PROMPT_DISABLED=1 HOME="$OPERATOR_HOME" LANG=C LC_ALL=C PATH=/usr/bin:/bin \
    "$GH_BIN" "$@"
}

operator_token() {
  local token
  token=$(/usr/bin/env -i \
    GH_PROMPT_DISABLED=1 HOME="$OPERATOR_HOME" LANG=C LC_ALL=C PATH=/usr/bin:/bin \
    "$GH_BIN" auth token --hostname github.com) \
    || die "authenticated read token is unavailable"
  [[ -n $token && ${#token} -le 4096 && $token != *[$'\r\n\t ']* ]] \
    || die "authenticated read token is unavailable"
  printf '%s' "$token"
}

run_python_clean() {
  /usr/bin/env -i \
    HOME=/nonexistent LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
    "$VERIFIER_PYTHON" -I -S -B "$@"
}

run_python_operator() {
  local token
  local status=0
  token=$(operator_token)
  /usr/bin/env -i \
    GH_TOKEN="$token" HOME=/nonexistent LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PATH=/usr/bin:/bin \
    "$VERIFIER_PYTHON" -I -S -B "$@" || status=$?
  token=
  return "$status"
}

run_python_account() {
  /usr/bin/env -i \
    CODEX_HOME="$CODEX_HOME" HOME="$OPERATOR_HOME" \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
    "$VERIFIER_PYTHON" -I -S -B "$@"
}

observed_repository=$(run_gh repo view "$REPOSITORY" \
  --json nameWithOwner --jq .nameWithOwner) \
  || die "repository identity could not be verified"
[[ ${observed_repository,,} == "${REPOSITORY,,}" ]] \
  || die "repository identity does not match"
RELEASE_SHA=$(run_gh pr view "$PR_NUMBER" --repo "$REPOSITORY" \
  --json mergeCommit,state \
  --jq 'select(.state == "MERGED") | .mergeCommit.oid') \
  || die "merged release commit could not be resolved"
[[ $RELEASE_SHA =~ ^[0-9a-f]{40}$ ]] || die "release PR is not merged"

RUNTIME_ROOT=$(/usr/bin/mktemp -d -- "$OPERATOR_HOME/.gpt2agent-account-release.XXXXXXXX")
[[ $RUNTIME_ROOT == "$OPERATOR_HOME"/.gpt2agent-account-release.* ]] \
  || die "private runtime path is invalid"
/usr/bin/chmod 700 -- "$RUNTIME_ROOT"
FETCH_REPOSITORY="$RUNTIME_ROOT/source.git"
CHECKOUT="$RUNTIME_ROOT/checkout"
WORKTREE_CREATED=0
EVIDENCE_STAGE=
DIST=
RECEIPT=
DIST_MOVED=0
RECEIPT_MOVED=0
IRREVERSIBLE_STATE_FILE="$RUNTIME_ROOT/irreversible-ref-state"

cleanup_release_runtime() {
  local status=$?
  local preserve_evidence=0
  trap - EXIT HUP INT TERM
  if (( WORKTREE_CREATED == 1 )); then
    run_git --git-dir="$FETCH_REPOSITORY" worktree remove --force "$CHECKOUT" \
      >/dev/null 2>&1 || status=1
  fi
  if [[ -e $IRREVERSIBLE_STATE_FILE || -L $IRREVERSIBLE_STATE_FILE ]]; then
    preserve_evidence=1
  fi
  if (( preserve_evidence == 0 )); then
    if (( RECEIPT_MOVED == 1 )) && [[ -n $RECEIPT ]]; then
      /usr/bin/rm -f -- "$RECEIPT" || status=1
    fi
    if (( DIST_MOVED == 1 )) && [[ -n $DIST ]]; then
      /usr/bin/rm -rf -- "$DIST" || status=1
    fi
  elif (( RECEIPT_MOVED == 1 || DIST_MOVED == 1 )); then
    printf '%s\n' \
      "IRREVERSIBLE POST-REF STATE: preserving release evidence at $RECEIPT and $DIST; inspect the exact remote tag before any retry." >&2
  fi
  if [[ -n $EVIDENCE_STAGE && -d $EVIDENCE_STAGE && ! -L $EVIDENCE_STAGE && \
    $EVIDENCE_STAGE == "$EVIDENCE_DIRECTORY"/.gpt2agent-release-evidence.* ]]; then
    /usr/bin/chmod -R u+w -- "$EVIDENCE_STAGE" || status=1
    /usr/bin/rm -rf -- "$EVIDENCE_STAGE" || status=1
  elif [[ -n $EVIDENCE_STAGE ]]; then
    status=1
  fi
  if [[ -d $RUNTIME_ROOT && ! -L $RUNTIME_ROOT && \
    $RUNTIME_ROOT == "$OPERATOR_HOME"/.gpt2agent-account-release.* ]]; then
    /usr/bin/chmod -R u+w -- "$RUNTIME_ROOT" || status=1
    /usr/bin/rm -rf -- "$RUNTIME_ROOT" || status=1
  else
    status=1
  fi
  return "$status"
}
trap cleanup_release_runtime EXIT
trap 'exit 130' HUP INT TERM

run_git init --bare --quiet "$FETCH_REPOSITORY"
run_git --git-dir="$FETCH_REPOSITORY" \
  -c credential.helper= -c core.askPass= \
  -c http.followRedirects=false -c http.sslVerify=true \
  -c protocol.file.allow=never -c protocol.ext.allow=never \
  -c protocol.version=2 \
  -c transfer.fsckObjects=true \
  fetch --no-tags --no-write-fetch-head \
  "https://github.com/$REPOSITORY.git" \
  refs/heads/main:refs/release-verification/main
run_git --git-dir="$FETCH_REPOSITORY" merge-base --is-ancestor \
  "$RELEASE_SHA" refs/release-verification/main \
  || die "merged release commit is not on public main"
run_git --git-dir="$FETCH_REPOSITORY" worktree add --detach --quiet \
  "$CHECKOUT" "$RELEASE_SHA"
WORKTREE_CREATED=1

COMMIT=$(run_git -C "$CHECKOUT" rev-parse HEAD)
TREE=$(run_git -C "$CHECKOUT" rev-parse 'HEAD^{tree}')
[[ $COMMIT == "$RELEASE_SHA" ]] || die "clean release checkout commit does not match"
[[ -z $(run_git -C "$CHECKOUT" status --porcelain=v1 --untracked-files=all \
  --ignored=matching --ignore-submodules=none) ]] || die "clean release checkout is dirty"
INDEX_STATE="$RUNTIME_ROOT/index-state"
run_git -C "$CHECKOUT" ls-files -v -z >"$INDEX_STATE"
while IFS= read -r -d '' entry; do
  [[ $entry == 'H '* ]] || die "clean release checkout contains hidden index state"
done <"$INDEX_STATE"
/usr/bin/rm -- "$INDEX_STATE"

# Install only from the independently reviewed, byte-pinned archive. The
# checkout's extractor authenticates and normalizes it; the separate tree
# hasher below independently binds the complete installed runtime.
TRUSTED_PYTHON_BASE="$RUNTIME_ROOT/cpython-3.12.13-linux-x86_64"
/usr/bin/env -i \
  HOME=/nonexistent LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
  "$SYSTEM_PYTHON" -I -S -B \
  "$CHECKOUT/scripts/extract_trusted_python.py" \
  --archive "$TRUSTED_PYTHON_ARCHIVE" \
  --destination "$TRUSTED_PYTHON_BASE" \
  || die "reviewed CPython archive could not be installed"
RUNTIME_TREE_SHA256=$(/usr/bin/env -i \
  LANG=C LC_ALL=C PATH=/usr/bin:/bin \
  /bin/bash -p "$CHECKOUT/scripts/hash_runtime_tree.sh" \
  "$TRUSTED_PYTHON_BASE") \
  || die "installed CPython runtime tree could not be verified"
[[ $RUNTIME_TREE_SHA256 == "$TRUSTED_PYTHON_TREE_SHA256" ]] \
  || die "installed CPython runtime tree digest does not match"
TRUSTED_PYTHON="$TRUSTED_PYTHON_BASE/bin/python3.12"
[[ $TRUSTED_PYTHON == "$(/usr/bin/realpath -e -- "$TRUSTED_PYTHON")" ]] \
  || die "installed CPython path is invalid"
INSTALLED_PYTHON_SHA256=$(/usr/bin/sha256sum -- "$TRUSTED_PYTHON")
INSTALLED_PYTHON_SHA256=${INSTALLED_PYTHON_SHA256%% *}
[[ $INSTALLED_PYTHON_SHA256 == "$TRUSTED_PYTHON_SHA256" ]] \
  || die "installed CPython executable digest does not match"
/usr/bin/env -i \
  HOME=/nonexistent LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
  "$TRUSTED_PYTHON" -I -S -B -c \
  'import os,sys; assert (sys.implementation.name,sys.version_info[:3],sys.platform,os.uname().machine)==("cpython",(3,12,13),"linux","x86_64")' \
  || die "installed CPython identity does not match"

VENV_PARENT="$RUNTIME_ROOT/account-runtime"
/usr/bin/install -d -m 700 -- "$VENV_PARENT"
VENV="$VENV_PARENT/venv"
SITE_PACKAGES=$(/usr/bin/env -i \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
  /bin/bash -p "$CHECKOUT/scripts/bootstrap_account_gate.sh" \
  --python "$TRUSTED_PYTHON" \
  --python-sha256 "$TRUSTED_PYTHON_SHA256" \
  --venv "$VENV")
VERIFIER_PYTHON="$VENV/bin/python"
[[ -x $VERIFIER_PYTHON ]] || die "trusted verifier runtime was not created"

run_python_operator "$CHECKOUT/scripts/audit_release_governance.py" \
  --live "$REPOSITORY" --policy "$GOVERNANCE_POLICY" --gh "$GH_BIN"

CANDIDATE_JSON="$RUNTIME_ROOT/candidate.json"
run_python_operator "$CHECKOUT/scripts/verify_main_ci.py" \
  --repository "$REPOSITORY" --commit "$RELEASE_SHA" \
  --print-candidate-json --attempts 180 --delay 10 >"$CANDIDATE_JSON"
candidate_field() {
  run_python_clean -c \
    'import json,pathlib,sys; print(json.loads(pathlib.Path(sys.argv[1]).read_bytes())[sys.argv[2]])' \
    "$CANDIDATE_JSON" "$1"
}
CI_RUN_ID=$(candidate_field run_id)
CI_RUN_ATTEMPT=$(candidate_field run_attempt)
CI_ARTIFACT_ID=$(candidate_field artifact_id)
CI_ARTIFACT_NAME=$(candidate_field artifact_name)
CI_ARTIFACT_DIGEST=$(candidate_field artifact_digest)
CI_ARTIFACT_SIZE=$(candidate_field artifact_size)
CI_ARTIFACT_EXPIRES_AT=$(candidate_field artifact_expires_at)

VERSION=$(run_python_clean -c \
  'import pathlib,sys,tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["project"]["version"])' \
  "$CHECKOUT/pyproject.toml")
TAG="v$VERSION"
run_python_clean "$CHECKOUT/scripts/verify_release.py" --tag "$TAG"
DIST="$EVIDENCE_DIRECTORY/gpt2agent-$TAG-$COMMIT-main-ci-candidate"
RECEIPT="$EVIDENCE_DIRECTORY/gpt2agent-$TAG-$COMMIT.account-receipt.json"
[[ ! -e $DIST && ! -L $DIST && ! -e $RECEIPT && ! -L $RECEIPT ]] \
  || die "release evidence output already exists"
EVIDENCE_STAGE=$(/usr/bin/mktemp -d -- \
  "$EVIDENCE_DIRECTORY/.gpt2agent-release-evidence.XXXXXXXX")
[[ $EVIDENCE_STAGE == "$EVIDENCE_DIRECTORY"/.gpt2agent-release-evidence.* ]] \
  || die "release evidence staging path is invalid"
/usr/bin/chmod 700 -- "$EVIDENCE_STAGE"
STAGED_DIST="$EVIDENCE_STAGE/dist"
STAGED_RECEIPT="$EVIDENCE_STAGE/receipt.json"
run_gh run download "$CI_RUN_ID" --repo "$REPOSITORY" \
  --name "$CI_ARTIFACT_NAME" --dir "$STAGED_DIST"
run_python_clean "$CHECKOUT/scripts/verify_pypi_artifacts.py" \
  --version "$VERSION" --dist "$STAGED_DIST" --require-absent

CREATE_SUMMARY="$RUNTIME_ROOT/create-summary.txt"
run_python_account "$CHECKOUT/scripts/verify_account_receipt.py" create \
  --checkout "$CHECKOUT" --dist "$STAGED_DIST" --output "$STAGED_RECEIPT" \
  --commit "$COMMIT" --tree "$TREE" --expected-plan pro \
  --repository "$REPOSITORY" \
  --ci-run-id "$CI_RUN_ID" --ci-run-attempt "$CI_RUN_ATTEMPT" \
  --ci-artifact-id "$CI_ARTIFACT_ID" \
  --ci-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --ci-artifact-size "$CI_ARTIFACT_SIZE" \
  --ci-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT" \
  --trusted-site-packages "$SITE_PACKAGES" >"$CREATE_SUMMARY"
RECEIPT_SHA256=$(run_python_clean -c \
  'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
  "$STAGED_RECEIPT")
[[ $RECEIPT_SHA256 =~ ^[0-9a-f]{64}$ ]] || die "receipt digest is invalid"
VERIFY_SUMMARY="$RUNTIME_ROOT/verify-summary.txt"
run_python_clean "$CHECKOUT/scripts/verify_account_receipt.py" verify \
  --receipt "$STAGED_RECEIPT" --checkout "$CHECKOUT" --dist "$STAGED_DIST" \
  --commit "$COMMIT" --tree "$TREE" --sha256 "$RECEIPT_SHA256" \
  --repository "$REPOSITORY" \
  --ci-run-id "$CI_RUN_ID" --ci-run-attempt "$CI_RUN_ATTEMPT" \
  --ci-artifact-id "$CI_ARTIFACT_ID" \
  --ci-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --ci-artifact-size "$CI_ARTIFACT_SIZE" \
  --ci-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT" >"$VERIFY_SUMMARY"
/usr/bin/cmp --silent "$CREATE_SUMMARY" "$VERIFY_SUMMARY" \
  || die "receipt create and verify summaries differ"

/usr/bin/mv -T -- "$STAGED_DIST" "$DIST"
DIST_MOVED=1
/usr/bin/mv -T -- "$STAGED_RECEIPT" "$RECEIPT"
RECEIPT_MOVED=1
/usr/bin/rmdir -- "$EVIDENCE_STAGE"
EVIDENCE_STAGE=

/usr/bin/env -i \
  HOME="$OPERATOR_HOME" LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
  /bin/bash -p "$CHECKOUT/scripts/create_release_tag.sh" \
  --python "$VERIFIER_PYTHON" --gh "$GH_BIN" --git "$GIT_BIN" \
  --governance-policy "$GOVERNANCE_POLICY" \
  --checkout "$CHECKOUT" --dist "$DIST" \
  --receipt "$RECEIPT" --receipt-sha256 "$RECEIPT_SHA256" \
  --irreversible-state-file "$IRREVERSIBLE_STATE_FILE" \
  --repository "$REPOSITORY" --version "$VERSION" --tag "$TAG" \
  --commit "$COMMIT" --tree "$TREE" \
  --ci-run-id "$CI_RUN_ID" --ci-run-attempt "$CI_RUN_ATTEMPT" \
  --ci-artifact-id "$CI_ARTIFACT_ID" \
  --ci-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --ci-artifact-size "$CI_ARTIFACT_SIZE" \
  --ci-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT"

DIST_MOVED=0
RECEIPT_MOVED=0

printf 'account receipt: %s\nreceipt sha256: %s\ncandidate artifacts: %s\n' \
  "$RECEIPT" "$RECEIPT_SHA256" "$DIST"

trap - EXIT HUP INT TERM
cleanup_release_runtime
