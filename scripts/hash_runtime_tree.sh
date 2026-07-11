#!/usr/bin/bash -p
# Produce the v1 complete-tree digest for a reviewed CPython account runtime.

set +o posix
unset POSIXLY_CORRECT
set -euo pipefail

LANG=C
LC_ALL=C
export LANG LC_ALL
readonly LANG LC_ALL
set +x
PATH=/usr/bin:/bin
export PATH
readonly PATH
hash -r
unset BASH_ENV ENV CDPATH GLOBIGNORE POSIXLY_CORRECT

die() {
  printf 'runtime tree hash: %s\n' "$*" >&2
  exit 1
}

[[ $# == 1 ]] || die "usage: hash_runtime_tree.sh /canonical/runtime/base"
ROOT=$1
[[ $ROOT == /* && -d $ROOT && ! -L $ROOT ]] \
  || die "runtime base must be a canonical directory"
CANONICAL_ROOT=$(/usr/bin/realpath -e -- "$ROOT") \
  || die "runtime base must be a canonical directory"
[[ $CANONICAL_ROOT == "$ROOT" ]] \
  || die "runtime base must be a canonical directory"

CURRENT_UID=$(/usr/bin/id -u)
ROOT_DEVICE=$(/usr/bin/stat -c '%d' -- "$ROOT")

check_owner() {
  local path=$1
  local owner
  owner=$(/usr/bin/stat -c '%u' -- "$path")
  [[ $owner == 0 || $owner == "$CURRENT_UID" ]] \
    || die "runtime path has an untrusted owner: $path"
}

check_protected_mode() {
  local path=$1
  local mode
  mode=$(/usr/bin/stat -c '%a' -- "$path")
  (( (8#$mode & 0022) == 0 )) \
    || die "runtime path is group- or world-writable: $path"
}

# Protect the pathname itself against replacement. A root-owned sticky
# directory such as /tmp is an accepted ancestor because sticky ownership
# prevents another UID from renaming this user's private child directory.
ancestor=$ROOT
while :; do
  check_owner "$ancestor"
  mode=$(/usr/bin/stat -c '%a' -- "$ancestor")
  owner=$(/usr/bin/stat -c '%u' -- "$ancestor")
  if (( (8#$mode & 0022) != 0 )); then
    (( owner == 0 && (8#$mode & 01000) != 0 )) \
      || die "runtime ancestor is group- or world-writable: $ancestor"
  fi
  [[ $ancestor == / ]] && break
  ancestor=$(/usr/bin/dirname -- "$ancestor")
done

generate_manifest() {
  printf 'gpt2agent-cpython-runtime-tree-v1\0'
  /usr/bin/find -P "$ROOT" -mindepth 1 -print0 \
    | /usr/bin/sort -z \
    | while IFS= read -r -d '' path; do
    relative=${path#"$ROOT"/}
    [[ -n $relative && $relative != "$path" ]] \
      || die "runtime tree contains an invalid path"
    [[ ! $relative =~ [[:cntrl:]] ]] \
      || die "runtime tree contains a control character in a path"

    device=$(/usr/bin/stat -c '%d' -- "$path")
    [[ $device == "$ROOT_DEVICE" ]] \
      || die "runtime tree crosses a filesystem boundary: $relative"
    check_owner "$path"

    if [[ -d $path && ! -L $path ]]; then
      check_protected_mode "$path"
      mode=$(/usr/bin/stat -c '%a' -- "$path")
      printf 'D\0%s\0%s\0' "$relative" "$mode"
    elif [[ -f $path && ! -L $path ]]; then
      check_protected_mode "$path"
      links=$(/usr/bin/stat -c '%h' -- "$path")
      [[ $links == 1 ]] \
        || die "runtime regular file has an external hard-link boundary: $relative"
      mode=$(/usr/bin/stat -c '%a' -- "$path")
      size=$(/usr/bin/stat -c '%s' -- "$path")
      digest=$(/usr/bin/sha256sum -- "$path")
      digest=${digest%% *}
      [[ $digest =~ ^[0-9a-f]{64}$ ]] \
        || die "runtime file digest failed: $relative"
      printf 'F\0%s\0%s\0%s\0%s\0' "$relative" "$mode" "$size" "$digest"
    elif [[ -L $path ]]; then
      target=$(/usr/bin/readlink -- "$path") \
        || die "runtime symlink could not be read: $relative"
      [[ $target != /* && ! $target =~ [[:cntrl:]] ]] \
        || die "runtime symlink escapes the tree: $relative"
      resolved=$(/usr/bin/realpath -e -- \
        "$(/usr/bin/dirname -- "$path")/$target") \
        || die "runtime symlink is broken: $relative"
      case "$resolved" in
        "$ROOT"|"$ROOT"/*) ;;
        *) die "runtime symlink escapes the tree: $relative" ;;
      esac
      printf 'L\0%s\0%s\0' "$relative" "$target"
    else
      die "runtime tree contains a non-regular entry: $relative"
    fi
    done
}

tree_digest=$(generate_manifest | /usr/bin/sha256sum) \
  || die "runtime tree traversal failed"
tree_digest=${tree_digest%% *}
[[ $tree_digest =~ ^[0-9a-f]{64}$ ]] || die "runtime tree digest failed"
printf '%s\n' "$tree_digest"
