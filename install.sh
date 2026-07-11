#!/usr/bin/env bash
# gpt2agent — one-line installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/robotlearning123/gpt2agent/main/install.sh | bash
#   ./install.sh                                          # from a checkout
#   ./install.sh --client claude-code                     # install for one client only
#   ./install.sh --no-skill                               # skip the deep-research skill
#   ./install.sh --no-register                            # install package only; skip client wiring
#   ./install.sh --source <path-or-git-url>               # install from a local path or git URL
#
# Steps:
#   1. Ensure Python 3.10+ and pipx are available.
#   2. pipx install gpt2agent (from PyPI by default).
#   3. codex login check (auth via $CODEX_HOME/auth.json or ~/.codex/auth.json).
#   4. gpt2agent install --client <X>   # register with detected MCP clients + drop skill.
set -euo pipefail

GREEN=$'\033[92m'; YELLOW=$'\033[93m'; RED=$'\033[91m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$*"; }
info() { printf "  ${YELLOW}→${RESET} %s\n" "$*"; }
err()  { printf "  ${RED}✗${RESET} %s\n" "$*" >&2; }
h1()   { printf "\n${BOLD}%s${RESET}\n" "$*"; }

CLIENT="all"
TRANSPORT="stdio"
SKILL_FLAG=""
REGISTER=1
SOURCE="gpt2agent"  # default: PyPI
SOURCE_EXPLICIT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --client)       CLIENT="$2"; shift 2 ;;
    --transport)    TRANSPORT="$2"; shift 2 ;;
    --port)         err "--port is unavailable because HTTP transport is disabled; use stdio."; exit 2 ;;
    --no-skill)     SKILL_FLAG="--no-skill"; shift ;;
    --no-register)  REGISTER=0; shift ;;
    --source)       SOURCE="$2"; SOURCE_EXPLICIT=1; shift 2 ;;
    -h|--help)
      sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) err "Unknown option: $1"; exit 2 ;;
  esac
done

if [[ $TRANSPORT != "stdio" ]]; then
  err "HTTP transport is disabled because loopback TCP cannot isolate your ChatGPT account; use stdio."
  exit 2
fi

h1 "gpt2agent installer"

# --- 1. Python 3.10+ -------------------------------------------------------

PYTHON=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if resolved_python=$("$cand" -c 'import os, sys; sys.version_info >= (3, 10) or sys.exit(1); print(os.path.realpath(sys.executable))' 2>/dev/null); then
      PYTHON="$resolved_python"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  err "Python 3.10+ required. Install via your package manager (e.g. brew install python@3.12)."
  exit 1
fi
ok "Python: $($PYTHON --version) ($(command -v "$PYTHON"))"

# --- 2. pipx ----------------------------------------------------------------

if ! command -v pipx >/dev/null 2>&1; then
  info "pipx not found; installing via $PYTHON -m pip install --user pipx"
  # `|| true` so a PEP-668 'externally-managed-environment' failure (Ubuntu/
  # Debian/Fedora) doesn't abort under `set -e` before the fallback hint below.
  "$PYTHON" -m pip install --user --quiet pipx || true
  "$PYTHON" -m pipx ensurepath >/dev/null 2>&1 || true
  # Refresh PATH for this shell so the next call finds pipx.
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
  esac
fi

if ! command -v pipx >/dev/null 2>&1; then
  err "pipx still not on PATH. Install it via your distro and re-run:"
  err "  Debian/Ubuntu:  sudo apt install pipx && pipx ensurepath"
  err "  Fedora:         sudo dnf install pipx && pipx ensurepath"
  err "  macOS:          brew install pipx && pipx ensurepath"
  err "  other:          python3 -m pip install --user pipx   (then open a new shell)"
  exit 1
fi
ok "pipx: $(pipx --version 2>/dev/null || echo present)"

# --- 3. install gpt2agent -------------------------------------------------

# A forced install keeps an existing pipx virtual environment, ignores
# --python, and removes that environment if installation fails. Move an
# existing environment aside first so the requested interpreter is honored
# and any failed or interrupted replacement can restore the previous install.
if ! PIPX_HOME_DIR=$(pipx environment --value PIPX_HOME); then
  err "Could not determine pipx's environment location. Upgrade pipx and retry."
  exit 1
fi
PIPX_VENV_DIR="$PIPX_HOME_DIR/venvs/gpt2agent"
PIPX_BACKUP_DIR=""
restore_pipx_environment() {
  status=$?
  trap - EXIT HUP INT TERM
  if [[ -n "$PIPX_BACKUP_DIR" && -d "$PIPX_BACKUP_DIR" && ! -L "$PIPX_BACKUP_DIR" ]]; then
    rm -rf -- "$PIPX_VENV_DIR"
    if ! mv -- "$PIPX_BACKUP_DIR" "$PIPX_VENV_DIR"; then
      err "Upgrade failed and the previous pipx environment could not be restored from $PIPX_BACKUP_DIR"
      status=1
    fi
  fi
  exit "$status"
}
if [[ -L "$PIPX_VENV_DIR" || ( -e "$PIPX_VENV_DIR" && ! -d "$PIPX_VENV_DIR" ) ]]; then
  err "Existing pipx environment path must be a real directory: $PIPX_VENV_DIR"
  exit 1
fi
if [[ -d "$PIPX_VENV_DIR" && ! -L "$PIPX_VENV_DIR" ]]; then
  info "Replacing existing gpt2agent pipx environment (restored automatically on failure)"
  PIPX_BACKUP_DIR=$(mktemp -d "$PIPX_HOME_DIR/.gpt2agent-upgrade.XXXXXXXX")
  rmdir -- "$PIPX_BACKUP_DIR"
  mv -- "$PIPX_VENV_DIR" "$PIPX_BACKUP_DIR"
  trap restore_pipx_environment EXIT
  trap 'exit 130' HUP INT TERM
fi

if [[ $SOURCE_EXPLICIT -eq 1 && -d "$SOURCE" ]]; then
  info "Installing (editable) from $SOURCE"
  pipx install --editable --force --python "$PYTHON" "$SOURCE"
elif [[ $SOURCE_EXPLICIT -eq 1 ]]; then
  info "Installing from $SOURCE"
  pipx install --force --python "$PYTHON" "$SOURCE"
else
  info "Installing $SOURCE from PyPI"
  if pipx install --force --python "$PYTHON" "$SOURCE" 2>&1; then
    :
  else
    status=$?
    err "PyPI install failed. Check your network and Python toolchain, then retry:"
    err "  pipx install --force --python $PYTHON $SOURCE"
    exit "$status"
  fi
fi

PIPX_APP="$PIPX_VENV_DIR/bin/gpt2agent"
if [[ ! -f "$PIPX_APP" || ! -x "$PIPX_APP" || -L "$PIPX_APP" ]]; then
  err "The new pipx environment does not contain an executable gpt2agent app."
  exit 1
fi
if ! "$PIPX_APP" --version >/dev/null 2>&1; then
  err "The new pipx environment's gpt2agent app failed its version smoke test."
  exit 1
fi
ok "gpt2agent installed"
if ! command -v gpt2agent >/dev/null 2>&1; then
  info "gpt2agent is installed at $PIPX_APP but its app directory is not on PATH."
  info "Run 'pipx ensurepath', then open a new shell."
fi

# --- 4. codex login check --------------------------------------------------

AUTH_FILE="${CODEX_HOME:-$HOME/.codex}/auth.json"
if [[ -f "$AUTH_FILE" ]]; then
  ok "codex token found at $AUTH_FILE (no extra login needed)"
else
  info "No $AUTH_FILE yet. Install codex CLI and run \`codex login\`:"
  info "  https://github.com/openai/codex#installation"
  info "  (or run \`gpt2agent setup\` to paste a token manually)"
fi

# --- 5. register with clients ----------------------------------------------

if [[ $REGISTER -eq 1 ]]; then
  ARGS=(install --client "$CLIENT" --transport "$TRANSPORT")
  if [[ -n "$SKILL_FLAG" ]]; then ARGS+=("$SKILL_FLAG"); fi
  "$PIPX_APP" "${ARGS[@]}"
else
  info "Skipping client registration (--no-register). Run later:"
  info "  $PIPX_APP install --client $CLIENT"
fi
if [[ -n "$PIPX_BACKUP_DIR" ]]; then
  rm -rf -- "$PIPX_BACKUP_DIR"
  PIPX_BACKUP_DIR=""
  trap - EXIT HUP INT TERM
fi

h1 "Done."
echo "  Try:  $PIPX_APP run --stdio   (manual smoke test)"
echo "  Or restart your MCP client (Claude Code / Codex) so it picks up the new server."
