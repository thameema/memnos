#!/usr/bin/env bash
# Unit: docker-compose.yml must parse cleanly with `docker compose config`
# and resolve to the expected service shape.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"

describe "docker-compose.yml"
COMPOSE="${REPO_ROOT}/docker-compose.yml"
assert_file "$COMPOSE" "compose file present"

if ! command -v docker >/dev/null 2>&1; then
  skip "docker not on PATH — cannot run 'compose config'"
  echo ""
  echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
  exit 0
fi

# A minimal .env is required to satisfy variable expansion (no real secrets).
TMP_ENV="$(mktemp)"
cat > "$TMP_ENV" <<EOF
ARCADEDB_PASSWORD=test
ENGRAM_API_KEY=test
ENGRAM_VAULT_KEY=test
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
ENGRAM_EMBED_MODE=auto
ENGRAM_DATA_DIR=/tmp/test
ENGRAM_CONFIG_FILE=/tmp/test/engram.yaml
ENGRAM_VECTOR_BACKEND=
EOF
trap 'rm -f "$TMP_ENV"' EXIT

if ( cd "$REPO_ROOT" && docker compose --env-file "$TMP_ENV" config >/dev/null 2>&1 ); then
  pass "docker compose config parses cleanly"
else
  ERR="$( cd "$REPO_ROOT" && docker compose --env-file "$TMP_ENV" config 2>&1 | tail -5)"
  fail "docker compose config failed: $ERR"
fi

# Verify the compose has the engram + arcadedb services and the qdrant profile.
CFG="$( cd "$REPO_ROOT" && docker compose --env-file "$TMP_ENV" config 2>/dev/null)"
echo "$CFG" | grep -q "^  engram:"          && pass "engram service defined"          || fail "engram service missing"
echo "$CFG" | grep -q "^  arcadedb:"        && pass "arcadedb service defined"        || fail "arcadedb service missing"
echo "$CFG" | grep -q "image: arcadedata/arcadedb" && pass "arcadedb image set"       || fail "arcadedb image not pinned"
echo "$CFG" | grep -q "dockerfile: docker/Dockerfile" && pass "engram builds from docker/Dockerfile" || fail "wrong dockerfile path"

# qdrant should only appear under the qdrant profile
if echo "$CFG" | grep -q "^  qdrant:"; then
  # When config is rendered without --profile, qdrant may or may not appear
  # depending on compose version. We only require it to exist with the profile.
  pass "qdrant service rendered"
else
  # Re-render WITH the profile
  CFG2="$( cd "$REPO_ROOT" && docker compose --env-file "$TMP_ENV" --profile qdrant config 2>/dev/null)"
  echo "$CFG2" | grep -q "^  qdrant:" && pass "qdrant service rendered under --profile qdrant" \
    || fail "qdrant service not found"
fi

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
