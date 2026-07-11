#!/bin/bash
# Create one annotated release tag from a freshly revalidated account receipt.

set -euo pipefail
set +x
PATH=/usr/bin:/bin
export PATH

usage() {
  printf '%s\n' \
    'usage: create_release_tag.sh --python PATH --gh PATH --git PATH' \
    '       --governance-policy PATH --checkout PATH --dist PATH' \
    '       --receipt PATH --receipt-sha256 HEX --repository OWNER/REPO' \
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
  RECEIPT_SHA256 REPOSITORY TAG COMMIT TREE CI_RUN_ID CI_RUN_ATTEMPT \
  CI_ARTIFACT_ID CI_ARTIFACT_DIGEST CI_ARTIFACT_SIZE CI_ARTIFACT_EXPIRES_AT; do
  if [[ -z ${!required} ]]; then usage; fi
done
if [[ $VERIFIER_PYTHON != /* || ! -x $VERIFIER_PYTHON ]]; then usage; fi
if [[ $GH_BIN != /* || ! -x $GH_BIN || $GIT_BIN != /* || ! -x $GIT_BIN ]]; then usage; fi
if [[ $GOVERNANCE_POLICY != /* || ! -f $GOVERNANCE_POLICY || -L $GOVERNANCE_POLICY ]]; then
  usage
fi
if [[ $CHECKOUT != /* || ! -d $CHECKOUT || -L $CHECKOUT ]]; then usage; fi
if [[ $DIST != /* || ! -d $DIST || -L $DIST ]]; then usage; fi
if [[ $RECEIPT != /* || ! -f $RECEIPT || -L $RECEIPT ]]; then usage; fi
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

require_canonical_file "$VERIFIER_PYTHON"
require_canonical_file "$GH_BIN"
require_canonical_file "$GIT_BIN"
require_canonical_file "$GOVERNANCE_POLICY"
require_canonical_file "$RECEIPT"
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
    "$GIT_BIN" "$@"
}

run_python_clean "$CHECKOUT/scripts/verify_release_tools.py" check \
  --gh "$GH_BIN" --git "$GIT_BIN" --policy "$GOVERNANCE_POLICY"
run_python_clean "$CHECKOUT/scripts/audit_release_governance.py" --help >/dev/null

if [[ $(run_git -C "$CHECKOUT" rev-parse HEAD) != "$COMMIT" ]]; then
  echo "release checkout commit does not match" >&2
  exit 1
fi
if [[ $(run_git -C "$CHECKOUT" rev-parse 'HEAD^{tree}') != "$TREE" ]]; then
  echo "release checkout tree does not match" >&2
  exit 1
fi
if [[ -n $(run_git -C "$CHECKOUT" status --porcelain=v1 --untracked-files=all \
  --ignored=matching --ignore-submodules=none) ]]; then
  echo "release checkout must be clean" >&2
  exit 1
fi

SCRATCH_ROOT=${TMPDIR:-/tmp}
if [[ $SCRATCH_ROOT != /* || ! -d $SCRATCH_ROOT || -L $SCRATCH_ROOT ]]; then
  echo "release scratch root is invalid" >&2
  exit 1
fi
SCRATCH_ROOT=$(canonical_directory "$SCRATCH_ROOT")
umask 077
REQUEST_DIR=$(/usr/bin/mktemp -d "$SCRATCH_ROOT/gpt2agent-release-tag.XXXXXXXX")
/usr/bin/chmod 700 "$REQUEST_DIR"
TAG_REQUEST="$REQUEST_DIR/tag-request.json"
VERIFY_REF="refs/release-verification/${TAG}-${REQUEST_DIR##*.}"
VERIFY_REF_EXPECTED=
OPERATOR_TOKEN=
RELEASE_APP_TOKEN=
REF_MUTATION_ATTEMPTED=0

cleanup() {
  local status=$?
  local current=
  trap - EXIT HUP INT TERM
  unset OPERATOR_TOKEN RELEASE_APP_TOKEN GH_TOKEN GITHUB_TOKEN \
    GPT2AGENT_RELEASE_APP_TOKEN
  if [[ -n $VERIFY_REF_EXPECTED ]]; then
    current=$(run_git -C "$CHECKOUT" show-ref --hash --verify "$VERIFY_REF" 2>/dev/null || true)
    if [[ $current == "$VERIFY_REF_EXPECTED" ]]; then
      run_git -C "$CHECKOUT" update-ref -d "$VERIFY_REF" "$VERIFY_REF_EXPECTED" \
        >/dev/null 2>&1 || true
    fi
  fi
  /usr/bin/rm -f -- "$TAG_REQUEST"
  /usr/bin/rmdir -- "$REQUEST_DIR" 2>/dev/null || true
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

if run_git -C "$CHECKOUT" show-ref --verify --quiet "$VERIFY_REF"; then
  echo "unique local release verification ref unexpectedly exists" >&2
  exit 1
fi

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
run_python_operator "$CHECKOUT/scripts/verify_remote_action_pin.py" \
  --gh "$GH_BIN" --repository "$REPOSITORY" \
  --workflow "$CHECKOUT/.github/workflows/release.yml" \
  --action-directory "$CHECKOUT/.github/actions/publish-exact-github-release"

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

if ! TAG_OBJECT_SHA=$(gh_app --method POST \
  "repos/$REPOSITORY/git/tags" --input "$TAG_REQUEST" --jq .sha); then
  echo "annotated tag object creation failed before ref mutation" >&2
  exit 1
fi
if [[ ! $TAG_OBJECT_SHA =~ ^[0-9a-f]{40}$ ]]; then
  echo "created annotated tag object is invalid" >&2
  exit 1
fi

REF_POST_OK=0
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
if run_git -C "$CHECKOUT" \
  -c credential.helper= -c core.askPass= -c core.hooksPath=/dev/null \
  -c protocol.file.allow=never -c transfer.fsckObjects=true \
  fetch --no-tags --no-write-fetch-head \
  "https://github.com/$REPOSITORY.git" "refs/tags/$TAG:$VERIFY_REF"; then
  VERIFY_REF_EXPECTED=$(run_git -C "$CHECKOUT" rev-parse "$VERIFY_REF")
  if [[ $(run_git -C "$CHECKOUT" cat-file -t "$VERIFY_REF") == tag && \
    $VERIFY_REF_EXPECTED == "$TAG_OBJECT_SHA" && \
    $(run_git -C "$CHECKOUT" rev-parse "$VERIFY_REF^{}") == "$COMMIT" ]]; then
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
