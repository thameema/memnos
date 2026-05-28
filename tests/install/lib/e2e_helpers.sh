# Helpers for Layer 3 — real-Docker E2E tests.
#
# Each test brings up an isolated engram stack with:
#   • Separate project name (engram-e2e) so containers don't collide with
#     the developer's existing engram install
#   • Test-only ports: 18765 (MCP/SSE), 18766 (REST API), 12480 (arcadedb)
#   • Test-only data dir: /tmp/engram-e2e-data
#   • Test-only source clone: /tmp/engram-e2e-src
#
# Setup is cached across tests in the same run: if the engram-e2e stack is
# already up and healthy from a prior test, e2e_up reuses it.

E2E_PROJECT="engram-e2e"
E2E_SRC="/tmp/engram-e2e-src"
E2E_DATA="/tmp/engram-e2e-data"
E2E_API_PORT="18766"
E2E_MCP_PORT="18765"
E2E_ARCADE_PORT="12480"
E2E_API="http://localhost:${E2E_API_PORT}"
E2E_MCP="http://localhost:${E2E_MCP_PORT}"

# Compose invocation (always include project + env-file).
e2e_compose() {
  ( cd "$E2E_SRC" && \
    docker compose --project-name "$E2E_PROJECT" --env-file "$E2E_DATA/.env" "$@" )
}

# Read the API key from the test stack's .env.
e2e_key() {
  grep '^ENGRAM_API_KEY=' "$E2E_DATA/.env" | cut -d= -f2
}

# Bring up the stack. Idempotent — does nothing if already healthy.
e2e_up() {
  # Quick health check — skip setup if already running
  if [ -f "$E2E_DATA/.env" ] && \
     curl -sf "${E2E_API}/api/v1/admin/health" \
       -H "Authorization: Bearer $(e2e_key)" >/dev/null 2>&1; then
    return 0
  fi

  echo "[e2e] bringing up isolated engram stack at :${E2E_API_PORT}/:${E2E_MCP_PORT}..."

  # Source clone
  if [ ! -d "$E2E_SRC/.git" ]; then
    rm -rf "$E2E_SRC"
    git clone --depth 1 "$REPO_ROOT" "$E2E_SRC" >/dev/null 2>&1 || \
      { echo "[e2e] clone failed"; return 1; }
  fi

  # Data dir + .env + engram.yaml
  mkdir -p "$E2E_DATA/arcadedb" "$E2E_DATA/arcadedb-logs" "$E2E_DATA/arcadedb-backups" "$E2E_DATA/qdrant"
  chmod 700 "$E2E_DATA"

  local KEY VAULT PW
  KEY="engram-e2e-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
  VAULT="$(python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
  PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18)[:20])')"

  cat > "$E2E_DATA/.env" <<EOF
ARCADEDB_PASSWORD=${PW}
ENGRAM_API_KEY=${KEY}
ENGRAM_VAULT_KEY=${VAULT}
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
ENGRAM_EMBED_MODE=local
ENGRAM_DATA_DIR=${E2E_DATA}
ENGRAM_CONFIG_FILE=${E2E_DATA}/engram.yaml
EOF
  chmod 600 "$E2E_DATA/.env"

  cp "$E2E_SRC/engram.yaml.example" "$E2E_DATA/engram.yaml"

  # Override BOTH ports AND container_name so the e2e stack does not collide
  # with an existing dev install. The base compose pins container_name to
  # engram-arcadedb / engram / engram-qdrant — without overriding those, the
  # 'compose up' fails with "name already in use" when the dev stack exists.
  cat > "$E2E_SRC/docker-compose.override.yml" <<EOF
services:
  arcadedb:
    container_name: engram-e2e-arcadedb
    ports: !override
      - "${E2E_ARCADE_PORT}:2480"
  engram:
    container_name: engram-e2e
    ports: !override
      - "${E2E_MCP_PORT}:8765"
      - "${E2E_API_PORT}:8766"
  qdrant:
    container_name: engram-e2e-qdrant
    ports: !override []
EOF

  echo "[e2e] building engram image (3-5 min on cold cache)..."
  e2e_compose build engram >/dev/null 2>&1 || { echo "[e2e] build failed"; e2e_compose logs engram | tail -20; return 1; }

  echo "[e2e] starting services..."
  e2e_compose up -d >/dev/null 2>&1 || { echo "[e2e] up failed"; return 1; }

  echo "[e2e] waiting for engram to be healthy..."
  local i=0
  while ! curl -sf "${E2E_API}/api/v1/admin/health" -H "Authorization: Bearer ${KEY}" >/dev/null 2>&1; do
    sleep 3; i=$((i+1)); echo -ne "\r  waiting ${i}*3s..."
    [ $i -ge 40 ] && { echo ""; echo "[e2e] engram never became healthy"; e2e_compose logs engram | tail -30; return 1; }
  done
  echo ""
  echo "[e2e] stack is healthy at ${E2E_API}"
  return 0
}

# Tear down the stack (called once at end of e2e layer).
e2e_down() {
  [ -d "$E2E_SRC" ] && e2e_compose down -v >/dev/null 2>&1 || true
  rm -rf "$E2E_SRC" "$E2E_DATA"
}
