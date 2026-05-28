#!/usr/bin/env bash
# engram post-install verification — exercises every layer of the install
# and reports pass / fail / warn for each check. Safe to re-run; the only
# mutations are POST of a test memory and DELETE of that same memory at
# the end (best-effort cleanup).
#
# Usage:
#   bash ~/.engram-src/tools/verify-install.sh
#   bash ~/.engram-src/tools/verify-install.sh --skip-write   # read-only mode

set -uo pipefail

# ─── Colors / helpers ────────────────────────────────────────────────────────
GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'; BOLD=$'\033[1m'; DIM=$'\033[2m'; NC=$'\033[0m'

PASS=0; FAIL=0; WARN=0
SKIP_WRITE="no"
[[ "${1:-}" == "--skip-write" ]] && SKIP_WRITE="yes"

pass()  { echo "  ${GREEN}✓${NC} $*"; PASS=$((PASS+1)); }
fail()  { echo "  ${RED}✗${NC} $*"; FAIL=$((FAIL+1)); }
warn()  { echo "  ${YELLOW}!${NC} $*"; WARN=$((WARN+1)); }
skip()  { echo "  ${DIM}-${NC} $* ${DIM}(skipped)${NC}"; }
hdr()   { echo ""; echo "${BOLD}═══ $* ═══${NC}"; }

DATA_DIR="${ENGRAM_DATA_DIR:-$HOME/.engram}"
SRC_DIR="${ENGRAM_SRC_DIR:-$HOME/.engram-src}"
ENV_FILE="${DATA_DIR}/.env"
YAML_FILE="${DATA_DIR}/engram.yaml"

# Will be populated from .env
ENGRAM_API=""; ENGRAM_KEY=""; QDRANT_ENABLED="no"

echo ""
echo "${BOLD}${BLUE}engram install verification${NC}"
echo "${DIM}data dir:${NC} ${DATA_DIR}"
echo "${DIM}source dir:${NC} ${SRC_DIR}"

# ─── 1. File layout (v1.4+ data-dir refactor) ────────────────────────────────
hdr "1. File layout"

[[ -d "${SRC_DIR}/.git" ]]              && pass "${SRC_DIR} is a git clone"                  || fail "${SRC_DIR} is not a git clone"
[[ -f "${SRC_DIR}/docker-compose.yml" ]] && pass "docker-compose.yml present in source"      || fail "docker-compose.yml missing"
[[ -f "${SRC_DIR}/docker/Dockerfile" ]]  && pass "docker/Dockerfile present"                 || fail "Dockerfile missing"

[[ -f "${ENV_FILE}" ]]                  && pass ".env in data dir (~/.engram/.env)"          || fail ".env NOT in ~/.engram/"
[[ -f "${YAML_FILE}" ]]                 && pass "engram.yaml in data dir"                    || fail "engram.yaml NOT in ~/.engram/"
[[ -d "${DATA_DIR}/arcadedb" ]]         && pass "arcadedb data directory exists"             || fail "no arcadedb data dir"

# Pre-v1.4 leftovers
[[ ! -f "${SRC_DIR}/.env" ]]            && pass "no stale .env in source clone"              || warn ".env still in ${SRC_DIR} (pre-v1.4 leftover)"
[[ ! -f "${SRC_DIR}/engram.yaml" ]]     && pass "no stale engram.yaml in source clone"       || warn "engram.yaml still in ${SRC_DIR} (pre-v1.4 leftover)"

# Permissions
if [[ -f "${ENV_FILE}" ]]; then
  PERM=$(stat -f %A "${ENV_FILE}" 2>/dev/null || stat -c %a "${ENV_FILE}" 2>/dev/null)
  [[ "$PERM" == "600" ]] && pass ".env permissions are 600" || warn ".env permissions are $PERM (expected 600)"
fi

# ─── 2. Configuration sanity ─────────────────────────────────────────────────
hdr "2. Configuration"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a; source "${ENV_FILE}"; set +a
  ENGRAM_KEY="${ENGRAM_API_KEY:-}"
  [[ -n "$ENGRAM_KEY" ]]                         && pass "ENGRAM_API_KEY set"                      || fail "ENGRAM_API_KEY missing"
  [[ -n "${ARCADEDB_PASSWORD:-}" ]]              && pass "ARCADEDB_PASSWORD set"                   || fail "ARCADEDB_PASSWORD missing"
  [[ -n "${ENGRAM_VAULT_KEY:-}" ]]               && pass "ENGRAM_VAULT_KEY set"                    || fail "ENGRAM_VAULT_KEY missing (vault encryption broken)"
  [[ "${ENGRAM_DATA_DIR:-}" == "${DATA_DIR}" ]]  && pass "ENGRAM_DATA_DIR points at data dir"      || warn "ENGRAM_DATA_DIR=${ENGRAM_DATA_DIR:-<unset>}"
  [[ "${ENGRAM_CONFIG_FILE:-}" == "${YAML_FILE}" ]] && pass "ENGRAM_CONFIG_FILE points at data dir engram.yaml" || warn "ENGRAM_CONFIG_FILE=${ENGRAM_CONFIG_FILE:-<unset>}"
  [[ "${ENGRAM_VECTOR_BACKEND:-}" == "qdrant" ]] && QDRANT_ENABLED="yes"
fi
ENGRAM_API="http://localhost:8766"

if [[ -f "${YAML_FILE}" ]]; then
  grep -q 'host: ${ARCADEDB_HOST' "${YAML_FILE}" \
    && pass "engram.yaml uses \${ARCADEDB_HOST} interpolation" \
    || fail "engram.yaml has literal host: localhost (won't work in Docker)"
fi

# ─── 3. Docker containers ────────────────────────────────────────────────────
hdr "3. Docker containers"

if ! command -v docker >/dev/null 2>&1; then
  fail "docker not on PATH — can't continue"
  echo ""; exit 1
fi

for c in engram engram-arcadedb $([ "$QDRANT_ENABLED" = "yes" ] && echo engram-qdrant); do
  STATUS=$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null || echo "missing")
  HEALTH=$(docker inspect "$c" --format '{{.State.Health.Status}}' 2>/dev/null || echo "n/a")
  if [[ "$STATUS" == "running" && "$HEALTH" == "healthy" ]]; then
    pass "$c: running + healthy"
  elif [[ "$STATUS" == "running" ]]; then
    warn "$c: running but health=$HEALTH"
  else
    fail "$c: state=$STATUS"
  fi
done

# All on same network
NETS=$(docker inspect engram --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null || echo "")
if [[ -n "$NETS" ]]; then
  ARCADEDB_NETS=$(docker inspect engram-arcadedb --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null || echo "")
  if echo "$ARCADEDB_NETS" | grep -qw "$(echo "$NETS" | awk '{print $1}')"; then
    pass "engram + engram-arcadedb on same docker network"
  else
    fail "engram and arcadedb on DIFFERENT networks (stale containers from old project)"
  fi
fi

# ─── 4. API auth — Bearer enforced, X-API-Key rejected ───────────────────────
hdr "4. API authentication"

# 4a. No auth → 401
CODE=$(curl -s -o /dev/null -w "%{http_code}" "${ENGRAM_API}/api/v1/admin/health" 2>/dev/null)
if [[ "$CODE" == "401" ]]; then
  pass "no-auth request returns 401 (auth enforced)"
elif [[ "$CODE" == "200" ]]; then
  warn "no-auth request returns 200 (engram.yaml open_mode: true — auth disabled)"
else
  fail "no-auth request returns $CODE (expected 401 or 200)"
fi

# 4b. Bearer → 200
CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  "${ENGRAM_API}/api/v1/admin/health" \
  -H "Authorization: Bearer ${ENGRAM_KEY}" 2>/dev/null)
[[ "$CODE" == "200" ]] && pass "Bearer auth → 200" || fail "Bearer auth → $CODE (API broken or wrong key)"

# 4c. X-API-Key → 401 (proves we're using Bearer-only API)
CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  "${ENGRAM_API}/api/v1/admin/health" \
  -H "X-API-Key: ${ENGRAM_KEY}" 2>/dev/null)
if [[ "$CODE" == "401" ]]; then
  pass "X-API-Key correctly REJECTED (proves Bearer is required, not optional)"
elif [[ "$CODE" == "200" ]]; then
  warn "X-API-Key accepted — engram is in open_mode (auth bypassed)"
else
  warn "X-API-Key returned $CODE"
fi

# ─── 5. Memory write + search roundtrip (proves embeddings work) ─────────────
hdr "5. Memory write + search roundtrip"

TEST_NS="verify-install-$(date +%s)"
TEST_CONTENT="verify-install marker $(date +%s%N) — if you see this in search, embeddings are working."

if [[ "$SKIP_WRITE" == "yes" ]]; then
  skip "memory write (--skip-write)"
  skip "memory search"
else
  # Write
  WRITE_RESP=$(curl -s -X POST "${ENGRAM_API}/api/v1/memory/" \
    -H "Authorization: Bearer ${ENGRAM_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"content\":\"${TEST_CONTENT}\",\"namespace\":\"${TEST_NS}\",\"memory_type\":\"fact\",\"tags\":[\"verify-install\"]}" \
    2>/dev/null)
  MEM_ID=$(echo "$WRITE_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")

  if [[ -n "$MEM_ID" ]]; then
    pass "POST /memory accepted (id=${MEM_ID:0:8}...)"
  else
    fail "POST /memory failed. Response: $(echo "$WRITE_RESP" | head -c 200)"
  fi

  # Search via vector / text — proves embeddings indexed
  if [[ -n "$MEM_ID" ]]; then
    sleep 1  # let async indexing settle
    SEARCH_RESP=$(curl -s -G "${ENGRAM_API}/api/v1/memory/search" \
      --data-urlencode "q=verify-install marker" \
      --data-urlencode "ns=${TEST_NS}" \
      --data-urlencode "top_k=3" \
      -H "Authorization: Bearer ${ENGRAM_KEY}" 2>/dev/null)
    HIT_COUNT=$(echo "$SEARCH_RESP" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    items = d if isinstance(d, list) else d.get('results', d.get('items', []))
    print(len(items))
except Exception:
    print(0)
" 2>/dev/null || echo "0")
    if [[ "$HIT_COUNT" -ge 1 ]]; then
      pass "search returned $HIT_COUNT result(s) for the test memory → embeddings working"
    else
      ERR=$(echo "$SEARCH_RESP" | head -c 200)
      if echo "$ERR" | grep -qi "sentence-transformers"; then
        fail "search failed: local embeddings (sentence-transformers) NOT installed in engram image"
      elif echo "$ERR" | grep -qi "embedding"; then
        fail "search failed: $(echo "$ERR" | head -c 150)"
      else
        fail "search returned 0 hits. Response: $ERR"
      fi
    fi

    # Cleanup
    curl -s -X DELETE "${ENGRAM_API}/api/v1/memory/${MEM_ID}" \
      -H "Authorization: Bearer ${ENGRAM_KEY}" >/dev/null 2>&1 || true
  fi
fi

# ─── 6. Namespaces endpoint ──────────────────────────────────────────────────
hdr "6. Namespaces"

NS_RESP=$(curl -s "${ENGRAM_API}/api/v1/admin/namespaces" \
  -H "Authorization: Bearer ${ENGRAM_KEY}" 2>/dev/null)
NS_COUNT=$(echo "$NS_RESP" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    items = d if isinstance(d, list) else d.get('namespaces', d.get('items', []))
    print(len(items))
except Exception:
    print(-1)
" 2>/dev/null)

if [[ "$NS_COUNT" -gt 0 ]]; then
  pass "namespaces endpoint returns $NS_COUNT namespace(s)"
elif [[ "$NS_COUNT" == "0" ]]; then
  warn "namespaces endpoint works but returned empty list"
else
  fail "namespaces endpoint failed: $(echo "$NS_RESP" | head -c 150)"
fi

# ─── 7. Corpus endpoint ──────────────────────────────────────────────────────
hdr "7. Corpus endpoint"

CORPUS_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  "${ENGRAM_API}/api/v1/corpus/" \
  -H "Authorization: Bearer ${ENGRAM_KEY}" 2>/dev/null)
if [[ "$CORPUS_CODE" == "200" ]]; then
  pass "GET /api/v1/corpus/ → 200 (corpus feature available)"
elif [[ "$CORPUS_CODE" == "404" ]]; then
  warn "corpus endpoint returns 404 (feature not built in this image)"
else
  warn "corpus endpoint returns $CORPUS_CODE"
fi

# ─── 8. MCP / SSE endpoint ───────────────────────────────────────────────────
hdr "8. MCP / SSE"

# HEAD on /sse — most MCP servers return either 200 (event-stream open) or
# 405 (method not allowed for HEAD). Both prove the endpoint exists.
SSE_HEAD=$(curl -sI --max-time 3 "http://localhost:8765/sse" 2>&1 | head -1 || echo "")
if echo "$SSE_HEAD" | grep -qE "200|405"; then
  pass "MCP SSE endpoint reachable: $SSE_HEAD"
elif [[ -z "$SSE_HEAD" ]]; then
  fail "MCP server on :8765 not responding"
else
  warn "MCP SSE responded: $SSE_HEAD"
fi

# ─── 9. Claude Code wiring ───────────────────────────────────────────────────
hdr "9. Claude Code wiring"

CLAUDE_JSON="$HOME/.claude.json"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

if [[ -f "$CLAUDE_JSON" ]]; then
  HAS_ENGRAM=$(python3 -c "
import json
d = json.load(open('$CLAUDE_JSON'))
print('yes' if 'engram' in d.get('mcpServers', {}) else 'no')
" 2>/dev/null)
  [[ "$HAS_ENGRAM" == "yes" ]] && pass "engram registered in ~/.claude.json (v2 location)" || fail "engram MCP NOT in ~/.claude.json"

  # Confirm the auth is Bearer
  AUTH=$(python3 -c "
import json
d = json.load(open('$CLAUDE_JSON'))
e = d.get('mcpServers', {}).get('engram', {})
print(e.get('headers',{}).get('Authorization',''))
" 2>/dev/null)
  if [[ "$AUTH" == Bearer\ * ]]; then
    pass "MCP auth uses Bearer scheme"
  elif [[ -n "$AUTH" ]]; then
    fail "MCP auth is not Bearer: $AUTH"
  fi
else
  fail "~/.claude.json not found (Claude Code not installed?)"
fi

# Hooks
for hook in engram.env engram-inject.sh engram-heartbeat.py \
            engram-git-write.sh engram-precompact.sh engram-session-write.sh; do
  [[ -f "$HOME/.claude/hooks/$hook" ]] && pass "hook: ~/.claude/hooks/$hook" || fail "missing hook: $hook"
done

# Hooks use Bearer (not X-API-Key)
if grep -rq "X-API-Key" "$HOME/.claude/hooks/" 2>/dev/null; then
  fail "X-API-Key still present in installed hooks (re-run install-client.sh to fix)"
else
  pass "no X-API-Key in installed hooks (auth is Bearer everywhere)"
fi

# Slash command
[[ -f "$HOME/.claude/commands/engram.md" ]] && pass "slash command: /engram" || fail "/engram slash command missing"

# CLAUDE.md
if [[ -f "$HOME/.claude/CLAUDE.md" ]] && grep -qE "engram MCP|engram — Persistent" "$HOME/.claude/CLAUDE.md"; then
  pass "~/.claude/CLAUDE.md has engram usage section"
else
  warn "~/.claude/CLAUDE.md missing engram section"
fi

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "${BOLD}═══ Summary ═══${NC}"
echo "  ${GREEN}passed: ${PASS}${NC}"
[[ $WARN -gt 0 ]] && echo "  ${YELLOW}warn:   ${WARN}${NC}"
[[ $FAIL -gt 0 ]] && echo "  ${RED}failed: ${FAIL}${NC}"
echo ""

if [[ $FAIL -eq 0 ]]; then
  echo "${GREEN}${BOLD}✓ engram install is healthy.${NC}"
  echo ""
  echo "Next: restart Claude Code (cmd+Q then reopen) and run /mcp to confirm engram connects."
  exit 0
else
  echo "${RED}${BOLD}✗ ${FAIL} check(s) failed.${NC} Review the output above."
  echo ""
  echo "Common fixes:"
  echo "  • Hooks have X-API-Key → re-run: curl -fsSL https://raw.githubusercontent.com/thameema/engram/master/install-client.sh | bash -s -- --server http://localhost:8766 --key \$(grep '^ENGRAM_API_KEY=' ~/.engram/.env | cut -d= -f2)"
  echo "  • Containers unhealthy   → cd ~/.engram-src && docker compose --env-file ~/.engram/.env logs engram"
  echo "  • engram crash-looping   → bash ~/.engram-src/tools/verify-install.sh --skip-write && docker logs engram --tail 50"
  exit 1
fi
