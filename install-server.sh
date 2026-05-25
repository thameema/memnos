#!/usr/bin/env bash
# engram server installer
#
# Installs the engram server (ArcadeDB + engram API) using Docker.
# Run this on the machine that will HOST engram — could be a laptop,
# a VM, or a remote server.
#
# Usage:
#   ./install-server.sh
#   curl -fsSL https://raw.githubusercontent.com/thameema/engram/main/install-server.sh | bash

set -euo pipefail

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
gen_key() { python3 -c "import secrets; print('engram-' + secrets.token_hex(16))" 2>/dev/null || openssl rand -hex 20; }
gen_pass() { python3 -c "import secrets,string; print(secrets.token_urlsafe(18)[:20])" 2>/dev/null || openssl rand -base64 16 | tr -dc 'A-Za-z0-9' | head -c 20; }

# ─── Banner ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${BLUE}"
cat <<'BANNER'
   ___  ____   ___  ____   ____  __  __
  / __)(  _ \ / __)(  _ \ / _  \(  \/  )
 ( (__  )   /( (__  )   // /_\ / )    /
  \___)(_)\_) \___)(____/ \___/ (_/\/\_)
  Server Installer
BANNER
echo -e "${NC}"

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

  # Data directory
  ask DATA_DIR "Data directory (stores ArcadeDB data, config, logs)" "$HOME/.engram"
  DATA_DIR="${DATA_DIR/#\~/$HOME}"

  # Ports
  ask MCP_PORT  "MCP / SSE port" "8765"
  ask API_PORT  "REST API port"  "8766"
  ask DB_PORT   "ArcadeDB HTTP port (set 0 to not expose)" "2480"

  # API key
  local default_key; default_key="$(gen_key)"
  ask ENGRAM_API_KEY "engram API key (blank = auto-generate)" ""
  [ -z "$ENGRAM_API_KEY" ] && ENGRAM_API_KEY="$default_key" && info "Generated API key: ${BOLD}${ENGRAM_API_KEY}${NC}"

  # ArcadeDB password
  local default_pw; default_pw="$(gen_pass)"
  ask ARCADEDB_PASSWORD "ArcadeDB root password (blank = auto-generate)" ""
  [ -z "$ARCADEDB_PASSWORD" ] && ARCADEDB_PASSWORD="$default_pw" && info "Generated ArcadeDB password: ${BOLD}${ARCADEDB_PASSWORD}${NC}"

  # LLM provider
  echo ""
  echo -e "  ${BOLD}LLM provider${NC} (used for reflection and skill extraction)"
  echo "  1) Anthropic — claude-sonnet-4-6 (recommended)"
  echo "  2) OpenAI — gpt-4o"
  echo "  3) Skip (vector/graph memory only)"
  ask LLM_CHOICE "Choose [1/2/3]" "1"

  case "${LLM_CHOICE}" in
    2) LLM_PROVIDER="openai";    ask OPENAI_API_KEY  "OpenAI API key"    ""; ANTHROPIC_API_KEY="" ;;
    3) LLM_PROVIDER="none";      OPENAI_API_KEY="";  ANTHROPIC_API_KEY="" ;;
    *) LLM_PROVIDER="anthropic"; ask ANTHROPIC_API_KEY "Anthropic API key" ""; OPENAI_API_KEY="" ;;
  esac
}

# ─── Create directory structure ───────────────────────────────────────────────
create_dirs() {
  step "Creating directories at ${DATA_DIR}"
  mkdir -p "${DATA_DIR}/data/arcadedb" \
           "${DATA_DIR}/logs"
  chmod 700 "${DATA_DIR}"
  success "Directories ready"
}

# ─── Write docker-compose.yml ─────────────────────────────────────────────────
write_compose() {
  step "Writing docker-compose.yml"

  local DB_PORTS_BLOCK=""
  if [ "${DB_PORT}" != "0" ]; then
    DB_PORTS_BLOCK="    ports:
      - \"${DB_PORT}:2480\""
  fi

  # Detect whether we are inside the source repo (dev install) or standalone
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-./install-server.sh}")" 2>/dev/null && pwd || echo "")"
  local ENGRAM_SERVICE=""
  if [ -f "${SCRIPT_DIR}/packages/core/pyproject.toml" ]; then
    ENGRAM_SERVICE="  engram:
    build: ${SCRIPT_DIR}
    depends_on:
      arcadedb:
        condition: service_healthy"
  else
    # Use published image — adjust tag as releases are made
    ENGRAM_SERVICE="  engram:
    image: ghcr.io/thameema/engram:latest
    depends_on:
      arcadedb:
        condition: service_healthy"
  fi

  cat > "${DATA_DIR}/docker-compose.yml" <<COMPOSE
# engram docker-compose — generated by install-server.sh
# Edit data directory mounts if you relocate ${DATA_DIR}

services:
  arcadedb:
    image: arcadedata/arcadedb:26.5.1
    container_name: engram-arcadedb
    environment:
      ARCADEDB_SERVER_ROOTPASSWORD: \${ARCADEDB_PASSWORD}
      ARCADEDB_SERVER_PLUGINS: >-
        com.arcadedb.postgres.PostgreSQLProtocolPlugin,
        com.arcadedb.redis.RedisProtocolPlugin,
        com.arcadedb.graphql.GraphQLProtocolPlugin
      JAVA_OPTS: -Xms512m -Xmx2g
${DB_PORTS_BLOCK}
    volumes:
      - ${DATA_DIR}/data/arcadedb:/home/arcadedb/databases
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:2480/api/v1/ready"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 30s
    restart: unless-stopped

${ENGRAM_SERVICE}
    container_name: engram
    environment:
      ENGRAM_API_KEY: \${ENGRAM_API_KEY}
      ARCADEDB_HOST: arcadedb
      ARCADEDB_PORT: 2480
      ARCADEDB_USER: root
      ARCADEDB_PASSWORD: \${ARCADEDB_PASSWORD}
      ANTHROPIC_API_KEY: \${ANTHROPIC_API_KEY}
      OPENAI_API_KEY: \${OPENAI_API_KEY}
    ports:
      - "${MCP_PORT}:8765"
      - "${API_PORT}:8766"
    volumes:
      - ${DATA_DIR}/logs:/app/logs
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8766/api/v1/admin/health"]
      interval: 15s
      timeout: 5s
      retries: 8
      start_period: 30s
    restart: unless-stopped
COMPOSE

  success "docker-compose.yml → ${DATA_DIR}/docker-compose.yml"
}

# ─── Write .env ───────────────────────────────────────────────────────────────
write_env() {
  step "Writing .env"
  cat > "${DATA_DIR}/.env" <<ENV
# engram environment — generated by install-server.sh on $(date)
# DO NOT commit this file.

ENGRAM_API_KEY=${ENGRAM_API_KEY}
ARCADEDB_PASSWORD=${ARCADEDB_PASSWORD}

ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
OPENAI_API_KEY=${OPENAI_API_KEY}
ENV
  chmod 600 "${DATA_DIR}/.env"
  success ".env → ${DATA_DIR}/.env  (mode 600)"
}

# ─── Pull images and start ────────────────────────────────────────────────────
start_services() {
  step "Pulling images and starting services"
  cd "${DATA_DIR}"
  set -a; source .env; set +a
  $DC pull arcadedb 2>&1 | tail -1
  $DC up -d --build 2>&1 | tail -5
  echo ""
  info "Waiting for services to be healthy..."
  local i=0
  while ! $DC ps 2>/dev/null | grep -q "healthy"; do
    sleep 4; i=$((i+1)); echo -ne "\r  Waiting... ${i}s"
    [ $i -ge 30 ] && break
  done
  echo ""

  # Quick health check
  sleep 2
  if curl -sf "http://localhost:${API_PORT}/api/v1/admin/health" \
    -H "X-API-Key: ${ENGRAM_API_KEY}" -o /dev/null 2>/dev/null; then
    success "engram API is healthy"
  else
    warn "API not responding yet — check logs: $DC logs engram"
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

  echo -e "    MCP / SSE endpoint : ${BOLD}http://${SERVER_HOST}:${MCP_PORT}/sse${NC}"
  echo -e "    REST API            : ${BOLD}http://${SERVER_HOST}:${API_PORT}/api/v1${NC}"
  echo -e "    API key             : ${YELLOW}${ENGRAM_API_KEY}${NC}"
  echo ""
  echo -e "  ${BOLD}Data directory${NC} : ${DATA_DIR}"
  echo -e "  ${BOLD}Manage services${NC}:"
  echo -e "    cd ${DATA_DIR} && ${DC} logs -f engram  # tail logs"
  echo -e "    cd ${DATA_DIR} && ${DC} down            # stop"
  echo -e "    cd ${DATA_DIR} && ${DC} up -d           # start"
  echo ""
  echo -e "  ${BOLD}Next step${NC} — install the client hooks on each developer machine:"
  echo -e "    ${CYAN}./install-client.sh --server http://${SERVER_HOST}:${API_PORT} --key ${ENGRAM_API_KEY}${NC}"
  echo ""
}

main() {
  detect_os
  check_docker
  check_python
  collect_config
  create_dirs
  write_compose
  write_env
  start_services
  print_success
}

main "$@"
