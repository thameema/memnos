#!/usr/bin/env bash
# engram installer
# Usage: curl -fsSL https://raw.githubusercontent.com/yourusername/engram/main/install.sh | bash
set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ─── Helpers ──────────────────────────────────────────────────────────────────
info()    { echo -e "${CYAN}  -->${NC} $*"; }
success() { echo -e "${GREEN}  [ok]${NC} $*"; }
warn()    { echo -e "${YELLOW}  [!]${NC} $*"; }
error()   { echo -e "${RED}  [error]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }
bold()    { echo -e "${BOLD}$*${NC}"; }
dim()     { echo -e "${DIM}$*${NC}"; }

header() {
  echo ""
  echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}${BLUE}  $*${NC}"
  echo -e "${BOLD}${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""
}

step() {
  echo ""
  echo -e "${BOLD}>>> $*${NC}"
}

ask() {
  # ask VARNAME "Prompt text" "default"
  local varname="$1"
  local prompt="$2"
  local default="${3:-}"

  if [ -n "$default" ]; then
    echo -ne "${CYAN}  ?${NC} ${prompt} ${DIM}[${default}]${NC}: "
  else
    echo -ne "${CYAN}  ?${NC} ${prompt}: "
  fi

  read -r input </dev/tty
  if [ -z "$input" ] && [ -n "$default" ]; then
    eval "$varname='$default'"
  else
    eval "$varname='$input'"
  fi
}

ask_yn() {
  # ask_yn VARNAME "Prompt" "Y|N"
  local varname="$1"
  local prompt="$2"
  local default="${3:-Y}"
  echo -ne "${CYAN}  ?${NC} ${prompt} ${DIM}[${default}]${NC}: "
  read -r input </dev/tty
  input="${input:-$default}"
  if [[ "$input" =~ ^[Yy] ]]; then
    eval "$varname=yes"
  else
    eval "$varname=no"
  fi
}

# ─── Random generators ────────────────────────────────────────────────────────
gen_password() {
  # 20-char alphanumeric password safe for YAML/env values
  if command -v openssl &>/dev/null; then
    openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20
  else
    cat /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 20 2>/dev/null || \
      python3 -c "import secrets,string; print(secrets.token_urlsafe(15)[:20])"
  fi
}

gen_api_key() {
  if command -v openssl &>/dev/null; then
    echo "engram-$(openssl rand -hex 16)"
  else
    echo "engram-$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  fi
}

# ─── Banner ───────────────────────────────────────────────────────────────────
clear_screen_if_tty() { [ -t 1 ] && clear || true; }
clear_screen_if_tty

echo ""
echo -e "${BOLD}${BLUE}"
cat <<'BANNER'
   ___  ____   ___  ____   ____  __  __
  / __)(  _ \ / __)(  _ \ / _  \(  \/  )
 ( (__  )   /( (__  )   // /_\ / )    /
  \___)(_)\_) \___)(____/ \___/ (_/\/\_)

  Persistent memory + multi-agent orchestration for LLM workflows
BANNER
echo -e "${NC}"
echo -e "  ${DIM}https://github.com/yourusername/engram${NC}"
echo ""

# ─── OS detection ─────────────────────────────────────────────────────────────
detect_os() {
  OS="unknown"
  ARCH="$(uname -m)"
  case "$(uname -s)" in
    Darwin) OS="macos";;
    Linux)  OS="linux";;
    *)      die "Unsupported OS: $(uname -s). engram supports macOS and Linux.";;
  esac
  info "Detected OS: ${OS} (${ARCH})"
}

# ─── Prerequisite checks ──────────────────────────────────────────────────────
check_cmd() {
  local cmd="$1"
  command -v "$cmd" &>/dev/null
}

check_docker() {
  step "Checking Docker"
  if ! check_cmd docker; then
    error "Docker is not installed."
    echo ""
    if [ "$OS" = "macos" ]; then
      echo "  Install Docker Desktop for Mac:"
      echo "    https://www.docker.com/products/docker-desktop/"
      echo "  Or via Homebrew: brew install --cask docker"
    else
      echo "  Install Docker on Linux:"
      echo "    curl -fsSL https://get.docker.com | bash"
      echo "  Then add your user to the docker group:"
      echo "    sudo usermod -aG docker \$USER && newgrp docker"
    fi
    echo ""
    die "Please install Docker and re-run this installer."
  fi
  success "Docker found: $(docker --version 2>&1 | head -1)"

  # Check Docker is running
  if ! docker info &>/dev/null 2>&1; then
    if [ "$OS" = "macos" ]; then
      warn "Docker daemon is not running. Attempting to start Docker Desktop..."
      open -a Docker 2>/dev/null || true
      echo -n "  Waiting for Docker to start"
      local attempts=0
      while ! docker info &>/dev/null 2>&1; do
        sleep 2
        echo -n "."
        attempts=$((attempts + 1))
        if [ $attempts -ge 30 ]; then
          echo ""
          die "Docker did not start within 60 seconds. Please start Docker Desktop manually."
        fi
      done
      echo ""
      success "Docker is running."
    else
      die "Docker daemon is not running. Start it with: sudo systemctl start docker"
    fi
  fi

  # Check Docker Compose (v2 plugin or standalone v1)
  if docker compose version &>/dev/null 2>&1; then
    DOCKER_COMPOSE="docker compose"
    success "Docker Compose (plugin): $(docker compose version 2>&1 | head -1)"
  elif check_cmd docker-compose; then
    DOCKER_COMPOSE="docker-compose"
    success "Docker Compose (standalone): $(docker-compose --version 2>&1 | head -1)"
  else
    error "Docker Compose is not installed."
    echo ""
    echo "  Install the Docker Compose plugin:"
    if [ "$OS" = "macos" ]; then
      echo "    Docker Desktop includes Compose — reinstall Docker Desktop."
    else
      echo "    sudo apt-get install docker-compose-plugin  # Debian/Ubuntu"
      echo "    sudo yum install docker-compose-plugin       # RHEL/CentOS"
    fi
    echo ""
    die "Please install Docker Compose and re-run this installer."
  fi
}

check_python() {
  step "Checking Python"
  PY_CMD=""
  for cmd in python3.12 python3.11 python3; do
    if check_cmd "$cmd"; then
      local ver
      ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
      local major minor
      major=$(echo "$ver" | cut -d. -f1)
      minor=$(echo "$ver" | cut -d. -f2)
      if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
        PY_CMD="$cmd"
        success "Python ${ver} found at: $(command -v $cmd)"
        return
      else
        warn "Found $cmd but version is ${ver} (need >= 3.11)"
      fi
    fi
  done

  error "Python 3.11 or newer is required."
  echo ""
  if [ "$OS" = "macos" ]; then
    echo "  Install via Homebrew:"
    echo "    brew install python@3.12"
  else
    echo "  Install on Ubuntu/Debian:"
    echo "    sudo apt-get install python3.12 python3.12-venv"
    echo "  Install on RHEL/CentOS:"
    echo "    sudo dnf install python3.12"
  fi
  echo ""
  die "Please install Python 3.11+ and re-run this installer."
}

check_pip() {
  step "Checking pip / uv"
  PIP_CMD=""
  # Prefer uv if available — it's much faster
  if check_cmd uv; then
    PIP_CMD="uv pip"
    success "uv found — will use uv for fast installs: $(uv --version 2>&1)"
    return
  fi
  # Try pip associated with the chosen Python
  if "$PY_CMD" -m pip --version &>/dev/null 2>&1; then
    PIP_CMD="$PY_CMD -m pip"
    success "pip found: $($PY_CMD -m pip --version 2>&1)"
    return
  fi
  # Fallback to standalone pip3 / pip
  for cmd in pip3 pip; do
    if check_cmd "$cmd"; then
      PIP_CMD="$cmd"
      success "pip found: $($cmd --version 2>&1)"
      return
    fi
  done
  die "Could not find pip. Run: $PY_CMD -m ensurepip --upgrade"
}

check_curl() {
  step "Checking curl"
  if ! check_cmd curl; then
    error "curl is required but not installed."
    if [ "$OS" = "macos" ]; then
      echo "  brew install curl"
    else
      echo "  sudo apt-get install curl"
    fi
    die "Please install curl and re-run this installer."
  fi
  success "curl found: $(curl --version | head -1)"
}

check_jq() {
  # Non-fatal — we fall back to Python for JSON manipulation
  JQ_CMD=""
  if check_cmd jq; then
    JQ_CMD="jq"
  fi
}

# ─── Configuration prompts ────────────────────────────────────────────────────
collect_config() {
  header "Configuration"

  # Install directory
  local default_dir="$HOME/.engram"
  ask ENGRAM_DIR "Install directory" "$default_dir"
  ENGRAM_DIR="${ENGRAM_DIR/#\~/$HOME}"  # expand ~ manually

  # Neo4j password
  local default_neo4j_pw
  default_neo4j_pw="$(gen_password)"
  ask NEO4J_PASSWORD "Neo4j password (leave blank to auto-generate)" ""
  if [ -z "$NEO4J_PASSWORD" ]; then
    NEO4J_PASSWORD="$default_neo4j_pw"
    info "Auto-generated Neo4j password: ${BOLD}${NEO4J_PASSWORD}${NC}"
  fi

  # engram API key
  local default_api_key
  default_api_key="$(gen_api_key)"
  ask ENGRAM_API_KEY "engram API key (leave blank to auto-generate)" ""
  if [ -z "$ENGRAM_API_KEY" ]; then
    ENGRAM_API_KEY="$default_api_key"
    info "Auto-generated API key: ${BOLD}${ENGRAM_API_KEY}${NC}"
  fi

  # LLM provider
  echo ""
  bold "LLM Provider"
  echo "  engram needs an LLM for reasoning and reflection."
  echo "  1) Anthropic (recommended) — claude-sonnet-4-6"
  echo "  2) OpenAI — gpt-4o"
  echo "  3) Skip — vector/graph memory only (no LLM reasoning)"
  echo ""
  ask LLM_PROVIDER_CHOICE "Choose LLM provider [1/2/3]" "1"

  case "$LLM_PROVIDER_CHOICE" in
    1)
      LLM_PROVIDER="anthropic"
      ask ANTHROPIC_API_KEY "Anthropic API key (sk-ant-...)" ""
      OPENAI_API_KEY=""
      ;;
    2)
      LLM_PROVIDER="openai"
      ask OPENAI_API_KEY "OpenAI API key (sk-...)" ""
      ANTHROPIC_API_KEY=""
      ;;
    3)
      LLM_PROVIDER="none"
      ANTHROPIC_API_KEY=""
      OPENAI_API_KEY=""
      warn "Vector-only mode: memory_search and memory_write will work, but reflection/reasoning will be disabled."
      ;;
    *)
      LLM_PROVIDER="anthropic"
      ask ANTHROPIC_API_KEY "Anthropic API key (sk-ant-...)" ""
      OPENAI_API_KEY=""
      ;;
  esac

  # Embeddings key (for OpenAI embeddings — used even in Anthropic mode)
  if [ "$LLM_PROVIDER" = "anthropic" ] && [ -z "$OPENAI_API_KEY" ]; then
    echo ""
    warn "Embeddings note: By default engram uses OpenAI text-embedding-3-small."
    echo "  You can skip this and use local embeddings (slower but free)."
    ask_yn USE_LOCAL_EMBEDDINGS "Use local (free) embeddings instead of OpenAI?" "N"
    if [ "$USE_LOCAL_EMBEDDINGS" = "no" ]; then
      ask OPENAI_API_KEY "OpenAI API key for embeddings (sk-...)" ""
    else
      OPENAI_API_KEY=""
      EMBEDDINGS_PROVIDER="local"
    fi
  else
    USE_LOCAL_EMBEDDINGS="no"
    EMBEDDINGS_PROVIDER="openai"
  fi

  [ "$USE_LOCAL_EMBEDDINGS" = "yes" ] && EMBEDDINGS_PROVIDER="local" || EMBEDDINGS_PROVIDER="openai"
}

# ─── Directory structure ──────────────────────────────────────────────────────
create_directories() {
  step "Creating directory structure at ${ENGRAM_DIR}"

  mkdir -p \
    "${ENGRAM_DIR}/data/neo4j" \
    "${ENGRAM_DIR}/data/qdrant" \
    "${ENGRAM_DIR}/agents" \
    "${ENGRAM_DIR}/skills" \
    "${ENGRAM_DIR}/logs"

  success "Directories created:"
  dim "  ${ENGRAM_DIR}/"
  dim "  ${ENGRAM_DIR}/data/neo4j    (Neo4j persistence)"
  dim "  ${ENGRAM_DIR}/data/qdrant   (Qdrant persistence)"
  dim "  ${ENGRAM_DIR}/agents        (your custom agents)"
  dim "  ${ENGRAM_DIR}/skills        (your custom skills)"
  dim "  ${ENGRAM_DIR}/logs          (server logs)"
}

# ─── engram.yaml generation ───────────────────────────────────────────────────
generate_config() {
  step "Generating engram.yaml"

  local EMBEDDINGS_MODEL="text-embedding-3-small"
  local EMBEDDINGS_CONFIG
  if [ "$EMBEDDINGS_PROVIDER" = "local" ]; then
    EMBEDDINGS_CONFIG="  provider: local\n  model: sentence-transformers/all-MiniLM-L6-v2"
  else
    EMBEDDINGS_CONFIG="  provider: openai\n  model: text-embedding-3-small\n  api_key: \${OPENAI_API_KEY}"
  fi

  local RUNTIME_PROVIDER="anthropic"
  local RUNTIME_MODEL="claude-sonnet-4-6"
  if [ "$LLM_PROVIDER" = "openai" ]; then
    RUNTIME_PROVIDER="openai"
    RUNTIME_MODEL="gpt-4o"
  fi

  cat > "${ENGRAM_DIR}/engram.yaml" <<YAML
server:
  host: 0.0.0.0
  mcp_port: 8765
  api_port: 8766
  log_level: INFO

auth:
  api_keys:
    - key: \${ENGRAM_API_KEY}
      user_id: default
      namespaces: ["*"]

neo4j:
  uri: bolt://localhost:7687
  username: neo4j
  password: \${NEO4J_PASSWORD}
  database: neo4j

qdrant:
  host: localhost
  port: 6333
  collection: engram_memories

embeddings:
$(echo -e "$EMBEDDINGS_CONFIG")

runtime:
  default: api
  max_concurrent_workers: 5
  worker_timeout_s: 300
  api:
    provider: ${RUNTIME_PROVIDER}
    model: ${RUNTIME_MODEL}
    api_key: \${$(echo "$RUNTIME_PROVIDER" | tr '[:lower:]' '[:upper:]')_API_KEY}

namespaces:
  default: personal:default
  definitions:
    personal:default:
      owners: [default]

gateway:
  telegram:
    enabled: false
    bot_token: \${TELEGRAM_BOT_TOKEN}
    allowed_users: []
    default_namespace: personal:default
  whatsapp:
    enabled: false
    evolution_api_url: http://localhost:8080
    evolution_api_key: \${EVOLUTION_API_KEY}
    default_namespace: personal:default

learning:
  enabled: true
  episodic:
    enabled: true
    retention_days: 365
  feedback:
    correction_detection: true
    feedback_endpoint: true
  reflection:
    enabled: $([ "$LLM_PROVIDER" = "none" ] && echo "false" || echo "true")
    schedule: "0 2 * * *"
    trigger_on_correction: true
    min_episodes_per_run: 5
    lookback_days: 7
    model: claude-haiku-4-5-20251001
  skill_extraction:
    enabled: $([ "$LLM_PROVIDER" = "none" ] && echo "false" || echo "true")
    quality_threshold: 0.8
    similarity_threshold: 0.92
  heuristic_decay:
    enabled: true
    schedule: "0 3 * * 0"
    inactive_days_before_decay: 30
    decay_rate: 0.9
  quality_routing:
    enabled: true
    min_samples: 10
    quality_threshold: 0.6
YAML

  success "engram.yaml written to: ${ENGRAM_DIR}/engram.yaml"
}

# ─── .env file ────────────────────────────────────────────────────────────────
generate_env() {
  step "Writing .env file"

  cat > "${ENGRAM_DIR}/.env" <<ENV
# engram environment — generated by install.sh on $(date)
# DO NOT COMMIT THIS FILE

NEO4J_PASSWORD=${NEO4J_PASSWORD}
ENGRAM_API_KEY=${ENGRAM_API_KEY}

ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENROUTER_API_KEY=

TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=
EVOLUTION_API_KEY=
ENV

  chmod 600 "${ENGRAM_DIR}/.env"
  success ".env written to: ${ENGRAM_DIR}/.env (mode 600)"
}

# ─── docker-compose.yml ───────────────────────────────────────────────────────
generate_docker_compose() {
  step "Writing docker-compose.yml"

  cat > "${ENGRAM_DIR}/docker-compose.yml" <<'COMPOSE'
version: "3.9"

services:
  neo4j:
    image: neo4j:5.20
    container_name: engram-neo4j
    ports:
      - "7474:7474"
      - "7687:7687"
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD}
      NEO4J_PLUGINS: '["apoc"]'
      NEO4J_dbms_memory_heap_initial__size: 512m
      NEO4J_dbms_memory_heap_max__size: 1G
      NEO4J_dbms_security_procedures_unrestricted: apoc.*
    volumes:
      - ./data/neo4j:/data
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -u neo4j -p ${NEO4J_PASSWORD} 'RETURN 1' || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 10
      start_period: 30s
    restart: unless-stopped

  qdrant:
    image: qdrant/qdrant:v1.9.0
    container_name: engram-qdrant
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - ./data/qdrant:/qdrant/storage
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/healthz"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    restart: unless-stopped
COMPOSE

  success "docker-compose.yml written to: ${ENGRAM_DIR}/docker-compose.yml"
}

# ─── Docker image pull ────────────────────────────────────────────────────────
pull_docker_images() {
  step "Pulling Docker images (neo4j:5.20, qdrant:v1.9.0)"
  info "This may take a few minutes on first install..."
  docker pull neo4j:5.20 2>&1 | tail -1
  docker pull qdrant/qdrant:v1.9.0 2>&1 | tail -1
  success "Docker images ready."
}

# ─── Python package install ───────────────────────────────────────────────────
install_python_packages() {
  step "Installing Python packages"

  # Determine install source: local git clone or PyPI
  local SCRIPT_DIR
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local INSTALL_FROM_SOURCE=no
  if [ -f "${SCRIPT_DIR}/packages/core/pyproject.toml" ]; then
    INSTALL_FROM_SOURCE=yes
    info "Installing from local source clone at ${SCRIPT_DIR}"
  fi

  if [ "$INSTALL_FROM_SOURCE" = "yes" ]; then
    # Install all packages from source in dependency order
    local src_packages=(
      "${SCRIPT_DIR}/packages/core"
      "${SCRIPT_DIR}/packages/mcp-server"
      "${SCRIPT_DIR}/packages/orchestrator"
      "${SCRIPT_DIR}/packages/api"
      "${SCRIPT_DIR}/packages/learning"
    )
    if [ "$EMBEDDINGS_PROVIDER" = "local" ]; then
      src_packages[0]="${SCRIPT_DIR}/packages/core[local-embeddings]"
    fi
    info "Installing source packages: ${src_packages[*]}"
    "$PY_CMD" -m pip install --quiet --upgrade "${src_packages[@]}" --break-system-packages 2>/dev/null || \
    "$PY_CMD" -m pip install --quiet --upgrade "${src_packages[@]}"
  else
    # Install from PyPI
    local packages=(
      "engram-core"
      "engram-mcp-server"
      "engram-orchestrator"
      "engram-api"
      "engram-learning"
    )
    if [ "$EMBEDDINGS_PROVIDER" = "local" ]; then
      packages=("engram-core[local-embeddings]" "${packages[@]:1}")
    fi
    info "Installing PyPI packages: ${packages[*]}"
    "$PY_CMD" -m pip install --quiet --upgrade "${packages[@]}" --break-system-packages 2>/dev/null || \
    "$PY_CMD" -m pip install --quiet --upgrade "${packages[@]}"
  fi

  success "Python packages installed."
}

# ─── CLI launcher script ──────────────────────────────────────────────────────
install_cli() {
  step "Installing engram CLI"

  # Determine install location
  local bin_dir
  if [ -w "/usr/local/bin" ]; then
    bin_dir="/usr/local/bin"
  elif [ -d "$HOME/.local/bin" ]; then
    bin_dir="$HOME/.local/bin"
  else
    mkdir -p "$HOME/.local/bin"
    bin_dir="$HOME/.local/bin"
    warn "Created $HOME/.local/bin — ensure it is in your PATH."
  fi

  ENGRAM_CLI="${bin_dir}/engram"

  cat > "$ENGRAM_CLI" <<ENGRAM_CLI
#!/usr/bin/env bash
# engram CLI — start/stop/status the engram server
# Generated by install.sh

ENGRAM_DIR="\${ENGRAM_DIR:-${ENGRAM_DIR}}"
ENGRAM_CONFIG="\${ENGRAM_CONFIG:-\${ENGRAM_DIR}/engram.yaml}"
ENGRAM_LOG="\${ENGRAM_DIR}/logs/engram.log"
ENGRAM_PID="\${ENGRAM_DIR}/logs/engram.pid"
MCP_PORT=8765
API_PORT=8766

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

cd "\${ENGRAM_DIR}"

_start_docker() {
  echo -e "\${CYAN}  -->\${NC} Starting Neo4j and Qdrant..."
  ${DOCKER_COMPOSE} up -d 2>&1 | grep -v "^#"
  echo -e "\${CYAN}  -->\${NC} Waiting for databases to be healthy..."
  local attempts=0
  while ! ${DOCKER_COMPOSE} ps | grep -E "neo4j.*healthy" &>/dev/null; do
    sleep 3
    attempts=\$((attempts + 1))
    echo -ne "\r  Waiting for Neo4j... \${attempts}s"
    if [ \$attempts -ge 40 ]; then
      echo ""
      echo -e "\${RED}  [error]\${NC} Neo4j did not become healthy in time." >&2
      echo "  Check logs: engram logs neo4j" >&2
      exit 1
    fi
  done
  echo ""
}

_start_server() {
  if [ -f "\${ENGRAM_PID}" ] && kill -0 "\$(cat "\${ENGRAM_PID}")" 2>/dev/null; then
    echo -e "\${YELLOW}  [!]\${NC} engram server already running (PID \$(cat "\${ENGRAM_PID}"))"
    return 0
  fi
  echo -e "\${CYAN}  -->\${NC} Starting engram server..."
  set -a; source "\${ENGRAM_DIR}/.env"; set +a
  # Find python — prefer the one engram was installed into
  local PY
  PY="\$(command -v python3.12 || command -v python3.11 || command -v python3)"
  nohup "\$PY" -m engram_api.main \\
    >> "\${ENGRAM_LOG}" 2>&1 &
  echo \$! > "\${ENGRAM_PID}"
  sleep 2
  if kill -0 "\$(cat "\${ENGRAM_PID}")" 2>/dev/null; then
    echo -e "\${GREEN}  [ok]\${NC} engram server started (PID \$(cat "\${ENGRAM_PID}"))"
  else
    echo -e "\${RED}  [error]\${NC} engram server failed to start. Check: \${ENGRAM_LOG}" >&2
    rm -f "\${ENGRAM_PID}"
    exit 1
  fi
}

_stop_docker() {
  echo -e "\${CYAN}  -->\${NC} Stopping Docker containers..."
  ${DOCKER_COMPOSE} down 2>&1 | tail -3
}

_stop_server() {
  if [ -f "\${ENGRAM_PID}" ]; then
    local pid="\$(cat "\${ENGRAM_PID}")"
    if kill -0 "\$pid" 2>/dev/null; then
      echo -e "\${CYAN}  -->\${NC} Stopping engram server (PID \${pid})..."
      kill "\$pid" 2>/dev/null && echo -e "\${GREEN}  [ok]\${NC} Server stopped."
    fi
    rm -f "\${ENGRAM_PID}"
  else
    echo -e "\${YELLOW}  [!]\${NC} engram server is not running."
  fi
}

_health_check() {
  local url="http://localhost:\${API_PORT}/api/v1/admin/health"
  if curl -sf "\${url}" -H "Authorization: Bearer \$(grep ENGRAM_API_KEY "\${ENGRAM_DIR}/.env" | cut -d= -f2)" &>/dev/null; then
    echo -e "\${GREEN}  [ok]\${NC} API health: ok"
    return 0
  else
    return 1
  fi
}

case "\${1:-}" in
  start)
    echo ""
    echo -e "\${BOLD}Starting engram...\${NC}"
    _start_docker
    _start_server
    echo ""
    echo -e "\${BOLD}\${GREEN}engram is running!\${NC}"
    echo ""
    echo -e "  MCP SSE endpoint : \${BOLD}http://localhost:\${MCP_PORT}/sse\${NC}"
    echo -e "  REST API          : \${BOLD}http://localhost:\${API_PORT}/api/v1\${NC}"
    echo -e "  Neo4j browser     : \${BOLD}http://localhost:7474\${NC}"
    echo -e "  Data directory    : \${BOLD}\${ENGRAM_DIR}\${NC}"
    echo ""
    echo -e "  Claude Code MCP config:"
    echo -e "  \$(cat <<JSON
{
  \"mcpServers\": {
    \"engram\": {
      \"url\": \"http://localhost:\${MCP_PORT}/sse\",
      \"apiKey\": \"\$(grep ENGRAM_API_KEY "\${ENGRAM_DIR}/.env" | cut -d= -f2)\"
    }
  }
}
JSON
)"
    echo ""
    ;;

  stop)
    echo ""
    echo -e "\${BOLD}Stopping engram...\${NC}"
    _stop_server
    _stop_docker
    echo ""
    ;;

  restart)
    "\$0" stop
    "\$0" start
    ;;

  status)
    echo ""
    echo -e "\${BOLD}engram status\${NC}"
    echo ""
    # Docker containers
    echo -e "  Docker containers:"
    ${DOCKER_COMPOSE} ps 2>/dev/null | grep -E "neo4j|qdrant" | awk '{printf "    %-30s %s\n", \$1, \$3}' || \
      echo "    (none running)"
    echo ""
    # Python server
    echo -e "  Python server:"
    if [ -f "\${ENGRAM_PID}" ] && kill -0 "\$(cat "\${ENGRAM_PID}")" 2>/dev/null; then
      echo -e "    PID: \$(cat "\${ENGRAM_PID}")"
      if _health_check; then
        :
      else
        echo -e "    \${YELLOW}[!]\${NC} Server process running but health check failed"
      fi
    else
      echo -e "    \${RED}Not running\${NC}"
    fi
    echo ""
    ;;

  logs)
    target="\${2:-engram}"
    case "\$target" in
      neo4j)   ${DOCKER_COMPOSE} logs -f neo4j ;;
      qdrant)  ${DOCKER_COMPOSE} logs -f qdrant ;;
      engram|server) tail -f "\${ENGRAM_LOG}" ;;
      *)       tail -f "\${ENGRAM_LOG}" ;;
    esac
    ;;

  config)
    cat "\${ENGRAM_CONFIG}"
    ;;

  *)
    echo ""
    echo -e "  \${BOLD}engram\${NC} — persistent memory for LLM workflows"
    echo ""
    echo "  Usage: engram <command>"
    echo ""
    echo "  Commands:"
    echo "    start     Start all services (Docker + Python server)"
    echo "    stop      Stop all services"
    echo "    restart   Restart all services"
    echo "    status    Show running status"
    echo "    logs      Tail server logs  (engram|neo4j|qdrant)"
    echo "    config    Print engram.yaml"
    echo ""
    echo "  Data directory: \${ENGRAM_DIR}"
    echo ""
    ;;
esac
ENGRAM_CLI

  chmod +x "$ENGRAM_CLI"
  success "engram CLI installed at: ${ENGRAM_CLI}"

  # PATH check
  if ! echo "$PATH" | grep -q "$bin_dir"; then
    warn "${bin_dir} is not in your PATH."
    echo "  Add this to your shell profile (~/.bashrc or ~/.zshrc):"
    echo "    export PATH=\"${bin_dir}:\$PATH\""
  fi
}

# ─── Claude Code MCP injection ────────────────────────────────────────────────
inject_mcp_config() {
  local settings_file="$HOME/.claude/settings.json"
  [ -f "$settings_file" ] || return 0

  step "Claude Code detected"
  ask_yn ADD_TO_CLAUDE "Add engram to Claude Code's MCP servers?" "Y"
  [ "$ADD_TO_CLAUDE" = "no" ] && return 0

  local mcp_port=8765

  # Use Python for reliable JSON manipulation (no jq dependency)
  "$PY_CMD" - <<PYEOF
import json, sys, os

settings_file = os.path.expanduser("$settings_file")
backup_file = settings_file + ".bak"

try:
    with open(settings_file, "r") as f:
        raw = f.read()
    settings = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"  [error] Could not parse {settings_file}: {e}")
    sys.exit(1)

# Backup
with open(backup_file, "w") as f:
    f.write(raw)

# Inject MCP server
settings.setdefault("mcpServers", {})
settings["mcpServers"]["engram"] = {
    "url": "http://localhost:${mcp_port}/sse",
    "apiKey": "${ENGRAM_API_KEY}"
}

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"  [ok] Injected engram MCP server into {settings_file}")
print(f"  [ok] Backup saved to {backup_file}")
PYEOF
}

# ─── Claude Code automation hooks ─────────────────────────────────────────────
install_claude_code_hooks() {
  # Skip silently if Claude Code is not installed
  [ -d "$HOME/.claude" ] || return 0

  step "Claude Code automation hooks"
  echo "  Zero-touch hooks inject engram context before every prompt and"
  echo "  write session/commit memories automatically — no agent action needed."
  echo ""
  ask_yn INSTALL_HOOKS "Install zero-touch automation hooks?" "Y"
  [ "$INSTALL_HOOKS" = "no" ] && return 0

  ask HOOKS_NS "Default namespace for hooks" "personal:me"

  local hooks_dir="$HOME/.claude/hooks"
  local git_hooks_dir="$HOME/.git-hooks"
  local commands_dir="$HOME/.claude/commands"
  local settings_file="$HOME/.claude/settings.json"

  mkdir -p "$hooks_dir" "$git_hooks_dir" "$commands_dir"

  # ── Write engram.env config file ────────────────────────────────────────────
  cat > "$hooks_dir/engram.env" <<ENV
# engram Claude Code hooks config — edit to change API connection or namespace.
ENGRAM_API=http://localhost:8766
ENGRAM_KEY=${ENGRAM_API_KEY}
ENGRAM_DEFAULT_NS=${HOOKS_NS}
ENGRAM_TOP_K=5
ENV
  success "Hook config: $hooks_dir/engram.env"

  # ── Write inject hook ────────────────────────────────────────────────────────
  cat > "$hooks_dir/engram-inject.sh" <<'INJECT_EOF'
#!/usr/bin/env bash
# UserPromptSubmit hook — injects engram context before every Claude Code prompt.
# Namespace: .engram file in project > ENGRAM_NS_OVERRIDE env > ENGRAM_DEFAULT_NS
set -euo pipefail
HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOOKS_DIR/engram.env" ] && source "$HOOKS_DIR/engram.env"
ENGRAM_API="${ENGRAM_API:-http://localhost:8766}"
ENGRAM_KEY="${ENGRAM_KEY:-}"
ENGRAM_TOP_K="${ENGRAM_TOP_K:-5}"
INPUT=$(cat)
CWD=$(echo "$INPUT"    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")
PROMPT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('prompt',''))" 2>/dev/null || echo "")
if ! curl -sf --max-time 2 "$ENGRAM_API/api/v1/admin/health" -o /dev/null 2>/dev/null; then exit 0; fi
ENGRAM_NS="${ENGRAM_DEFAULT_NS:-personal:me}"
ENGRAM_NS="${ENGRAM_NS_OVERRIDE:-$ENGRAM_NS}"
REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || echo "")
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
  FILE_NS=$(grep '^namespace=' "$REPO_ROOT/.engram" 2>/dev/null | cut -d= -f2 | tr -d ' ')
  [[ -n "$FILE_NS" ]] && ENGRAM_NS="$FILE_NS"
fi
QUERY=$(echo "$PROMPT" | head -c 200 | python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip()))" 2>/dev/null || echo "")
[[ -z "$QUERY" ]] && exit 0
RESPONSE=$(curl -sf --max-time 5 "$ENGRAM_API/api/v1/memory/search?q=$QUERY&ns=$ENGRAM_NS&top_k=$ENGRAM_TOP_K" -H "X-API-Key: $ENGRAM_KEY" 2>/dev/null || echo "[]")
CONTEXT=$(echo "$RESPONSE" | python3 -c "
import sys, json
try: data = json.load(sys.stdin)
except: sys.exit(0)
results = data if isinstance(data, list) else data.get('results', [])
if not results: sys.exit(0)
lines = ['[engram: relevant past context]']
for r in results:
    mem = r.get('memory', r)
    mtype = mem.get('memory_type', 'fact')
    content = mem.get('content', '').strip()
    score = r.get('score', '')
    score_str = f'  (similarity: {score:.2f})' if isinstance(score, float) else ''
    if content: lines.append(f'[{mtype}]{score_str} {content[:280]}')
if len(lines) <= 1: sys.exit(0)
print('\n'.join(lines))
" 2>/dev/null || echo "")
[[ -z "$CONTEXT" ]] && exit 0
python3 -c "import json,sys; print(json.dumps({'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':sys.argv[1]}}))" "$CONTEXT"
INJECT_EOF
  chmod +x "$hooks_dir/engram-inject.sh"
  success "Inject hook: $hooks_dir/engram-inject.sh"

  # ── Write session-write hook ─────────────────────────────────────────────────
  cat > "$hooks_dir/engram-session-write.sh" <<'SESSION_EOF'
#!/usr/bin/env bash
# Stop hook — writes session state to engram after every Claude Code turn.
set -euo pipefail
HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOOKS_DIR/engram.env" ] && source "$HOOKS_DIR/engram.env"
ENGRAM_API="${ENGRAM_API:-http://localhost:8766}"
ENGRAM_KEY="${ENGRAM_KEY:-}"
INPUT=$(cat)
CWD=$(echo "$INPUT"        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || echo "")
[[ -z "$CWD" ]] && exit 0
if ! curl -sf --max-time 2 "$ENGRAM_API/api/v1/admin/health" -o /dev/null 2>/dev/null; then exit 0; fi
ENGRAM_NS="${ENGRAM_DEFAULT_NS:-personal:me}"
ENGRAM_NS="${ENGRAM_NS_OVERRIDE:-$ENGRAM_NS}"
REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || echo "")
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
  FILE_NS=$(grep '^namespace=' "$REPO_ROOT/.engram" 2>/dev/null | cut -d= -f2 | tr -d ' ')
  [[ -n "$FILE_NS" ]] && ENGRAM_NS="$FILE_NS"
fi
PROJECT=$(basename "$CWD")
BRANCH="" RECENT_COMMITS="" UNCOMMITTED=0
if git -C "$CWD" rev-parse --git-dir >/dev/null 2>&1; then
  BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
  RECENT_COMMITS=$(git -C "$CWD" log --oneline -5 --no-decorate 2>/dev/null || echo "")
  UNCOMMITTED=$(git -C "$CWD" status --short 2>/dev/null | wc -l | tr -d ' ')
fi
if [[ -n "$RECENT_COMMITS" ]]; then
  CONTENT="session ended | project: $PROJECT | dir: $CWD${BRANCH:+ | branch: $BRANCH} | uncommitted: $UNCOMMITTED
Recent commits:
$RECENT_COMMITS"
else
  CONTENT="session ended | project: $PROJECT | dir: $CWD"
fi
PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({'content':sys.argv[1],'namespace':sys.argv[2],'memory_type':'fact','tags':['session-log','auto',sys.argv[3]],'metadata':{'session_id':sys.argv[4],'project':sys.argv[3],'source':'claude-code-stop-hook'}}))
" "$CONTENT" "$ENGRAM_NS" "$PROJECT" "$SESSION_ID" 2>/dev/null)
curl -sf --max-time 5 -X POST "$ENGRAM_API/api/v1/memory/" -H "Content-Type: application/json" -H "X-API-Key: $ENGRAM_KEY" -d "$PAYLOAD" -o /dev/null 2>/dev/null || true
exit 0
SESSION_EOF
  chmod +x "$hooks_dir/engram-session-write.sh"
  success "Session hook: $hooks_dir/engram-session-write.sh"

  # ── Write global git post-commit hook ────────────────────────────────────────
  cat > "$git_hooks_dir/post-commit" <<'GIT_EOF'
#!/usr/bin/env bash
# Global git post-commit hook — writes every commit to engram memory.
# Never blocks a commit. Namespace: .engram file > ENGRAM_NS_OVERRIDE > ENGRAM_DEFAULT_NS
# Per-repo override: create .git/hooks/post-commit.local (called automatically).
set -euo pipefail
HOOKS_CONFIG="$HOME/.claude/hooks/engram.env"
[ -f "$HOOKS_CONFIG" ] && source "$HOOKS_CONFIG"
ENGRAM_API="${ENGRAM_API:-http://localhost:8766}"
ENGRAM_KEY="${ENGRAM_KEY:-}"
if ! curl -sf --max-time 2 "$ENGRAM_API/api/v1/admin/health" -o /dev/null 2>/dev/null; then exit 0; fi
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
REPO_NAME=$(basename "$REPO_ROOT")
ENGRAM_NS="${ENGRAM_DEFAULT_NS:-personal:me}"
ENGRAM_NS="${ENGRAM_NS_OVERRIDE:-$ENGRAM_NS}"
if [[ -f "$REPO_ROOT/.engram" ]]; then
  FILE_NS=$(grep '^namespace=' "$REPO_ROOT/.engram" 2>/dev/null | cut -d= -f2 | tr -d ' ')
  [[ -n "$FILE_NS" ]] && ENGRAM_NS="$FILE_NS"
fi
COMMIT_HASH=$(git rev-parse --short HEAD)
COMMIT_FULL=$(git rev-parse HEAD)
COMMIT_MSG=$(git log -1 --pretty=%B | head -5)
COMMIT_AUTHOR=$(git log -1 --pretty=%an)
CHANGED_FILES=$(git diff-tree --no-commit-id -r --name-only HEAD | head -20 | tr '\n' ' ')
BRANCH=$(git rev-parse --abbrev-ref HEAD)
MEMORY_TYPE="fact"
if echo "$COMMIT_MSG" | grep -qiE '^(feat|feature|refactor|arch):'; then MEMORY_TYPE="decision"
elif echo "$COMMIT_MSG" | grep -qiE '^(fix|hotfix|bug):'; then MEMORY_TYPE="incident"; fi
CONTENT="[engram-commit] $COMMIT_MSG
repo: $REPO_NAME | commit: $COMMIT_HASH | branch: $BRANCH | author: $COMMIT_AUTHOR
files: $CHANGED_FILES"
PAYLOAD=$(python3 -c "
import json, sys
msg,ns,mtype,commit,branch,author,files,repo = sys.argv[1:9]
print(json.dumps({'content':msg,'namespace':ns,'memory_type':mtype,'author':author,'tags':['git-commit','auto',repo,mtype],'metadata':{'commit_hash':commit,'repo':repo,'branch':branch,'source':'post-commit-hook'}}))
" "$CONTENT" "$ENGRAM_NS" "$MEMORY_TYPE" "$COMMIT_FULL" "$BRANCH" "$COMMIT_AUTHOR" "$CHANGED_FILES" "$REPO_NAME" 2>/dev/null)
curl -sf --max-time 5 -X POST "$ENGRAM_API/api/v1/memory/" -H "Content-Type: application/json" -H "X-API-Key: $ENGRAM_KEY" -d "$PAYLOAD" -o /dev/null 2>/dev/null || true
LOCAL_HOOK="$(git rev-parse --git-dir 2>/dev/null)/hooks/post-commit.local"
if [[ -x "$LOCAL_HOOK" ]]; then exec "$LOCAL_HOOK" "$@"; fi
exit 0
GIT_EOF
  chmod +x "$git_hooks_dir/post-commit"
  success "Git hook: $git_hooks_dir/post-commit"

  # ── Set global git hooks path ────────────────────────────────────────────────
  git config --global core.hooksPath "$git_hooks_dir"
  success "git config --global core.hooksPath $git_hooks_dir"

  # ── Write /engram slash command ──────────────────────────────────────────────
  cat > "$commands_dir/engram.md" <<'CMD_EOF'
Run the following bash commands to show engram status, then format the results clearly.

```bash
curl -sf http://localhost:8766/api/v1/admin/namespaces \
  -H "X-API-Key: $(grep ENGRAM_KEY ~/.claude/hooks/engram.env | cut -d= -f2 | tr -d ' ')" \
  2>/dev/null || echo "[]"

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
  echo "project-ns:$(grep '^namespace=' "$REPO_ROOT/.engram" | cut -d= -f2)"
else
  echo "project-ns:$(grep ENGRAM_DEFAULT_NS ~/.claude/hooks/engram.env | cut -d= -f2 | tr -d ' ')"
fi

NS=$(grep ENGRAM_DEFAULT_NS ~/.claude/hooks/engram.env | cut -d= -f2 | tr -d ' ')
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
  NS=$(grep '^namespace=' "$REPO_ROOT/.engram" | cut -d= -f2 | tr -d ' ')
fi
curl -sf "http://localhost:8766/api/v1/memory/search?q=session+commit+work&ns=$NS&top_k=5" \
  -H "X-API-Key: $(grep ENGRAM_KEY ~/.claude/hooks/engram.env | cut -d= -f2 | tr -d ' ')" \
  2>/dev/null || echo "[]"
```

Show:
1. **Namespaces** — bullet list of all namespace names
2. **Active namespace** — current namespace for this project, and how it was set (.engram file, env override, or default). If $ARGUMENTS contains a namespace like `ns:project:myproject`, show how to set it: create `.engram` with `namespace=project:myproject`
3. **Recent memories** — up to 5 as: `[type] score — first 120 chars`
CMD_EOF
  success "Slash command: $commands_dir/engram.md  (use /engram in Claude Code)"

  # ── Patch settings.json with hooks block ─────────────────────────────────────
  if [ -f "$settings_file" ]; then
    "$PY_CMD" - <<PYEOF
import json, os

settings_file = os.path.expanduser("$settings_file")
hooks_dir = os.path.expanduser("$hooks_dir")

try:
    with open(settings_file) as f:
        settings = json.load(f)
except Exception as e:
    print(f"  [warn] Could not patch {settings_file}: {e}")
    import sys; sys.exit(0)

inject_cmd = os.path.join(hooks_dir, "engram-inject.sh")
session_cmd = os.path.join(hooks_dir, "engram-session-write.sh")

# Build hooks block, preserving any existing hooks
settings.setdefault("hooks", {})

# UserPromptSubmit — prepend engram inject if not already there
ups = settings["hooks"].setdefault("UserPromptSubmit", [{"hooks": []}])
if not any(h.get("command", "") == inject_cmd
           for entry in ups for h in entry.get("hooks", [])):
    ups[0]["hooks"].insert(0, {"type": "command", "command": inject_cmd, "timeout": 8})

# Stop — append engram session-write if not already there
stops = settings["hooks"].setdefault("Stop", [{"hooks": []}])
if not any(h.get("command", "") == session_cmd
           for entry in stops for h in entry.get("hooks", [])):
    stops[0]["hooks"].append({"type": "command", "command": session_cmd, "timeout": 8, "async": True})

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"  [ok] Hooks registered in {settings_file}")
PYEOF
  fi

  echo ""
  success "Zero-touch automation installed."
  dim "  Context inject : every Claude Code prompt"
  dim "  Session write  : every turn end (async)"
  dim "  Commit write   : every git commit (all repos)"
  dim "  Slash command  : /engram — show namespace status"
  echo ""
  info "To set a namespace for a specific project, add a .engram file:"
  dim "    echo 'namespace=project:myproject' > /path/to/repo/.engram"
}

# ─── Final success message ────────────────────────────────────────────────────
print_success() {
  echo ""
  echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}${GREEN}  engram installed successfully!${NC}"
  echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""
  echo -e "  ${BOLD}Quick start:${NC}"
  echo ""
  echo -e "    ${CYAN}engram start${NC}"
  echo ""
  echo -e "  ${BOLD}Services once running:${NC}"
  echo ""
  echo -e "    MCP SSE endpoint : ${BOLD}http://localhost:8765/sse${NC}"
  echo -e "    REST API          : ${BOLD}http://localhost:8766/api/v1${NC}"
  echo -e "    Neo4j browser     : ${BOLD}http://localhost:7474${NC}"
  echo ""
  echo -e "  ${BOLD}Your data lives at:${NC}"
  echo -e "    ${ENGRAM_DIR}/"
  echo ""
  echo -e "  ${BOLD}Save these credentials:${NC}"
  echo -e "    Neo4j password : ${YELLOW}${NEO4J_PASSWORD}${NC}"
  echo -e "    engram API key : ${YELLOW}${ENGRAM_API_KEY}${NC}"
  echo -e "    (also saved in: ${ENGRAM_DIR}/.env)"
  echo ""
  echo -e "  ${BOLD}Claude Code MCP config${NC} (~/.claude/settings.json):"
  echo ""
  cat <<JSON
    {
      "mcpServers": {
        "engram": {
          "url": "http://localhost:8765/sse",
          "apiKey": "${ENGRAM_API_KEY}"
        }
      }
    }
JSON
  echo ""
  echo -e "  ${DIM}Run 'engram --help' for all commands.${NC}"
  echo ""
}

# ─── Main ─────────────────────────────────────────────────────────────────────
main() {
  header "engram Installer"

  # Already installed check
  if [ -f "$HOME/.engram/.env" ] || [ -f "${ENGRAM_DIR:-$HOME/.engram}/.env" ]; then
    warn "Existing engram installation detected."
    ask_yn REINSTALL "Re-run installation (existing data will NOT be deleted)?" "Y"
    if [ "$REINSTALL" = "no" ]; then
      echo "  Run 'engram start' to start existing installation."
      exit 0
    fi
  fi

  detect_os
  check_curl
  check_docker
  check_python
  check_pip
  check_jq

  collect_config
  create_directories
  generate_config
  generate_env
  generate_docker_compose
  pull_docker_images
  install_python_packages
  install_cli
  inject_mcp_config
  install_claude_code_hooks
  print_success
}

main "$@"
