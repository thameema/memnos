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
  read -r input </dev/tty
  eval "$varname='${input:-$default}'"
}
ask_yn() {
  local varname="$1" prompt="$2" default="${3:-Y}"
  echo -ne "${CYAN}  ?${NC} ${prompt} ${DIM}[${default}]${NC}: "
  read -r input </dev/tty
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
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) ENGRAM_REF_ARG="$2"; shift 2 ;;
    --help|-h)
      cat <<HLP
  Usage: install-server.sh [--version <ref>]

    --version <ref>   Pin to a specific git ref. Examples:
                        --version v1.2.0     install release v1.2.0 (stable)
                        --version master     install bleeding-edge master
                        --version <sha>      install a specific commit

                      Default: latest published GitHub Release (queried at
                      install time). Override with ENGRAM_REF env var.
HLP
      exit 0 ;;
    *) shift ;;
  esac
done

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
  echo -e "  ${BOLD}1) Upgrade${NC}     — git pull source, rebuild image, restart. Keeps data + .env."
  echo -e "  ${BOLD}2) Fresh install${NC} — wipe .env (NOT data dir), reconfigure from scratch."
  echo -e "  ${BOLD}3) Abort${NC}       — leave everything as-is."
  echo ""
  ask CHOICE "Choose [1/2/3]" "1"
  case "$CHOICE" in
    1) INSTALL_MODE="upgrade"; info "Mode: upgrade (preserve config and data)" ;;
    2) INSTALL_MODE="fresh";   info "Mode: fresh install (data dir preserved, .env rewritten)" ;;
    *) die "Aborted by user. Existing install left untouched." ;;
  esac
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

  # ─── Optional API keys ────────────────────────────────────────────────────
  echo ""
  echo -e "  ${BOLD}Optional API keys${NC}  ${DIM}(both can be skipped — engram works without them)${NC}"
  echo ""
  echo -e "  ${BOLD}1. Anthropic API key${NC}  — for LLM reflection & skill extraction."
  echo -e "     ${DIM}If skipped:${NC} engram uses Claude Code built-in ${BOLD}claude --print${NC} CLI."
  echo -e "     ${DIM}Recommended:${NC} skip unless you do not have Claude Code installed."
  echo ""
  echo -e "  ${BOLD}2. OpenAI API key${NC}     — for high-quality embeddings (text-embedding-3-small)."
  echo -e "     ${DIM}If skipped:${NC} you must choose a local embedding backend below."
  echo ""
  echo -e "  ${DIM}You can edit ${BOLD}~/.engram/.env${NC}${DIM} later to add or change keys.${NC}"
  echo ""

  ask ANTHROPIC_API_KEY "Anthropic API key (press Enter to skip)" ""
  ask OPENAI_API_KEY    "OpenAI API key (press Enter to skip)"    ""

  # If user skipped OpenAI, they MUST opt in to local embeddings (or search will
  # be permanently broken). Local embeddings add ~2 GB to the Docker image.
  ENGRAM_EMBED_MODE="online"
  if [ -z "${OPENAI_API_KEY}" ]; then
    echo ""
    echo -e "  ${BOLD}Embeddings backend (required — engram needs one)${NC}"
    echo -e "  You skipped the OpenAI key, so engram needs a local model to do semantic search."
    echo ""
    echo -e "  ${BOLD}Local embeddings:${NC} sentence-transformers + torch, baked into the engram image."
    echo -e "     ${YELLOW}Disk cost:${NC}  ${BOLD}~2 GB added to the Docker image${NC} (one-time download during build)."
    echo -e "     ${DIM}Runtime:${NC}    runs in-container, no API cost, fully offline."
    echo -e "     ${DIM}Quality:${NC}    decent (all-MiniLM-L6-v2, 384-dim). Less accurate than OpenAI."
    echo -e "     ${DIM}Build time:${NC} adds ~3-5 minutes to the first 'docker compose build'."
    echo ""
    ask_yn USE_LOCAL_EMBED "Install local embeddings now? (recommended when no OpenAI key)" "Y"
    if [ "${USE_LOCAL_EMBED}" = "yes" ]; then
      ENGRAM_EMBED_MODE="local"
    else
      warn "No embedding backend selected — semantic search will return errors until you"
      warn "either add OPENAI_API_KEY to ~/.engram/.env or rebuild with ENGRAM_EMBED_MODE=local."
    fi
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
resolve_ref() {
  # Priority: --version arg  >  ENGRAM_REF env  >  latest GitHub Release  >  master
  if [ -n "${ENGRAM_REF_ARG}" ]; then
    ENGRAM_REF="${ENGRAM_REF_ARG}"
    info "Pinning to ref from --version: ${BOLD}${ENGRAM_REF}${NC}"
    return
  fi
  if [ -n "${ENGRAM_REF:-}" ]; then
    info "Pinning to ref from ENGRAM_REF env: ${BOLD}${ENGRAM_REF}${NC}"
    return
  fi
  info "Looking up latest engram release on GitHub..."
  local tag
  tag="$(curl -fsSL --max-time 8 https://api.github.com/repos/thameema/engram/releases/latest 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('tag_name',''))" 2>/dev/null || true)"
  if [ -n "${tag}" ]; then
    ENGRAM_REF="${tag}"
    info "Latest release: ${BOLD}${ENGRAM_REF}${NC} (override with --version master for bleeding-edge)"
  else
    ENGRAM_REF="master"
    warn "Could not query GitHub Releases API — falling back to ${BOLD}master${NC} branch."
  fi
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
  success "engram.yaml refreshed from engram.yaml.example"

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
    # Reuse existing values; do not re-prompt.
    DATA_DIR="$HOME/.engram"
    ENGRAM_SRC="$HOME/.engram-src"
    # Pre-v1.4 .env lived at ENGRAM_SRC/.env — migrate transparently.
    if [ ! -f "${DATA_DIR}/.env" ] && [ -f "${ENGRAM_SRC}/.env" ]; then
      mkdir -p "${DATA_DIR}"
      mv "${ENGRAM_SRC}/.env" "${DATA_DIR}/.env"
      info "Migrated .env from ${ENGRAM_SRC} → ${DATA_DIR}"
    fi
    local ENV_FILE="${DATA_DIR}/.env"
    [ -f "${ENV_FILE}" ] || die "Upgrade mode but ${ENV_FILE} is missing — switch to fresh install."
    set -a; source "${ENV_FILE}"; set +a
    USE_QDRANT="no"
    grep -q "^ENGRAM_VECTOR_BACKEND=qdrant" "${ENV_FILE}" && USE_QDRANT="yes"
    info "Upgrade: preserving ${ENV_FILE} (keeps your existing keys)."
    resolve_source       # git pull
    refresh_yaml_config  # ensure DATA_DIR/engram.yaml exists and is current
    # Ensure ENGRAM_CONFIG_FILE is set in .env (pre-v1.4 installs didn't have it)
    if ! grep -q "^ENGRAM_CONFIG_FILE=" "${ENV_FILE}"; then
      echo "ENGRAM_CONFIG_FILE=${DATA_DIR}/engram.yaml" >> "${ENV_FILE}"
    fi
    start_services       # rebuild + restart
  else
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
