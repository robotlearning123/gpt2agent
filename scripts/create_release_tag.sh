#!/bin/bash -p
# Create one annotated release tag from a freshly revalidated account receipt.

set -euo pipefail
set +x
PATH=/usr/bin:/bin
export PATH

usage() {
  printf '%s\n' \
    'usage: create_release_tag.sh --python PATH --gh PATH --git PATH' \
    '       --governance-policy PATH --checkout PATH --dist PATH' \
    '       --receipt PATH --receipt-sha256 HEX --irreversible-state-file PATH' \
    '       --repository OWNER/REPO' \
    '       --tag vX.Y.Z --commit HEX --tree HEX --ci-run-id N' \
    '       --ci-run-attempt N --ci-artifact-id N --ci-artifact-digest sha256:HEX' \
    '       --ci-artifact-size N --ci-artifact-expires-at UTC' >&2
  exit 2
}

VERIFIER_PYTHON=
GH_BIN=
GIT_BIN=
GOVERNANCE_POLICY=
CHECKOUT=
DIST=
RECEIPT=
RECEIPT_SHA256=
IRREVERSIBLE_STATE_FILE=
REPOSITORY=
TAG=
COMMIT=
TREE=
CI_RUN_ID=
CI_RUN_ATTEMPT=
CI_ARTIFACT_ID=
CI_ARTIFACT_DIGEST=
CI_ARTIFACT_SIZE=
CI_ARTIFACT_EXPIRES_AT=

while (($#)); do
  case "$1" in
    --python) VERIFIER_PYTHON=${2-}; shift 2 ;;
    --gh) GH_BIN=${2-}; shift 2 ;;
    --git) GIT_BIN=${2-}; shift 2 ;;
    --governance-policy) GOVERNANCE_POLICY=${2-}; shift 2 ;;
    --checkout) CHECKOUT=${2-}; shift 2 ;;
    --dist) DIST=${2-}; shift 2 ;;
    --receipt) RECEIPT=${2-}; shift 2 ;;
    --receipt-sha256) RECEIPT_SHA256=${2-}; shift 2 ;;
    --irreversible-state-file) IRREVERSIBLE_STATE_FILE=${2-}; shift 2 ;;
    --repository) REPOSITORY=${2-}; shift 2 ;;
    --tag) TAG=${2-}; shift 2 ;;
    --commit) COMMIT=${2-}; shift 2 ;;
    --tree) TREE=${2-}; shift 2 ;;
    --ci-run-id) CI_RUN_ID=${2-}; shift 2 ;;
    --ci-run-attempt) CI_RUN_ATTEMPT=${2-}; shift 2 ;;
    --ci-artifact-id) CI_ARTIFACT_ID=${2-}; shift 2 ;;
    --ci-artifact-digest) CI_ARTIFACT_DIGEST=${2-}; shift 2 ;;
    --ci-artifact-size) CI_ARTIFACT_SIZE=${2-}; shift 2 ;;
    --ci-artifact-expires-at) CI_ARTIFACT_EXPIRES_AT=${2-}; shift 2 ;;
    *) usage ;;
  esac
done

for required in \
  VERIFIER_PYTHON GH_BIN GIT_BIN GOVERNANCE_POLICY CHECKOUT DIST RECEIPT \
  RECEIPT_SHA256 IRREVERSIBLE_STATE_FILE REPOSITORY TAG COMMIT TREE CI_RUN_ID CI_RUN_ATTEMPT \
  CI_ARTIFACT_ID CI_ARTIFACT_DIGEST CI_ARTIFACT_SIZE CI_ARTIFACT_EXPIRES_AT; do
  if [[ -z ${!required} ]]; then usage; fi
done
if [[ $VERIFIER_PYTHON != /* || ! -x $VERIFIER_PYTHON ]]; then usage; fi
if [[ $GH_BIN != /* || ! -x $GH_BIN || $GIT_BIN != /* || ! -x $GIT_BIN ]]; then usage; fi
if [[ $GH_BIN != /usr/bin/gh || $GIT_BIN != /usr/bin/git ]]; then usage; fi
if [[ $GOVERNANCE_POLICY != /* || ! -f $GOVERNANCE_POLICY || -L $GOVERNANCE_POLICY ]]; then
  usage
fi
if [[ $CHECKOUT != /* || ! -d $CHECKOUT || -L $CHECKOUT ]]; then usage; fi
if [[ $DIST != /* || ! -d $DIST || -L $DIST ]]; then usage; fi
if [[ $RECEIPT != /* || ! -f $RECEIPT || -L $RECEIPT ]]; then usage; fi
if [[ $IRREVERSIBLE_STATE_FILE != /* || -e $IRREVERSIBLE_STATE_FILE || \
  -L $IRREVERSIBLE_STATE_FILE ]]; then usage; fi
if [[ ! $REPOSITORY =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then usage; fi
if [[ ! $TAG =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-(alpha|beta|rc)[0-9]+)?$ ]]; then
  usage
fi
if [[ ! $COMMIT =~ ^[0-9a-f]{40}$ || ! $TREE =~ ^[0-9a-f]{40}$ ]]; then usage; fi
if [[ ! $RECEIPT_SHA256 =~ ^[0-9a-f]{64}$ ]]; then usage; fi
if [[ ! $CI_ARTIFACT_DIGEST =~ ^sha256:[0-9a-f]{64}$ ]]; then usage; fi
for positive in "$CI_RUN_ID" "$CI_RUN_ATTEMPT" "$CI_ARTIFACT_ID" "$CI_ARTIFACT_SIZE"; do
  if [[ ! $positive =~ ^[1-9][0-9]*$ ]]; then usage; fi
done
if [[ ! $CI_ARTIFACT_EXPIRES_AT =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$ ]]; then
  usage
fi

canonical_directory() {
  (cd -- "$1" && pwd -P)
}

canonical_file() {
  local path=$1
  local parent=${path%/*}
  local name=${path##*/}
  printf '%s/%s\n' "$(canonical_directory "$parent")" "$name"
}

require_canonical_file() {
  local supplied=$1
  local canonical
  canonical=$(canonical_file "$supplied")
  if [[ $canonical != "$supplied" ]]; then
    echo "release input path is not canonical" >&2
    exit 1
  fi
}

require_root_protected_tool() {
  local supplied=$1
  local expected_name=$2
  local owner mode parent parent_owner parent_mode
  if [[ $supplied != "/usr/bin/$expected_name" || ! -f $supplied || \
    ! -x $supplied || -L $supplied ]]; then
    echo "$expected_name is not a trusted system executable" >&2
    exit 1
  fi
  read -r owner mode < <(/usr/bin/stat --format='%u %a' -- "$supplied")
  if [[ $owner != 0 || ! $mode =~ ^[0-7]{3,4}$ ]] || \
    (( (8#$mode & 06022) != 0 || (8#$mode & 0111) == 0 )); then
    echo "$expected_name is not a trusted system executable" >&2
    exit 1
  fi
  for parent in /usr/bin /usr /; do
    read -r parent_owner parent_mode < <(/usr/bin/stat --format='%u %a' -- "$parent")
    if [[ $parent_owner != 0 || ! $parent_mode =~ ^[0-7]{3,4}$ ]] || \
      (( (8#$parent_mode & 0022) != 0 )); then
      echo "$expected_name parent path is not protected" >&2
      exit 1
    fi
  done
}

require_canonical_file "$VERIFIER_PYTHON"
require_canonical_file "$GH_BIN"
require_canonical_file "$GIT_BIN"
require_canonical_file "$GOVERNANCE_POLICY"
require_canonical_file "$RECEIPT"
if [[ $(canonical_file "$IRREVERSIBLE_STATE_FILE") != "$IRREVERSIBLE_STATE_FILE" ]]; then
  echo "irreversible state path is not canonical" >&2
  exit 1
fi
require_root_protected_tool "$GH_BIN" gh
require_root_protected_tool "$GIT_BIN" git
CHECKOUT=$(canonical_directory "$CHECKOUT")
DIST=$(canonical_directory "$DIST")

run_python_clean() {
  /usr/bin/env -i \
    HOME=/nonexistent LC_ALL=C PATH=/usr/bin:/bin \
    "$VERIFIER_PYTHON" -I -S -B "$@"
}

run_python_operator() {
  /usr/bin/env -i \
    GH_TOKEN="$OPERATOR_TOKEN" HOME=/nonexistent LC_ALL=C PATH=/usr/bin:/bin \
    "$VERIFIER_PYTHON" -I -S -B "$@"
}

run_git() {
  /usr/bin/env -i \
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
    HOME=/nonexistent LC_ALL=C PATH=/usr/bin:/bin \
    "$GIT_BIN" -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"
}

run_checkout_git() {
  run_git -C "$CHECKOUT" --work-tree="$CHECKOUT" "$@"
}

verify_checkout_state() {
  local root index_state index_entry marker
  root=$(run_checkout_git rev-parse --show-toplevel)
  if [[ $root != "$CHECKOUT" ]]; then
    echo "release checkout Git root does not match" >&2
    exit 1
  fi
  if [[ $(run_checkout_git rev-parse HEAD) != "$COMMIT" ]]; then
    echo "release checkout commit does not match" >&2
    exit 1
  fi
  if [[ $(run_checkout_git rev-parse 'HEAD^{tree}') != "$TREE" ]]; then
    echo "release checkout tree does not match" >&2
    exit 1
  fi
  index_state=$(run_checkout_git ls-files -v)
  while IFS= read -r index_entry; do
    marker=${index_entry:0:1}
    if [[ $marker == S || $marker == [a-z] ]]; then
      echo "release checkout index contains hidden paths" >&2
      exit 1
    fi
  done <<< "$index_state"
  if [[ -n $(run_checkout_git status --porcelain=v1 --untracked-files=all \
    --ignored=matching --ignore-submodules=none) ]]; then
    echo "release checkout must be clean" >&2
    exit 1
  fi
}

# Literal root-protected git establishes the exact reviewed checkout before any
# checkout-owned Python module is loaded. The documented release boundary
# trusts the same local user not to mutate these inputs concurrently.
verify_checkout_state

run_python_clean "$CHECKOUT/scripts/verify_release_tools.py" check \
  --gh "$GH_BIN" --git "$GIT_BIN" --policy "$GOVERNANCE_POLICY"
run_python_clean "$CHECKOUT/scripts/audit_release_governance.py" --help >/dev/null

TOKEN_TIMEOUT=${GPT2AGENT_RELEASE_TOKEN_TIMEOUT_SECONDS-300}
if [[ ! $TOKEN_TIMEOUT =~ ^[1-9][0-9]{0,2}$ ]] || (( TOKEN_TIMEOUT > 900 )); then
  echo "release App token timeout is invalid" >&2
  exit 1
fi

OPERATOR_HOME=${HOME-}
if [[ $OPERATOR_HOME != /* || ! -d $OPERATOR_HOME || -L $OPERATOR_HOME ]]; then
  echo "operator home is unavailable" >&2
  exit 1
fi
OPERATOR_HOME=$(canonical_directory "$OPERATOR_HOME")
OPERATOR_UID=$(/usr/bin/id -u)
read -r OPERATOR_HOME_UID OPERATOR_HOME_MODE < <(
  /usr/bin/stat --format='%u %a' -- "$OPERATOR_HOME"
)
if [[ $OPERATOR_HOME_UID != "$OPERATOR_UID" || \
  ! $OPERATOR_HOME_MODE =~ ^[0-7]{3,4}$ ]] || \
  (( (8#$OPERATOR_HOME_MODE & 0022) != 0 )); then
  echo "operator home is not protected" >&2
  exit 1
fi

REQUEST_DIR=
TAG_REQUEST=
MAIN_GIT_DIR=
VERIFY_REF=
OPERATOR_TOKEN=
RELEASE_APP_TOKEN=
REF_MUTATION_ATTEMPTED=0

cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  unset OPERATOR_TOKEN RELEASE_APP_TOKEN GH_TOKEN GITHUB_TOKEN \
    GPT2AGENT_RELEASE_APP_TOKEN
  if [[ -n $REQUEST_DIR && \
    $REQUEST_DIR == "$OPERATOR_HOME"/.gpt2agent-release-tag.* && \
    -d $REQUEST_DIR && ! -L $REQUEST_DIR ]]; then
    /usr/bin/chmod -R u+w -- "$REQUEST_DIR" 2>/dev/null || true
    /usr/bin/rm -rf -- "$REQUEST_DIR"
  fi
  exit "$status"
}
signal_exit() {
  if (( REF_MUTATION_ATTEMPTED == 1 )); then
    printf '%s\n' \
      "IRREVERSIBLE POST-REF STATE: refs/tags/$TAG creation was interrupted after the mutation attempt. Do not retry, update, or delete blindly; independently inspect the exact remote tag." >&2
  fi
  exit 130
}
trap cleanup EXIT
trap signal_exit HUP INT TERM

# Ignore ambient TMPDIR. This fresh, same-user-owned 0700 directory is the
# only scratch root for the tag request and fetched-main ancestry proof.
umask 077
REQUEST_DIR=$(/usr/bin/mktemp -d "$OPERATOR_HOME/.gpt2agent-release-tag.XXXXXXXX")
/usr/bin/chmod 700 "$REQUEST_DIR"
REQUEST_DIR=$(canonical_directory "$REQUEST_DIR")
read -r REQUEST_UID REQUEST_MODE < <(/usr/bin/stat --format='%u %a' -- "$REQUEST_DIR")
if [[ $REQUEST_UID != "$OPERATOR_UID" || $REQUEST_MODE != 700 || \
  ! -d $REQUEST_DIR || -L $REQUEST_DIR ]]; then
  echo "release scratch root is invalid" >&2
  exit 1
fi
TAG_REQUEST="$REQUEST_DIR/tag-request.json"
MAIN_GIT_DIR="$REQUEST_DIR/main.git"
VERIFY_REF="refs/release-verification/${TAG}-${REQUEST_DIR##*.}"

unset GH_TOKEN GITHUB_TOKEN GPT2AGENT_RELEASE_APP_TOKEN GH_HOST GH_CONFIG_DIR GH_DEBUG
OPERATOR_TOKEN=$(/usr/bin/env -i \
  GH_PROMPT_DISABLED=1 HOME="$OPERATOR_HOME" LC_ALL=C PATH=/usr/bin:/bin \
  "$GH_BIN" auth token --hostname github.com)
if [[ -z $OPERATOR_TOKEN || ${#OPERATOR_TOKEN} -gt 4096 || \
  $OPERATOR_TOKEN == *[$'\r\n\t ']* ]]; then
  echo "authenticated read token is unavailable" >&2
  exit 1
fi

# The immutable full-SHA action gate runs before asking for the mutation
# credential. Missing, inaccessible, or byte-mismatched pins therefore fail
# without acquiring a release App token.
ACTION_PIN=$(run_python_operator "$CHECKOUT/scripts/verify_remote_action_pin.py" \
  --gh "$GH_BIN" --repository "$REPOSITORY" \
  --workflow "$CHECKOUT/.github/workflows/release.yml" \
  --action-directory "$CHECKOUT/.github/actions/publish-exact-github-release" \
  --print-pin)
if [[ ! $ACTION_PIN =~ ^[0-9a-f]{40}$ ]]; then
  echo "reviewed publication action pin is invalid" >&2
  exit 1
fi

run_git init --bare --quiet "$MAIN_GIT_DIR"
fetch_and_verify_action_on_main() {
  run_git --git-dir="$MAIN_GIT_DIR" \
    -c credential.helper= -c core.askPass= -c core.hooksPath=/dev/null \
    -c protocol.file.allow=never -c transfer.fsckObjects=true \
    fetch --force --no-tags --no-write-fetch-head \
    "https://github.com/$REPOSITORY.git" \
    "+refs/heads/main:refs/remotes/origin/main"
  if ! run_git --git-dir="$MAIN_GIT_DIR" merge-base --is-ancestor \
    "$ACTION_PIN" refs/remotes/origin/main; then
    echo "reviewed publication action pin is not an ancestor of fetched origin/main" >&2
    exit 1
  fi
}
fetch_and_verify_action_on_main

if ! IFS= read -r -s -t "$TOKEN_TIMEOUT" \
  -p "Short-lived release App installation token: " RELEASE_APP_TOKEN; then
  printf '\nrelease App token acquisition failed\n' >&2
  exit 1
fi
printf '\n' >&2
if [[ -z $RELEASE_APP_TOKEN || ${#RELEASE_APP_TOKEN} -gt 4096 || \
  $RELEASE_APP_TOKEN == *[$'\r\n\t ']* ]]; then
  echo "release App token is invalid" >&2
  exit 1
fi
if [[ $RELEASE_APP_TOKEN == "$OPERATOR_TOKEN" ]]; then
  echo "operator and release App tokens must be distinct" >&2
  exit 1
fi

# This is the final closed pre-mutation sequence. The App token remains a shell
# variable and is passed only to exact gh invocations below.
run_python_operator "$CHECKOUT/scripts/verify_main_ci.py" \
  --repository "$REPOSITORY" --commit "$COMMIT" \
  --expected-run-id "$CI_RUN_ID" \
  --expected-run-attempt "$CI_RUN_ATTEMPT" \
  --expected-artifact-id "$CI_ARTIFACT_ID" \
  --expected-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --expected-artifact-size "$CI_ARTIFACT_SIZE" \
  --expected-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT" \
  --minimum-artifact-lifetime-hours 1 --attempts 1 --delay 0

run_python_operator "$CHECKOUT/scripts/audit_release_governance.py" \
  --live "$REPOSITORY" --policy "$GOVERNANCE_POLICY" --gh "$GH_BIN" >/dev/null

run_python_clean "$CHECKOUT/scripts/verify_account_receipt.py" prepare-tag \
  --receipt "$RECEIPT" --checkout "$CHECKOUT" --dist "$DIST" \
  --output "$TAG_REQUEST" --tag "$TAG" \
  --commit "$COMMIT" --tree "$TREE" --sha256 "$RECEIPT_SHA256" \
  --repository "$REPOSITORY" \
  --ci-run-id "$CI_RUN_ID" --ci-run-attempt "$CI_RUN_ATTEMPT" \
  --ci-artifact-id "$CI_ARTIFACT_ID" \
  --ci-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --ci-artifact-size "$CI_ARTIFACT_SIZE" \
  --ci-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT"
if [[ ! -s $TAG_REQUEST || -L $TAG_REQUEST ]]; then
  echo "release tag request was not prepared" >&2
  exit 1
fi

gh_app() {
  /usr/bin/env -i \
    GH_CONFIG_DIR=/nonexistent GH_PROMPT_DISABLED=1 GH_TOKEN="$RELEASE_APP_TOKEN" \
    HOME=/nonexistent LC_ALL=C PATH=/usr/bin:/bin \
    "$GH_BIN" api --hostname github.com "$@"
}

gh_operator_api() {
  /usr/bin/env -i \
    GH_CONFIG_DIR=/nonexistent GH_PROMPT_DISABLED=1 GH_TOKEN="$OPERATOR_TOKEN" \
    HOME=/nonexistent LC_ALL=C PATH=/usr/bin:/bin \
    "$GH_BIN" api --hostname github.com "$@"
}

MATCHING_REFS=$(gh_app --method GET \
  "repos/$REPOSITORY/git/matching-refs/tags/$TAG" --jq '.[].ref')
while IFS= read -r remote_ref; do
  if [[ $remote_ref == "refs/tags/$TAG" ]]; then
    echo "release tag already exists" >&2
    exit 1
  fi
done <<< "$MATCHING_REFS"

# Refresh remote main and source identity at the final pre-object boundary.
fetch_and_verify_action_on_main
verify_checkout_state

if ! TAG_OBJECT_SHA=$(gh_app --method POST \
  "repos/$REPOSITORY/git/tags" --input "$TAG_REQUEST" --jq .sha); then
  echo "annotated tag object creation failed before ref mutation" >&2
  exit 1
fi
if [[ ! $TAG_OBJECT_SHA =~ ^[0-9a-f]{40}$ ]]; then
  echo "created annotated tag object is invalid" >&2
  exit 1
fi

# No checkout-owned code or mutable local input is consumed between this final
# same-user trust-boundary recheck and the one irreversible ref POST.
if ! run_git --git-dir="$MAIN_GIT_DIR" merge-base --is-ancestor \
  "$ACTION_PIN" refs/remotes/origin/main; then
  echo "reviewed publication action pin is not an ancestor of fetched origin/main" >&2
  exit 1
fi

REF_POST_OK=0
if ! (set -o noclobber; printf 'attempted refs/tags/%s object=%s commit=%s\n' \
  "$TAG" "$TAG_OBJECT_SHA" "$COMMIT" >"$IRREVERSIBLE_STATE_FILE"); then
  echo "irreversible state marker could not be created before ref mutation" >&2
  exit 1
fi
/usr/bin/chmod 600 -- "$IRREVERSIBLE_STATE_FILE"
if [[ ! -f $IRREVERSIBLE_STATE_FILE || -L $IRREVERSIBLE_STATE_FILE || \
  $(/usr/bin/stat -c '%u:%a:%h' -- "$IRREVERSIBLE_STATE_FILE") != \
  "$(/usr/bin/id -u):600:1" ]]; then
  echo "irreversible state marker is invalid" >&2
  exit 1
fi
verify_checkout_state
REF_MUTATION_ATTEMPTED=1
if gh_app --method POST \
  "repos/$REPOSITORY/git/refs" \
  --raw-field ref="refs/tags/$TAG" \
  --raw-field sha="$TAG_OBJECT_SHA" >/dev/null; then
  REF_POST_OK=1
fi
READBACK_OK=0
REMOTE_OBJECT_SHA=
if REMOTE_OBJECT_SHA=$(gh_app --method GET \
  "repos/$REPOSITORY/git/ref/tags/$TAG" --jq .object.sha); then
  if [[ $REMOTE_OBJECT_SHA == "$TAG_OBJECT_SHA" ]]; then
    READBACK_OK=1
  fi
fi
unset RELEASE_APP_TOKEN

# A separate read principal performs exact GETs after the single mutation
# attempt. Neither an App readback nor a local fetch alone may recover an
# ambiguous POST.
INDEPENDENT_REF=
INDEPENDENT_TAG=
INDEPENDENT_GET_OK=0
if INDEPENDENT_REF=$(gh_operator_api --method GET \
  "repos/$REPOSITORY/git/ref/tags/$TAG" \
  --jq '[.ref,.object.type,.object.sha]|@tsv'); then
  :
fi
if INDEPENDENT_TAG=$(gh_operator_api --method GET \
  "repos/$REPOSITORY/git/tags/$TAG_OBJECT_SHA" \
  --jq '[.tag,.sha,.object.type,.object.sha]|@tsv'); then
  :
fi
if [[ $INDEPENDENT_REF == $'refs/tags/'"$TAG"$'\ttag\t'"$TAG_OBJECT_SHA" && \
  $INDEPENDENT_TAG == "$TAG"$'\t'"$TAG_OBJECT_SHA"$'\tcommit\t'"$COMMIT" ]]; then
  INDEPENDENT_GET_OK=1
fi

FETCH_OK=0
if run_git --git-dir="$MAIN_GIT_DIR" \
  -c credential.helper= -c core.askPass= -c core.hooksPath=/dev/null \
  -c protocol.file.allow=never -c transfer.fsckObjects=true \
  fetch --no-tags --no-write-fetch-head \
  "https://github.com/$REPOSITORY.git" "refs/tags/$TAG:$VERIFY_REF"; then
  FETCHED_TAG_OBJECT=$(run_git --git-dir="$MAIN_GIT_DIR" rev-parse "$VERIFY_REF")
  if [[ $(run_git --git-dir="$MAIN_GIT_DIR" cat-file -t "$VERIFY_REF") == tag && \
    $FETCHED_TAG_OBJECT == "$TAG_OBJECT_SHA" && \
    $(run_git --git-dir="$MAIN_GIT_DIR" rev-parse "$VERIFY_REF^{}") == "$COMMIT" ]]; then
    FETCH_OK=1
  fi
fi

if (( INDEPENDENT_GET_OK != 1 || FETCH_OK != 1 )); then
  printf '%s\n' \
    "IRREVERSIBLE POST-REF STATE: refs/tags/$TAG creation was attempted, but independent remote verification was absent or mismatched. Do not retry, update, or delete blindly; inspect the exact remote tag and object $TAG_OBJECT_SHA." >&2
  exit 1
fi

if (( REF_POST_OK != 1 || READBACK_OK != 1 )); then
  printf 'release tag created; independent remote verification recovered an ambiguous API result: %s object=%s commit=%s\n' \
    "$TAG" "$TAG_OBJECT_SHA" "$COMMIT"
else
  printf 'release tag created: %s object=%s commit=%s\n' "$TAG" "$TAG_OBJECT_SHA" "$COMMIT"
fi
