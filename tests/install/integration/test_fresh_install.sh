#!/usr/bin/env bash
# Integration: a fresh install with all defaults writes the expected layout
# and starts the (mocked) docker compose stack.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "fresh install (server-only, no API keys, no Qdrant)"

# Build a scenario script the container will execute.
SCRIPT="$(mktemp)"
trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-server.sh /tmp/install-server.sh
sed -i 's|</dev/tty||g' /tmp/install-server.sh
export HOME=/test-home; mkdir -p "$HOME"

# Inputs:
#   DATA_DIR (default), MEMNOS_API_KEY (auto), ARCADEDB_PASSWORD (auto),
#   ANTHROPIC_API_KEY (skip), OPENAI_API_KEY (skip),
#   USE_LOCAL_EMBED (Y), USE_QDRANT (N)
printf '\n\n\n\n\nN\n' | bash /tmp/install-server.sh --version master >/tmp/install.log 2>&1
EXIT=$?

# Report findings the parent test can parse.
echo "EXIT=$EXIT"
echo "MEMNOS_SRC_EXISTS=$([ -d "$HOME/.memnos-src/.git" ] && echo yes || echo no)"
echo "ENV_FILE_EXISTS=$([ -f "$HOME/.memnos/.env" ] && echo yes || echo no)"
echo "YAML_FILE_EXISTS=$([ -f "$HOME/.memnos/memnos.yaml" ] && echo yes || echo no)"
echo "STALE_ENV_IN_SRC=$([ -f "$HOME/.memnos-src/.env" ] && echo yes || echo no)"
echo "STALE_YAML_IN_SRC=$([ -f "$HOME/.memnos-src/memnos.yaml" ] && echo yes || echo no)"

if [ -f "$HOME/.memnos/.env" ]; then
  echo "HAS_API_KEY=$(grep -q '^MEMNOS_API_KEY=memnos-' "$HOME/.memnos/.env" && echo yes || echo no)"
  echo "HAS_VAULT_KEY=$(grep -qE '^MEMNOS_VAULT_KEY=.{20,}' "$HOME/.memnos/.env" && echo yes || echo no)"
  echo "HAS_CONFIG_FILE_VAR=$(grep -q "^MEMNOS_CONFIG_FILE=$HOME/.memnos/memnos.yaml" "$HOME/.memnos/.env" && echo yes || echo no)"
  echo "HAS_EMBED_MODE=$(grep -q '^MEMNOS_EMBED_MODE=local' "$HOME/.memnos/.env" && echo yes || echo no)"
  PERM=$(stat -c %a "$HOME/.memnos/.env" 2>/dev/null)
  echo "ENV_PERM=$PERM"
fi

if [ -f "$HOME/.memnos/memnos.yaml" ]; then
  echo "YAML_USES_ENVVAR=$(grep -q 'host: ${ARCADEDB_HOST' "$HOME/.memnos/memnos.yaml" && echo yes || echo no)"
fi

grep -q "Pinning to ref from --version: " /tmp/install.log && echo "VERSION_PINNED=yes" || echo "VERSION_PINNED=no"
grep -q -- "--env-file $HOME/.memnos/.env" /tmp/install.log && echo "USES_ENV_FILE_FLAG=yes" || echo "USES_ENV_FILE_FLAG=no"
SCENARIO

# Run scenario, capture output, assert from it.
OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

assert_eq "$(get EXIT)" "0" "installer exits 0"
assert_eq "$(get MEMNOS_SRC_EXISTS)" "yes" "source clone created at ~/.memnos-src"
assert_eq "$(get ENV_FILE_EXISTS)" "yes" ".env lives in data dir (~/.memnos)"
assert_eq "$(get YAML_FILE_EXISTS)" "yes" "memnos.yaml lives in data dir"
assert_eq "$(get STALE_ENV_IN_SRC)" "no" "no .env left in source clone"
assert_eq "$(get STALE_YAML_IN_SRC)" "no" "no memnos.yaml left in source clone"
assert_eq "$(get HAS_API_KEY)" "yes" "auto-generated MEMNOS_API_KEY"
assert_eq "$(get HAS_VAULT_KEY)" "yes" "MEMNOS_VAULT_KEY generated (>=20 chars)"
assert_eq "$(get HAS_CONFIG_FILE_VAR)" "yes" "MEMNOS_CONFIG_FILE points at data-dir memnos.yaml"
assert_eq "$(get HAS_EMBED_MODE)" "yes" "MEMNOS_EMBED_MODE=local (since no OpenAI key + user opted in)"
assert_eq "$(get ENV_PERM)" "600" ".env permissions 600"
assert_eq "$(get YAML_USES_ENVVAR)" "yes" "memnos.yaml uses \${ARCADEDB_HOST} interpolation"
assert_eq "$(get VERSION_PINNED)" "yes" "honoured --version master"
assert_eq "$(get USES_ENV_FILE_FLAG)" "yes" "compose called with --env-file pointing at data dir"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
