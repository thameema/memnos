#!/usr/bin/env bash
# engram installer — orchestrator
#
# Delegates to install-server.sh and/or install-client.sh based on user choice.
#
# Usage:
#   ./install.sh               # interactive menu
#   ./install.sh --server      # server only
#   ./install.sh --client      # client hooks only (points to existing/remote server)
#   ./install.sh --both        # server + client on this machine
#   curl -fsSL https://raw.githubusercontent.com/thameema/engram/master/install.sh | bash

set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'; CYAN=$'\033[0;36m'; BOLD=$'\033[1m'; DIM=$'\033[2m'; NC=$'\033[0m'

info()    { echo -e "${CYAN}  -->${NC} $*"; }
success() { echo -e "${GREEN}  [ok]${NC} $*"; }
warn()    { echo -e "${YELLOW}  [!]${NC} $*"; }
die()     { echo -e "${RED}  [error]${NC} $*" >&2; exit 1; }

# ─── Banner ───────────────────────────────────────────────────────────────────
echo ""
printf "${BOLD}${BLUE}"
cat <<'BANNER'
   ___    _  _    ___   ___     _     __  __
  | __|  | \| |  / __| | _ \   /_\   |  \/  |
  | _|   | .` | | (_ | |   /  / _ \  | |\/| |
  |___|  |_|\_| \____| |_|\_\ /_/ \_ |_|  |_|
BANNER
printf "${NC}\n"
echo "  Persistent memory + AI governance for Claude Code and LLM agents"
echo ""

# ─── Locate sub-scripts ───────────────────────────────────────────────────────
# When run from a clone: scripts are beside this file.
# When piped via curl: BASH_SOURCE[0] is empty — download from GitHub.
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
fi

GITHUB_RAW="https://raw.githubusercontent.com/thameema/engram/master"

_get_script() {
  local name="$1"   # install-server.sh or install-client.sh
  local local_path="${SCRIPT_DIR:+${SCRIPT_DIR}/${name}}"

  if [[ -n "$local_path" && -f "$local_path" ]]; then
    echo "$local_path"
    return
  fi

  # Download to a temp file
  # NOTE: X placeholders must be at the END of the template — required by BSD mktemp on macOS.
  local tmp
  tmp="$(mktemp /tmp/engram.XXXXXX 2>/dev/null)" || \
    die "Could not create temp file in /tmp (mktemp failed)."
  if curl -fsSL "${GITHUB_RAW}/${name}" -o "$tmp" 2>/dev/null; then
    chmod +x "$tmp"
    echo "$tmp"
  else
    rm -f "$tmp"
    die "Could not find or download ${name}. Run from the engram source directory or check your internet connection."
  fi
}

# ─── Argument parsing ─────────────────────────────────────────────────────────
MODE=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) MODE="server"; shift ;;
    --client) MODE="client"; shift ;;
    --both)   MODE="both";   shift ;;
    --help|-h)
      cat <<HLP
  Usage: ./install.sh [--server|--client|--both] [--version <ref>]

    --server          Install engram server (Docker: ArcadeDB + API)
    --client          Install Claude Code hooks for an existing engram server
    --both            Install server + client on this machine
    --version <ref>   Pin engram to a specific git ref (passed to install-server.sh).
                      Examples:
                        --version v1.4.0     install frozen release v1.4.0
                        --version master     install latest master (default)
                      Default: master (always-current).

  Releases:  https://github.com/thameema/engram/releases
HLP
      exit 0 ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

# ─── Interactive menu (if no mode flag) ───────────────────────────────────────
if [[ -z "$MODE" ]]; then
  echo "  What would you like to install?"
  echo ""
  echo -e "  ${BOLD}1) Full install (server + client)${NC}"
  echo "     -> Run engram server here AND install Claude Code hooks on this machine."
  echo "     -> Best for: local laptop / single-developer setup."
  echo ""
  echo -e "  ${BOLD}2) Server only${NC}"
  echo "     -> Install engram server (Docker). Share the API URL + key with team members."
  echo "     -> Best for: dedicated VM or shared server."
  echo ""
  echo -e "  ${BOLD}3) Client only${NC}"
  echo "     -> Install Claude Code hooks only. Connects to an existing engram server."
  echo "     -> Best for: developer machines pointing at a remote server."
  echo ""
  echo -ne "${CYAN}  ?${NC} Choose [1/2/3] [1]: "
  read -r choice </dev/tty
  choice="${choice:-1}"

  case "$choice" in
    1) MODE="both"   ;;
    2) MODE="server" ;;
    3) MODE="client" ;;
    *) die "Invalid choice: ${choice}. Run ./install.sh --help for options." ;;
  esac
fi

# ─── Dispatch ─────────────────────────────────────────────────────────────────
case "$MODE" in
  server)
    info "Running server installer..."
    SERVER_SCRIPT="$(_get_script install-server.sh)"
    bash "$SERVER_SCRIPT" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
    ;;

  client)
    info "Running client installer..."
    CLIENT_SCRIPT="$(_get_script install-client.sh)"
    bash "$CLIENT_SCRIPT" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
    ;;

  both)
    info "Running server installer..."
    SERVER_SCRIPT="$(_get_script install-server.sh)"
    bash "$SERVER_SCRIPT" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"

    echo ""
    echo -e "${BOLD}>>> Installing Claude Code client hooks${NC}"

    # Extract connection info from the .env the server installer just wrote.
    # v1.4+: .env lives at ~/.engram/.env. Pre-v1.4: ~/.engram-src/.env.
    SERVER_URL="http://localhost:8766"
    SERVER_KEY=""
    for candidate in "${HOME}/.engram/.env" "${HOME}/.engram-src/.env" "${SCRIPT_DIR:-}/.env"; do
      if [[ -n "$candidate" && -f "$candidate" ]]; then
        set -a; source "$candidate"; set +a
        SERVER_KEY="${ENGRAM_API_KEY:-}"
        [[ -n "$SERVER_KEY" ]] && break
      fi
    done

    CLIENT_SCRIPT="$(_get_script install-client.sh)"
    CLIENT_ARGS=()
    [[ -n "$SERVER_URL" ]] && CLIENT_ARGS+=(--server "$SERVER_URL")
    [[ -n "$SERVER_KEY" ]] && CLIENT_ARGS+=(--key "$SERVER_KEY")
    bash "$CLIENT_SCRIPT" "${CLIENT_ARGS[@]}" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
    ;;
esac
