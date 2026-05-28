#!/usr/bin/env bash
# engram server installer
#
# Installs the engram server (ArcadeDB + engram API) using Docker.
# Run this on the machine that will HOST engram — could be a laptop,
# a VM, or a remote server.
#
# Usage:
#   ./install-server.sh
#   curl -fsSL https://raw.githubusercontent.com/thameema/engram/master/install-server.sh | bash

set -euo pipefail

# ─── Capture all output to a timestamped log file ────────────────────────────
LOG_FILE="/tmp/engram-install-server-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

info()    { echo -e "${CYAN}  -->${NC} $*"; }
success() { echo -e "${GREEN}  [ok]${NC} $*"; }
warn()    { echo -e "${YELLOW}  [!]${NC} $*"; }
error()   { echo -e "${RED}  [error]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }
step()    { echo ""; echo -e "${BOLD}>>> $*${NC}"; }
ask() {
  local varname="$1" prompt="$2" default="${3:-}"
  if [ -n "$default" ]; then
    echo -ne "${CYAN}  ?${NC} ${prompt} ${DIM}[${default}]${NC}: "
  else
    echo -ne "${CYAN}  ?${NC} ${prompt}: "
  fi
  # Read from /dev/tty when run interactively (curl|bash from a human),
  # or from stdin when piped (agent feeding pre-recorded answers).
  if [ -t 0 ]; then read -r input </dev/tty; else read -r input; fi
  eval "$varname='${input:-$default}'"
}
ask_yn() {
  local varname="$1" prompt="$2" default="${3:-Y}"
  echo -ne "${CYAN}  ?${NC} ${prompt} ${DIM}[${default}]${NC}: "
  # Read from /dev/tty when run interactively (curl|bash from a human),
  # or from stdin when piped (agent feeding pre-recorded answers).
  if [ -t 0 ]; then read -r input </dev/tty; else read -r input; fi
  input="${input:-$default}"
  [[ "$input" =~ ^[Yy] ]] && eval "$varname=yes" || eval "$varname=no"
}
gen_key()      { python3 -c "import secrets; print('engram-' + secrets.token_hex(16))" 2>/dev/null || openssl rand -hex 20; }
gen_pass()     { python3 -c "import secrets,string; print(secrets.token_urlsafe(18)[:20])" 2>/dev/null || openssl rand -base64 16 | tr -dc 'A-Za-z0-9' | head -c 20; }
gen_vault_key(){ python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" 2>/dev/null || openssl rand -base64 32 | tr -d '\n'; }
# Cross-platform sed in-place edit (BSD/macOS + GNU/Linux)
sed_i()        { sed -i.bak "$@" && rm -f "${@: -1}.bak"; }

# ─── Banner ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${BLUE}"
cat <<'BANNER'
   ___    _  _    ___   ___     _     __  __
  | __|  | \| |  / __| | _ \   /_\   |  \/  |
  | _|   | .` | | (_ | |   /  / _ \  | |\/| |
  |___|  |_|\_| \____| |_|\_\ /_/ \_ |_|  |_|
  Server Installer
BANNER
echo -e "${NC}"

# ─── Argument parsing ────────────────────────────────────────────────────────
# --version <ref>    git ref (tag, branch, or commit) to install. Default:
#                    resolved at runtime to the latest GitHub release tag,
#                    falling back to master if the API is unreachable.
# ENGRAM_REF env var also honoured (--version takes precedence).
ENGRAM_REF_ARG=""
# Deployment mode controls security defaults in engram.yaml:
#   server-only (default) — secure: open_mode: false → Bearer auth enforced
#   full                  — convenient: open_mode: true → no auth needed
#                           (only safe on a single-user local laptop)
# DEPLOY_MODE_EXPLICIT lets upgrade-mode auto-preserve the user's existing
# open_mode setting when --mode was NOT passed.
DEPLOY_MODE="server-only"
DEPLOY_MODE_EXPLICIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) ENGRAM_REF_ARG="$2"; shift 2 ;;
    --mode)    DEPLOY_MODE="$2"; DEPLOY_MODE_EXPLICIT=1; shift 2 ;;
    --help|-h)
      cat <<HLP
  Usage: install-server.sh [--version <ref>] [--mode <full|server-only>]

    --version <ref>   Pin to a specific git ref. Examples:
                        --version v1.4.0     install frozen release v1.4.0
                        --version master     install latest master (default)
                        --version <sha>      install a specific commit

                      Default: master (always-current). Override with the
                      ENGRAM_REF env var if you cannot pass arguments.

    --mode <m>        Deployment mode — controls auth defaults:
                        server-only (default) — open_mode: false
                                                 (Bearer auth enforced;
                                                 use for shared / remote / VM)
                        full                  — open_mode: true
                                                 (auth bypassed; use only for
                                                 single-user local laptop)
HLP
      exit 0 ;;
    *) shift ;;
  esac
done

# Validate DEPLOY_MODE
case "$DEPLOY_MODE" in
  full|server-only) ;;
  *) echo "error: --mode must be 'full' or 'server-only', got: $DEPLOY_MODE" >&2; exit 2 ;;
esac

# ─── Detect prior install and choose upgrade / fresh / abort ─────────────────
INSTALL_MODE="fresh"   # fresh | upgrade
detect_existing_install() {
  local src_clone="$HOME/.engram-src"
  local data_dir="$HOME/.engram"
  local found=()

  [ -d "${src_clone}/.git" ] && found+=("source clone:  ${src_clone}")
  [ -f "${src_clone}/.env" ] && found+=("config:         ${src_clone}/.env")
  [ -d "${data_dir}" ] && found+=("data directory: ${data_dir}")

  local containers
  containers="$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E '^engram(-arcadedb|-qdrant)?$' || true)"
  [ -n "$containers" ] && found+=("docker containers: $(echo "$containers" | tr '\n' ' ')")

  [ ${#found[@]} -eq 0 ] && return 0

  step "Previous engram install detected"
  for item in "${found[@]}"; do
    echo "    - $item"
  done
  echo ""
  echo -e "  ${BOLD}1) Upgrade${NC}      — git pull source, rebuild image, restart."
  echo -e "                  ${DIM}Keeps everything: .env, engram.yaml, memories, vault key, open_mode.${NC}"
  echo -e "                  ${DIM}No prompts. Use this for routine version updates.${NC}"
  echo ""
  echo -e "  ${BOLD}2) Fresh install${NC} — ${RED}DELETES all stored memories${NC} and reconfigures from scratch."
  echo -e "                  ${DIM}Wipes ~/.engram/{arcadedb, qdrant, .env, engram.yaml}.${NC}"
  echo -e "                  ${DIM}Source clone (~/.engram-src) is kept. Requires explicit 'yes'.${NC}"
  echo ""
  echo -e "  ${BOLD}3) Abort${NC}        — leave everything as-is."
  echo ""
  ask CHOICE "Choose [1/2/3]" "1"
  case "$CHOICE" in
    1) INSTALL_MODE="upgrade"; info "Mode: upgrade (preserve config and data)" ;;
    2) INSTALL_MODE="fresh"
       confirm_fresh_wipe_or_abort
       info "Mode: fresh install (data wiped, full reconfigure)" ;;
    *) die "Aborted by user. Existing install left untouched." ;;
  esac
}

# ─── Fresh-install destructive confirmation ─────────────────────────────────
# Called only when the user picks Fresh and the data dir has real content.
# Default answer is 'abort'; the user must type the literal string 'yes' to
# proceed. After confirmation, wipe arcadedb/qdrant data + config files so
# the upcoming collect_config / write_env runs against a truly clean slate.
confirm_fresh_wipe_or_abort() {
  local data_dir="$HOME/.engram"
  local has_memories=0

  # "Real content" = arcadedb directory has files in it.
  if [ -d "${data_dir}/arcadedb" ] && \
     find "${data_dir}/arcadedb" -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
    has_memories=1
  fi

  # If nothing to lose, no confirmation needed — silently proceed.
  if [ "$has_memories" -eq 0 ] && [ ! -f "${data_dir}/.env" ]; then
    return 0
  fi

  echo ""
  echo -e "${RED}${BOLD}┌─────────────────────────────────────────────────────────────────────┐${NC}"
  echo -e "${RED}${BOLD}│  ⚠  FRESH INSTALL — DESTRUCTIVE OPERATION                          ⚠  │${NC}"
  echo -e "${RED}${BOLD}└─────────────────────────────────────────────────────────────────────┘${NC}"
  echo ""
  echo -e "  Fresh install will ${BOLD}${RED}PERMANENTLY DELETE${NC} the following:"
  echo ""
  [ "$has_memories" -eq 1 ] && \
    echo -e "    ${RED}•${NC} ${data_dir}/arcadedb/    ${DIM}— all stored memories + vectors${NC}"
  [ -d "${data_dir}/qdrant" ] && \
    echo -e "    ${RED}•${NC} ${data_dir}/qdrant/      ${DIM}— Qdrant HNSW index${NC}"
  [ -f "${data_dir}/.env" ] && \
    echo -e "    ${RED}•${NC} ${data_dir}/.env         ${DIM}— ENGRAM_API_KEY, ARCADEDB_PASSWORD, ENGRAM_VAULT_KEY${NC}"
  [ -f "${data_dir}/engram.yaml" ] && \
    echo -e "    ${RED}•${NC} ${data_dir}/engram.yaml  ${DIM}— configuration (open_mode, embeddings)${NC}"
  echo ""
  echo -e "  ${BOLD}This cannot be undone.${NC} If you only want to update engram software,"
  echo -e "  cancel this prompt and pick ${BOLD}1) Upgrade${NC} on the previous menu instead."
  echo ""
  echo -ne "${RED}${BOLD}  ?${NC} Type ${BOLD}yes${NC} to confirm wipe (any other input aborts) ${DIM}[default: abort]${NC}: "
  # Read from /dev/tty when run interactively (curl|bash from a human),
  # or from stdin when piped (agent feeding pre-recorded answers).
  if [ -t 0 ]; then read -r ans </dev/tty; else read -r ans; fi
  if [ "$ans" != "yes" ]; then
    die "Fresh install aborted — your data was NOT touched."
  fi

  # Stop containers so they release file locks before we rm -rf
  info "Stopping engram containers..."
  docker rm -f engram engram-arcadedb engram-qdrant >/dev/null 2>&1 || true

  info "Wiping ${data_dir}/{arcadedb, qdrant, .env, engram.yaml}..."
  rm -rf "${data_dir}/arcadedb" "${data_dir}/qdrant"
  rm -f  "${data_dir}/.env"     "${data_dir}/engram.yaml"
  # Also drop any obsolete files from older installs
  rm -f  "${data_dir}/keys.db" "${data_dir}/learning.db" "${data_dir}/tasks.db" 2>/dev/null || true

  success "Data directory wiped. Proceeding with fresh configuration."
}

# ─── OS / arch detection ──────────────────────────────────────────────────────
detect_os() {
  ARCH="$(uname -m)"
  case "$(uname -s)" in
    Darwin) OS="macos" ;;
    Linux)
      if grep -qi microsoft /proc/version 2>/dev/null; then OS="wsl"
      else OS="linux"; fi ;;
    *) die "Unsupported OS: $(uname -s). engram server requires macOS, Linux, or WSL." ;;
  esac
  info "Detected: ${OS} (${ARCH})"
}

# ─── Prerequisites ────────────────────────────────────────────────────────────
check_docker() {
  step "Checking Docker"
  command -v docker &>/dev/null || die "Docker not found. Install Docker Desktop (mac/windows) or Docker Engine (linux)."
  docker info &>/dev/null 2>&1 || {
    if [ "$OS" = "macos" ]; then
      warn "Docker not running — starting Docker Desktop..."
      open -a Docker 2>/dev/null || true
      local i=0
      while ! docker info &>/dev/null 2>&1; do
        sleep 3; i=$((i+1)); echo -ne "\r  Waiting for Docker... ${i}s"
        [ $i -ge 30 ] && die "Docker did not start. Launch Docker Desktop manually."
      done; echo ""
    else
      die "Docker daemon not running. Start it with: sudo systemctl start docker"
    fi
  }
  if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
  elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
  else
    die "Docker Compose not found. Install the Compose plugin: https://docs.docker.com/compose/install/"
  fi
  success "Docker: $(docker --version | head -1)"
  success "Compose: $($DC version | head -1)"
}

check_python() {
  PY=""
  for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
      local v; v=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
      local maj; maj=$(echo "$v" | cut -d. -f1)
      local min; min=$(echo "$v" | cut -d. -f2)
      if [ "$maj" -ge 3 ] && [ "$min" -ge 10 ]; then PY="$cmd"; break; fi
    fi
  done
  [ -n "$PY" ] || die "Python 3.10+ required. Install via: https://python.org/downloads"
}

# ─── Collect config ───────────────────────────────────────────────────────────
collect_config() {
  step "Configuration"

  # Data directory (persistent volumes)
  ask DATA_DIR "Data directory (ArcadeDB, keys.db, learning.db, logs)" "$HOME/.engram"
  DATA_DIR="${DATA_DIR/#\~/$HOME}"

  # engram API key
  ask ENGRAM_API_KEY "engram API key (blank = auto-generate)" ""
  [ -z "$ENGRAM_API_KEY" ] && ENGRAM_API_KEY="$(gen_key)" && \
    info "Generated API key: ${BOLD}${ENGRAM_API_KEY}${NC}"

  # ArcadeDB root password
  ask ARCADEDB_PASSWORD "ArcadeDB root password (blank = auto-generate)" ""
  [ -z "$ARCADEDB_PASSWORD" ] && ARCADEDB_PASSWORD="$(gen_pass)" && \
    info "Generated ArcadeDB password: ${BOLD}${ARCADEDB_PASSWORD}${NC}"

  # Vault encryption key — always auto-generated, used to encrypt secrets at rest
  ENGRAM_VAULT_KEY="$(gen_vault_key)"
  info "Generated vault encryption key (saved to .env)"

  # ─── Anthropic key (independent of embeddings) ───────────────────────────
  echo ""
  echo -e "  ${BOLD}Anthropic API key${NC}  ${DIM}(optional)${NC}"
  echo -e "     For LLM reflection & skill extraction."
  echo -e "     ${DIM}If skipped:${NC} engram uses Claude Code built-in ${BOLD}claude --print${NC} CLI."
  echo -e "     ${DIM}Recommended:${NC} skip if you have Claude Code installed (most users)."
  echo ""
  ask ANTHROPIC_API_KEY "Anthropic API key (press Enter to skip)" ""

  # ─── EMBEDDINGS — critical lock-in warning + informed choice ─────────────
  echo ""
  echo -e "${RED}${BOLD}┌─────────────────────────────────────────────────────────────────────┐${NC}"
  echo -e "${RED}${BOLD}│  ⚠  EMBEDDING BACKEND — PICK CAREFULLY, MOSTLY PERMANENT          ⚠  │${NC}"
  echo -e "${RED}${BOLD}└─────────────────────────────────────────────────────────────────────┘${NC}"
  echo ""
  echo -e "  engram writes a vector for every memory you save. Different embedding"
  echo -e "  models produce ${BOLD}different dimensions${NC} (384 vs 1536) and live in"
  echo -e "  ${BOLD}incompatible vector spaces${NC} — vectors from one model cannot be"
  echo -e "  compared to vectors from another. Switching backend later requires:"
  echo ""
  echo -e "    • Re-encoding ${BOLD}every existing memory${NC} (slow + costs API tokens)"
  echo -e "    • Dropping + recreating the vector index"
  echo -e "    • Search is ${BOLD}BROKEN${NC} until the migration completes"
  echo -e "    • Only one direction is scripted today: ${DIM}local → OpenAI${NC}"
  echo -e "      (local → local-model-B and OpenAI → local require a custom script)"
  echo ""
  echo -e "  ${BOLD}Pick the right one NOW based on your use case:${NC}"
  echo ""
  echo -e "  ${BOLD}A) Local embeddings${NC}  ${DIM}(sentence-transformers all-MiniLM-L6-v2, 384-dim)${NC}"
  echo -e "     ${GREEN}Cost:${NC}     \$0 forever. No API calls. No data leaves your machine."
  echo -e "     ${GREEN}Privacy:${NC}  100% offline — ideal for legal / medical / regulated content."
  echo -e "     ${YELLOW}Disk:${NC}     ${BOLD}+2 GB${NC} baked into engram Docker image."
  echo -e "     ${YELLOW}Build:${NC}    adds 3-5 min to first 'docker compose build'."
  echo -e "     ${DIM}Quality:${NC}  ~80% of OpenAI on relevance benchmarks. Good for personal use."
  echo ""
  echo -e "  ${BOLD}B) OpenAI embeddings${NC}  ${DIM}(text-embedding-3-small, 1536-dim)${NC}"
  echo -e "     ${GREEN}Cost:${NC}     ~\$0.02 per 1 million tokens. Indicative lifetime cost:"
  echo -e "                 100 memories  →  ~\$0.0002    (essentially free)"
  echo -e "                 10K memories  →  ~\$0.02      (cents)"
  echo -e "                 100K memories →  ~\$0.20      (pennies)"
  echo -e "                 1M memories   →  ~\$2.00      (still trivial)"
  echo -e "     ${YELLOW}Privacy:${NC}  every memory's text sent to OpenAI servers for embedding."
  echo -e "     ${YELLOW}Network:${NC}  requires internet for every write + every search query."
  echo -e "     ${GREEN}Quality:${NC}  state-of-the-art relevance (best of the three options)."
  echo -e "     ${GREEN}Disk:${NC}     no extra image weight."
  echo ""
  echo -e "  ${BOLD}Recommendations:${NC}"
  echo -e "     ${DIM}•${NC} Heavy use (>10K memories) + okay with cloud → ${BOLD}OpenAI${NC}"
  echo -e "     ${DIM}•${NC} Privacy-sensitive content / offline / no API costs → ${BOLD}Local${NC}"
  echo -e "     ${DIM}•${NC} Light personal use, undecided → ${BOLD}Local${NC} (zero risk, switch later if needed)"
  echo ""
  echo -e "  ${DIM}Migration path (local → OpenAI later):${NC} python3 ~/.engram-src/tools/reembed.py"
  echo ""

  ask OPENAI_API_KEY "OpenAI API key (paste to use OpenAI embeddings, or press Enter to use Local)" ""

  ENGRAM_EMBED_MODE="online"
  if [ -z "${OPENAI_API_KEY}" ]; then
    echo ""
    info "Using ${BOLD}local embeddings${NC} — engram image build will add ~2 GB for sentence-transformers + torch."
    ENGRAM_EMBED_MODE="local"
  else
    info "Using ${BOLD}OpenAI embeddings${NC} (text-embedding-3-small, 1536-dim)."
    info "Memory text will be sent to OpenAI for vector encoding."
  fi

  # ─── Optional Qdrant vector backend ───────────────────────────────────────
  echo ""
  echo -e "  ${BOLD}Vector backend${NC}"
  echo -e "  ${DIM}Default:${NC} ArcadeDB native vectors — works well up to ~100K memories per namespace."
  echo -e "  ${DIM}Optional:${NC} Qdrant adds HNSW ANN search — recommended for larger namespaces."
  echo -e "             Adds one extra container (~100 MB). Storage in ${DIM}${DATA_DIR}/qdrant/${NC}."
  echo ""
  ask_yn USE_QDRANT "Enable Qdrant?" "N"

  # Summarize
  echo ""
  if [ -n "$ANTHROPIC_API_KEY" ]; then
    info "Reflection: Anthropic API"
  else
    info "Reflection: Claude Code built-in (${BOLD}claude --print${NC})"
  fi
  if [ -n "$OPENAI_API_KEY" ]; then
    info "Embeddings: OpenAI (text-embedding-3-small)"
  elif [ "${ENGRAM_EMBED_MODE:-online}" = "local" ]; then
    info "Embeddings: local model baked into image (sentence-transformers, +2 GB)"
  else
    warn "Embeddings: NONE configured — semantic search will error out"
  fi
  if [ "$USE_QDRANT" = "yes" ]; then
    info "Vector backend: ArcadeDB + Qdrant (HNSW ANN)"
  else
    info "Vector backend: ArcadeDB only"
  fi
}

# ─── Create directory structure ───────────────────────────────────────────────
# ─── Resolve which git ref to install (release tag, branch, or commit) ──────
# Default is master (always-current). Users who want a frozen release must
# pass --version <tag> explicitly. This avoids the situation where a user's
# default install lags behind master by N commits while we accumulate fixes
# between releases.
resolve_ref() {
  # Priority: --version arg  >  ENGRAM_REF env  >  master
  if [ -n "${ENGRAM_REF_ARG}" ]; then
    ENGRAM_REF="${ENGRAM_REF_ARG}"
    info "Pinning to ref from --version: ${BOLD}${ENGRAM_REF}${NC}"
    return
  fi
  if [ -n "${ENGRAM_REF:-}" ]; then
    info "Pinning to ref from ENGRAM_REF env: ${BOLD}${ENGRAM_REF}${NC}"
    return
  fi
  ENGRAM_REF="master"
  info "Installing from ${BOLD}master${NC} (always-current default)."
  info "For a frozen release, pass --version v1.x.y (see https://github.com/thameema/engram/releases)."
}

# ─── Resolve source tree (clone if not running from one) ─────────────────────
resolve_source() {
  step "Resolving engram source"
  resolve_ref
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-./install-server.sh}")" 2>/dev/null && pwd || echo "")"
  if [ -f "${SCRIPT_DIR}/docker-compose.yml" ] && [ -f "${SCRIPT_DIR}/docker/Dockerfile" ]; then
    ENGRAM_SRC="${SCRIPT_DIR}"
    info "Using local source at ${ENGRAM_SRC} (ignoring --version because running from a clone)"
    return
  fi

  # Standalone (curl|bash) — clone to a stable location next to the data dir
  ENGRAM_SRC="${HOME}/.engram-src"
  command -v git &>/dev/null || die "git not found. Install git, or run this script from an engram source clone."
  if [ -d "${ENGRAM_SRC}/.git" ]; then
    info "Updating engram source at ${ENGRAM_SRC} → ${ENGRAM_REF}..."
    ( cd "${ENGRAM_SRC}" && git fetch --depth 1 origin "${ENGRAM_REF}" 2>/dev/null \
        && git reset --hard FETCH_HEAD >/dev/null 2>&1 ) \
      || warn "git update to ${ENGRAM_REF} failed — using existing checkout"
  else
    info "Cloning engram@${ENGRAM_REF} to ${ENGRAM_SRC}..."
    git clone --depth 1 --branch "${ENGRAM_REF}" https://github.com/thameema/engram.git "${ENGRAM_SRC}" 2>&1 | tail -2 \
      || die "git clone failed for ref '${ENGRAM_REF}'. Check the ref exists: https://github.com/thameema/engram/releases"
  fi
  [ -f "${ENGRAM_SRC}/docker-compose.yml" ] || die "Clone is missing docker-compose.yml — repo layout changed?"

  # engram.yaml + .env now live in the DATA dir (~/.engram), not the source
  # clone. The source clone stays pure code so it can be wiped + re-cloned
  # without losing user config or data.
  [ -f "${ENGRAM_SRC}/engram.yaml.example" ] \
    || die "Missing engram.yaml.example in source — repo layout changed?"

  # Also defend against Docker auto-creating a DIRECTORY at the bind-mount
  # path from a previous failed 'compose up' (IsADirectoryError on next run).
  for stale in "${ENGRAM_SRC}/engram.yaml" "${DATA_DIR}/engram.yaml"; do
    if [ -d "$stale" ]; then
      warn "$stale is a directory (Docker auto-created from a failed previous run) — removing"
      rm -rf "$stale"
    fi
  done

  # Verify other bind-mount sources in the source clone are the right type
  for d in agents skills packages docker; do
    [ -d "${ENGRAM_SRC}/${d}" ] || die "Missing required directory: ${ENGRAM_SRC}/${d}"
  done
}

# ─── Refresh engram.yaml in the DATA dir from the committed example ──────────
# Called after create_dirs so DATA_DIR exists. Always pulls the latest template
# so upstream fixes (e.g. ARCADEDB_HOST interpolation) land on re-install.
refresh_yaml_config() {
  step "Writing engram.yaml to ${DATA_DIR}/engram.yaml"
  local YAML_FILE="${DATA_DIR}/engram.yaml"
  if [ -f "${YAML_FILE}" ] && ! cmp -s "${YAML_FILE}" "${ENGRAM_SRC}/engram.yaml.example"; then
    local yaml_backup="${YAML_FILE}.before-install-$(date +%Y%m%d-%H%M%S)"
    cp "${YAML_FILE}" "$yaml_backup"
    info "Backed up existing engram.yaml → $yaml_backup"
  fi
  cp "${ENGRAM_SRC}/engram.yaml.example" "${YAML_FILE}"

  # Patch open_mode based on deployment mode (idempotent — works regardless of
  # what the example file currently defaults to):
  #   server-only → false (auth enforced — safe for shared / remote / VM)
  #   full        → true  (auth bypassed — convenient for single-user local)
  if [ "${DEPLOY_MODE}" = "server-only" ]; then
    sed_i "s|^  open_mode: true|  open_mode: false|" "${YAML_FILE}"
    success "engram.yaml refreshed (mode=server-only, open_mode=false — Bearer auth ENFORCED)"
  else
    sed_i "s|^  open_mode: false|  open_mode: true|" "${YAML_FILE}"
    success "engram.yaml refreshed (mode=full, open_mode=true — auth bypassed for single-user local use)"
  fi

  # Clean up old in-source copies if they exist (migration from pre-v1.4 layout)
  if [ -f "${ENGRAM_SRC}/engram.yaml" ] && [ ! -L "${ENGRAM_SRC}/engram.yaml" ]; then
    info "Removing obsolete ${ENGRAM_SRC}/engram.yaml (now lives in ${DATA_DIR})"
    rm -f "${ENGRAM_SRC}/engram.yaml"
  fi
}

# ─── Create persistent data directories ──────────────────────────────────────
create_dirs() {
  step "Creating data directory at ${DATA_DIR}"
  mkdir -p "${DATA_DIR}/arcadedb" \
           "${DATA_DIR}/arcadedb-logs" \
           "${DATA_DIR}/arcadedb-backups" \
           "${DATA_DIR}/qdrant"
  chmod 700 "${DATA_DIR}"
  success "Directories ready"
}

# ─── Write .env to the DATA dir, patched with user input ─────────────────────
# .env lives in the data dir so wiping the source clone never loses secrets.
write_env() {
  local ENV_FILE="${DATA_DIR}/.env"
  step "Writing .env to ${ENV_FILE}"

  # Migrate from pre-v1.4 location if needed
  if [ -f "${ENGRAM_SRC}/.env" ] && [ ! -f "${ENV_FILE}" ]; then
    info "Migrating .env from ${ENGRAM_SRC}/.env to ${ENV_FILE}"
    mv "${ENGRAM_SRC}/.env" "${ENV_FILE}"
  fi

  if [ -f "${ENGRAM_SRC}/.env.example" ]; then
    cp "${ENGRAM_SRC}/.env.example" "${ENV_FILE}"
  else
    : > "${ENV_FILE}"
  fi

  # Overwrite required values
  sed_i "s|^ARCADEDB_PASSWORD=.*|ARCADEDB_PASSWORD=${ARCADEDB_PASSWORD}|"     "${ENV_FILE}"
  sed_i "s|^ENGRAM_API_KEY=.*|ENGRAM_API_KEY=${ENGRAM_API_KEY}|"              "${ENV_FILE}"
  sed_i "s|^ENGRAM_VAULT_KEY=.*|ENGRAM_VAULT_KEY=${ENGRAM_VAULT_KEY}|"        "${ENV_FILE}"
  sed_i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}|"     "${ENV_FILE}"
  sed_i "s|^# OPENAI_API_KEY=.*|OPENAI_API_KEY=${OPENAI_API_KEY}|"            "${ENV_FILE}"
  sed_i "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=${OPENAI_API_KEY}|"              "${ENV_FILE}"
  sed_i "s|^ENGRAM_EMBED_MODE=.*|ENGRAM_EMBED_MODE=${ENGRAM_EMBED_MODE:-online}|" "${ENV_FILE}"

  # Append values not in .env.example — compose reads these via --env-file.
  # ENGRAM_CONFIG_FILE is the absolute path to engram.yaml; compose substitutes
  # it into the volume mount: "${ENGRAM_CONFIG_FILE}:/app/engram.yaml:ro".
  {
    echo ""
    echo "# Set by install-server.sh"
    echo "ENGRAM_DATA_DIR=${DATA_DIR}"
    echo "ENGRAM_CONFIG_FILE=${DATA_DIR}/engram.yaml"
    if [ "${USE_QDRANT}" = "yes" ]; then
      echo "ENGRAM_VECTOR_BACKEND=qdrant"
    fi
  } >> "${ENV_FILE}"

  chmod 600 "${ENV_FILE}"
  success ".env written (mode 600)"

  # Remove obsolete in-source .env if it still exists (post-migration cleanup)
  if [ -f "${ENGRAM_SRC}/.env" ]; then
    info "Removing obsolete ${ENGRAM_SRC}/.env (now lives in ${DATA_DIR})"
    rm -f "${ENGRAM_SRC}/.env"
  fi
}

# ─── Tear down stale containers from a previous compose project ─────────────
# If engram-arcadedb / engram / engram-qdrant exist but were started from a
# different compose project (different directory, different network), they
# cannot talk to a fresh engram container we start from ENGRAM_SRC — they
# end up on different docker networks → 'httpx.ConnectError: All connection
# attempts failed' inside engram. Remove them first; data persists in the
# bind-mounted DATA_DIR.
clean_stale_containers() {
  local expected_net
  expected_net="$(basename "${ENGRAM_SRC}")_default"
  local stale=()
  for c in engram engram-arcadedb engram-qdrant; do
    docker inspect "$c" >/dev/null 2>&1 || continue
    local nets
    nets="$(docker inspect "$c" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)"
    # Stale if the container is NOT on the expected ENGRAM_SRC network
    if ! echo "$nets" | grep -qw "$expected_net"; then
      stale+=("$c")
    fi
  done
  if [ ${#stale[@]} -gt 0 ]; then
    step "Removing stale containers from a previous compose project"
    info "These were started from a different directory and are on a different docker network."
    info "Data on disk in ${DATA_DIR} is NOT touched — only the container shells are removed."
    for c in "${stale[@]}"; do
      info "  removing: $c"
      docker rm -f "$c" >/dev/null 2>&1 || true
    done
    # Also tear down the old compose project if it lived at ~/.engram
    if [ -f "${HOME}/.engram/docker-compose.yml" ]; then
      info "  found old ~/.engram/docker-compose.yml — tearing down its project"
      ( cd "${HOME}/.engram" && $DC down 2>/dev/null || true )
      mv "${HOME}/.engram/docker-compose.yml" "${HOME}/.engram/docker-compose.yml.OBSOLETE.$(date +%s)" 2>/dev/null || true
    fi
  fi
}

# ─── Pull images, build engram, start services ───────────────────────────────
# Compose runs from the source clone (for build context + relative agents/skills
# mounts) but reads .env from the data dir via --env-file.
start_services() {
  cd "${ENGRAM_SRC}"
  local ENV_FILE="${DATA_DIR}/.env"
  set -a; source "${ENV_FILE}"; set +a
  clean_stale_containers

  # Compose command with explicit --env-file pointing at the data-dir .env.
  # Add --profile qdrant when the user opted in.
  local DC_CMD="${DC} --env-file ${ENV_FILE}"
  if [ "${USE_QDRANT}" = "yes" ]; then
    DC_CMD="${DC_CMD} --profile qdrant"
  fi

  step "Pulling ArcadeDB image"
  info "First pull downloads ~250 MB — may take a minute."
  $DC_CMD pull arcadedb

  if [ "${USE_QDRANT}" = "yes" ]; then
    step "Pulling Qdrant image"
    $DC_CMD pull qdrant
  fi

  step "Building engram image"
  info "First build downloads Python dependencies — typically 3-5 minutes."
  info "You will see progress below. Do not interrupt."
  echo ""
  $DC_CMD build --progress=plain engram

  step "Starting services"
  $DC_CMD up -d

  echo ""
  info "Waiting for services to be healthy..."
  local i=0
  while ! $DC_CMD ps 2>/dev/null | grep -q "engram.*healthy"; do
    sleep 4; i=$((i+1)); echo -ne "\r  Waiting... ${i}s"
    [ $i -ge 45 ] && break
  done
  echo ""

  sleep 2
  if curl -sf "http://localhost:8766/api/v1/admin/health" \
    -H "Authorization: Bearer ${ENGRAM_API_KEY}" -o /dev/null 2>/dev/null; then
    success "engram API is healthy"
  else
    warn "API not responding yet — check logs: cd ${ENGRAM_SRC} && ${DC} --env-file ${ENV_FILE} logs engram"
  fi
}

# ─── Success message ──────────────────────────────────────────────────────────
print_success() {
  echo ""
  echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}${GREEN}  engram server installed!${NC}"
  echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""
  echo -e "  ${BOLD}Connection details${NC} (share with clients running install-client.sh):"
  echo ""

  local SERVER_HOST; SERVER_HOST="$(hostname -f 2>/dev/null || hostname)"
  if [ "$OS" = "wsl" ]; then
    SERVER_HOST="localhost"
  fi

  echo -e "    MCP / SSE endpoint : ${BOLD}http://${SERVER_HOST}:8765/sse${NC}"
  echo -e "    REST API           : ${BOLD}http://${SERVER_HOST}:8766/api/v1${NC}"
  echo -e "    API key            : ${YELLOW}${ENGRAM_API_KEY}${NC}"
  echo ""
  echo -e "  ${BOLD}Source directory${NC} : ${ENGRAM_SRC}  ${DIM}(code only — safe to wipe + re-clone)${NC}"
  echo -e "  ${BOLD}Data directory${NC}   : ${DATA_DIR}  ${DIM}(config + secrets + data — keep this)${NC}"
  echo -e "    ${DIM}├─ engram.yaml      configuration${NC}"
  echo -e "    ${DIM}├─ .env             API keys + passwords${NC}"
  echo -e "    ${DIM}├─ arcadedb/        graph + vector data${NC}"
  if [ "${USE_QDRANT}" = "yes" ]; then
    echo -e "    ${DIM}└─ qdrant/          HNSW ANN index${NC}"
  fi
  echo ""
  local PROFILE=""
  [ "${USE_QDRANT}" = "yes" ] && PROFILE=" --profile qdrant"
  local DC_FULL="${DC} --env-file ${DATA_DIR}/.env${PROFILE}"
  echo -e "  ${BOLD}Manage services${NC}:"
  echo -e "    cd ${ENGRAM_SRC} && ${DC_FULL} logs -f engram  # tail logs"
  echo -e "    cd ${ENGRAM_SRC} && ${DC_FULL} down             # stop"
  echo -e "    cd ${ENGRAM_SRC} && ${DC_FULL} up -d            # start"
  echo ""
  echo -e "  ${BOLD}Next step${NC} — install the client hooks on each developer machine:"
  echo -e "    ${CYAN}./install-client.sh --server http://${SERVER_HOST}:8766 --key ${ENGRAM_API_KEY}${NC}"
  echo ""
}

main() {
  detect_os
  check_docker
  check_python
  detect_existing_install

  if [ "$INSTALL_MODE" = "upgrade" ]; then
    # ── Upgrade: non-destructive. Reuse .env, preserve existing engram.yaml
    #             (including open_mode), only update source code + rebuild image.
    DATA_DIR="$HOME/.engram"
    ENGRAM_SRC="$HOME/.engram-src"
    # Pre-v1.4 .env lived at ENGRAM_SRC/.env — migrate transparently.
    if [ ! -f "${DATA_DIR}/.env" ] && [ -f "${ENGRAM_SRC}/.env" ]; then
      mkdir -p "${DATA_DIR}"
      mv "${ENGRAM_SRC}/.env" "${DATA_DIR}/.env"
      info "Migrated .env from ${ENGRAM_SRC} → ${DATA_DIR}"
    fi
    local ENV_FILE="${DATA_DIR}/.env"
    [ -f "${ENV_FILE}" ] || die "Upgrade mode but ${ENV_FILE} is missing — pick 'Fresh install' instead."
    set -a; source "${ENV_FILE}"; set +a
    USE_QDRANT="no"
    grep -q "^ENGRAM_VECTOR_BACKEND=qdrant" "${ENV_FILE}" && USE_QDRANT="yes"

    # Auto-detect the existing open_mode so we don't accidentally flip it
    # when refresh_yaml_config runs. User can override with --mode.
    if [ "${DEPLOY_MODE_EXPLICIT}" -eq 0 ] && [ -f "${DATA_DIR}/engram.yaml" ]; then
      if grep -qE "^\s+open_mode:\s+true\b" "${DATA_DIR}/engram.yaml"; then
        DEPLOY_MODE="full"
      else
        DEPLOY_MODE="server-only"
      fi
      info "Upgrade: detected existing deploy mode = ${BOLD}${DEPLOY_MODE}${NC} (pass --mode to override)"
    fi

    info "Upgrade: preserving ${ENV_FILE} — no re-prompts, no key changes."
    resolve_source       # git pull source
    refresh_yaml_config  # refresh from updated template, preserve open_mode
    # Ensure ENGRAM_CONFIG_FILE is set in .env (pre-v1.4 installs didn't have it)
    if ! grep -q "^ENGRAM_CONFIG_FILE=" "${ENV_FILE}"; then
      echo "ENGRAM_CONFIG_FILE=${DATA_DIR}/engram.yaml" >> "${ENV_FILE}"
    fi
    start_services       # rebuild image, restart containers
  else
    # ── Fresh: detect_existing_install already ran confirm_fresh_wipe_or_abort
    #          (which deleted ~/.engram/{arcadedb,qdrant,.env,engram.yaml}
    #          when there was anything to lose). Now run a clean configure.
    collect_config
    resolve_source
    create_dirs
    refresh_yaml_config
    write_env
    start_services
  fi

  print_success
  echo ""
  echo -e "  ${DIM}Install log saved to ${BOLD}${LOG_FILE}${NC}"
}

main "$@"
