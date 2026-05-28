#!/usr/bin/env bash
# Integration: upgrading from the pre-v1.4 layout (.env in source clone)
# migrates the .env to ~/.engram/ and preserves the API key.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "pre-v1.4 layout migration on upgrade"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-server.sh /tmp/install-server.sh
sed -i 's|</dev/tty||g' /tmp/install-server.sh
export HOME=/test-home; mkdir -p "$HOME/.engram-src" "$HOME/.engram/arcadedb"

# Simulate pre-v1.4 state: .env in source clone, no .env in data dir.
git clone --depth 1 https://github.com/thameema/engram.git "$HOME/.engram-src" 2>/dev/null
cat > "$HOME/.engram-src/.env" <<EOF
ARCADEDB_PASSWORD=pre-v14-password-preserve-me
ENGRAM_API_KEY=engram-pre-v14-key-preserve-me
ENGRAM_VAULT_KEY=pre-v14-vault-key-1234567890abcdef
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
ENGRAM_DATA_DIR=$HOME/.engram
EOF

# Run upgrade
printf '1\n' | bash /tmp/install-server.sh --version master >/tmp/upgrade.log 2>&1
echo "EXIT=$?"

echo "MIGRATED_MSG=$(grep -q 'Migrated .env' /tmp/upgrade.log && echo yes || echo no)"
echo "ENV_IN_DATA_DIR=$([ -f "$HOME/.engram/.env" ] && echo yes || echo no)"
echo "ENV_NOT_IN_SRC=$([ ! -f "$HOME/.engram-src/.env" ] && echo yes || echo no)"
echo "KEY_PRESERVED=$(grep -q '^ENGRAM_API_KEY=engram-pre-v14-key-preserve-me' "$HOME/.engram/.env" 2>/dev/null && echo yes || echo no)"
echo "CONFIG_FILE_ADDED=$(grep -q '^ENGRAM_CONFIG_FILE=' "$HOME/.engram/.env" 2>/dev/null && echo yes || echo no)"
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

assert_eq "$(get EXIT)" "0" "upgrade exited 0"
assert_eq "$(get MIGRATED_MSG)" "yes" "installer announced migration"
assert_eq "$(get ENV_IN_DATA_DIR)" "yes" ".env now in ~/.engram/"
assert_eq "$(get ENV_NOT_IN_SRC)" "yes" ".env no longer in source clone"
assert_eq "$(get KEY_PRESERVED)" "yes" "API key preserved across migration"
assert_eq "$(get CONFIG_FILE_ADDED)" "yes" "ENGRAM_CONFIG_FILE added on upgrade"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
