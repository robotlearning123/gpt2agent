#!/usr/bin/env bash
# Create one annotated release tag from a freshly revalidated account receipt.

set -euo pipefail
set +x

usage() {
  cat >&2 <<'EOF'
usage: create_release_tag.sh --python PATH --checkout PATH --dist PATH
       --receipt PATH --receipt-sha256 HEX --repository OWNER/REPO
       --tag vX.Y.Z --commit HEX --tree HEX --ci-run-id N
       --ci-run-attempt N --ci-artifact-id N --ci-artifact-digest sha256:HEX
       --ci-artifact-size N --ci-artifact-expires-at UTC
EOF
  exit 2
}

VERIFIER_PYTHON=
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
  VERIFIER_PYTHON CHECKOUT DIST RECEIPT RECEIPT_SHA256 REPOSITORY TAG COMMIT TREE \
  CI_RUN_ID CI_RUN_ATTEMPT CI_ARTIFACT_ID CI_ARTIFACT_DIGEST CI_ARTIFACT_SIZE \
  CI_ARTIFACT_EXPIRES_AT; do
  if [[ -z ${!required} ]]; then usage; fi
done
if [[ $VERIFIER_PYTHON != /* || ! -x $VERIFIER_PYTHON ]]; then usage; fi
if [[ $CHECKOUT != /* || ! -d $CHECKOUT || -L $CHECKOUT ]]; then usage; fi
if [[ $DIST != /* || ! -d $DIST || -L $DIST ]]; then usage; fi
if [[ $RECEIPT != /* || ! -f $RECEIPT || -L $RECEIPT ]]; then usage; fi
if [[ ! $REPOSITORY =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then usage; fi
if [[ ! $TAG =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-(alpha|beta|rc)[0-9]+)?$ ]]; then usage; fi
if [[ ! $COMMIT =~ ^[0-9a-f]{40}$ || ! $TREE =~ ^[0-9a-f]{40}$ ]]; then usage; fi
if [[ ! $RECEIPT_SHA256 =~ ^[0-9a-f]{64}$ ]]; then usage; fi
if [[ ! $CI_ARTIFACT_DIGEST =~ ^sha256:[0-9a-f]{64}$ ]]; then usage; fi
for positive in "$CI_RUN_ID" "$CI_RUN_ATTEMPT" "$CI_ARTIFACT_ID" "$CI_ARTIFACT_SIZE"; do
  if [[ ! $positive =~ ^[1-9][0-9]*$ ]]; then usage; fi
done
if [[ ! $CI_ARTIFACT_EXPIRES_AT =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$ ]]; then
  usage
fi

CHECKOUT=$(cd -- "$CHECKOUT" && pwd -P)
DIST=$(cd -- "$DIST" && pwd -P)
RECEIPT_PARENT=$(cd -- "$(dirname -- "$RECEIPT")" && pwd -P)
RECEIPT="$RECEIPT_PARENT/$(basename -- "$RECEIPT")"
if [[ $(git -C "$CHECKOUT" rev-parse HEAD) != "$COMMIT" ]]; then
  echo "release checkout commit does not match" >&2
  exit 1
fi
if [[ $(git -C "$CHECKOUT" rev-parse 'HEAD^{tree}') != "$TREE" ]]; then
  echo "release checkout tree does not match" >&2
  exit 1
fi
if [[ -n $(git -C "$CHECKOUT" status --porcelain=v1 --untracked-files=all \
  --ignored=matching --ignore-submodules=none) ]]; then
  echo "release checkout must be clean" >&2
  exit 1
fi

VERIFY_REF=refs/release-verification/created-tag
if git -C "$CHECKOUT" show-ref --verify --quiet "$VERIFY_REF"; then
  echo "local release verification ref already exists" >&2
  exit 1
fi

REQUEST_DIR=
TAG_REQUEST=
OPERATOR_TOKEN=
RELEASE_APP_TOKEN=
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  unset OPERATOR_TOKEN RELEASE_APP_TOKEN GH_TOKEN GITHUB_TOKEN GPT2AGENT_RELEASE_APP_TOKEN
  git -C "$CHECKOUT" update-ref -d "$VERIFY_REF" >/dev/null 2>&1 || true
  if [[ -n $TAG_REQUEST ]]; then rm -f -- "$TAG_REQUEST"; fi
  if [[ -n $REQUEST_DIR ]]; then rmdir -- "$REQUEST_DIR" 2>/dev/null || true; fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

TOKEN_TIMEOUT=${GPT2AGENT_RELEASE_TOKEN_TIMEOUT_SECONDS-300}
if [[ ! $TOKEN_TIMEOUT =~ ^[1-9][0-9]{0,2}$ ]] || (( TOKEN_TIMEOUT > 900 )); then
  echo "release App token timeout is invalid" >&2
  exit 1
fi

unset GH_TOKEN GITHUB_TOKEN GPT2AGENT_RELEASE_APP_TOKEN
OPERATOR_TOKEN=$(gh auth token)
if [[ -z $OPERATOR_TOKEN || ${#OPERATOR_TOKEN} -gt 4096 || $OPERATOR_TOKEN == *[$'\r\n']* ]]; then
  echo "authenticated read token is unavailable" >&2
  exit 1
fi
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

GH_TOKEN="$OPERATOR_TOKEN" "$VERIFIER_PYTHON" -I -S -B \
  "$CHECKOUT/scripts/verify_main_ci.py" \
  --repository "$REPOSITORY" --commit "$COMMIT" \
  --expected-run-id "$CI_RUN_ID" \
  --expected-run-attempt "$CI_RUN_ATTEMPT" \
  --expected-artifact-id "$CI_ARTIFACT_ID" \
  --expected-artifact-digest "$CI_ARTIFACT_DIGEST" \
  --expected-artifact-size "$CI_ARTIFACT_SIZE" \
  --expected-artifact-expires-at "$CI_ARTIFACT_EXPIRES_AT" \
  --minimum-artifact-lifetime-hours 1 --attempts 1 --delay 0

umask 077
REQUEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/gpt2agent-release-tag.XXXXXXXX")
chmod 700 "$REQUEST_DIR"
TAG_REQUEST="$REQUEST_DIR/tag-request.json"
env -u GH_TOKEN -u GITHUB_TOKEN -u GPT2AGENT_RELEASE_APP_TOKEN \
  "$VERIFIER_PYTHON" -I -S -B \
  "$CHECKOUT/scripts/verify_account_receipt.py" prepare-tag \
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

MATCHING_REFS=$(GH_TOKEN="$RELEASE_APP_TOKEN" gh api \
  "repos/$REPOSITORY/git/matching-refs/tags/$TAG" --jq '.[].ref')
while IFS= read -r remote_ref; do
  if [[ $remote_ref == "refs/tags/$TAG" ]]; then
    echo "release tag already exists" >&2
    exit 1
  fi
done <<< "$MATCHING_REFS"

TAG_OBJECT_SHA=$(GH_TOKEN="$RELEASE_APP_TOKEN" gh api --method POST \
  "repos/$REPOSITORY/git/tags" --input "$TAG_REQUEST" --jq .sha)
if [[ ! $TAG_OBJECT_SHA =~ ^[0-9a-f]{40}$ ]]; then
  echo "created annotated tag object is invalid" >&2
  exit 1
fi
GH_TOKEN="$RELEASE_APP_TOKEN" gh api --method POST \
  "repos/$REPOSITORY/git/refs" \
  --raw-field ref="refs/tags/$TAG" \
  --raw-field sha="$TAG_OBJECT_SHA" >/dev/null
REMOTE_OBJECT_SHA=$(GH_TOKEN="$RELEASE_APP_TOKEN" gh api \
  "repos/$REPOSITORY/git/ref/tags/$TAG" --jq .object.sha)
if [[ $REMOTE_OBJECT_SHA != "$TAG_OBJECT_SHA" ]]; then
  echo "remote release tag object does not match" >&2
  exit 1
fi
unset RELEASE_APP_TOKEN

git -C "$CHECKOUT" fetch --force --no-tags origin "refs/tags/$TAG:$VERIFY_REF"
if [[ $(git -C "$CHECKOUT" cat-file -t "$VERIFY_REF") != tag || \
  $(git -C "$CHECKOUT" rev-parse "$VERIFY_REF") != "$TAG_OBJECT_SHA" || \
  $(git -C "$CHECKOUT" rev-parse "$VERIFY_REF^{}") != "$COMMIT" ]]; then
  echo "remote annotated release tag verification failed" >&2
  exit 1
fi

printf 'release tag created: %s object=%s commit=%s\n' "$TAG" "$TAG_OBJECT_SHA" "$COMMIT"
