#!/usr/bin/env bash
# Integration: when the user answers Y to 'Enable Qdrant?', .env gets
# ENGRAM_VECTOR_BACKEND=qdrant and compose is invoked with --profile qdrant.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "Qdrant opt-in"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-server.sh /tmp/install-server.sh
sed -i 's|</dev/tty||g' /tmp/install-server.sh
export HOME=/test-home; mkdir -p "$HOME"

# Inputs: defaults, skip both API keys, opt-in to local embeddings (Y), opt-in to Qdrant (Y)
printf '\n\n\n\n\nY\n' | bash /tmp/install-server.sh --version master >/tmp/install.log 2>&1

echo "QDRANT_IN_ENV=$(grep -q '^ENGRAM_VECTOR_BACKEND=qdrant' "$HOME/.engram/.env" && echo yes || echo no)"
grep -q -- "--profile qdrant" /tmp/install.log && echo "PROFILE_USED=yes" || echo "PROFILE_USED=no"
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

assert_eq "$(get QDRANT_IN_ENV)" "yes" "ENGRAM_VECTOR_BACKEND=qdrant in .env"
assert_eq "$(get PROFILE_USED)" "yes" "compose invoked with --profile qdrant"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
