#!/usr/bin/env bash
# Integration: every hook + the slash command that the client installer
# writes to ~/.claude/ uses 'Authorization: Bearer', never 'X-API-Key'.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "installed hooks use Bearer auth"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-client.sh /tmp/install-client.sh
sed -i 's|</dev/tty||g' /tmp/install-client.sh
export HOME=/test-home; mkdir -p "$HOME/.claude"
echo '{}' > "$HOME/.claude/settings.json"

bash /tmp/install-client.sh --server http://localhost:8766 --key engram-AUTH-TEST --namespace personal:me >/tmp/cli.log 2>&1

# Count X-API-Key and Bearer occurrences across every installed hook/command
HOOKS_DIR="$HOME/.claude/hooks"
CMDS_DIR="$HOME/.claude/commands"

X_API_HITS=$(grep -rl "X-API-Key" "$HOOKS_DIR" "$CMDS_DIR" 2>/dev/null | wc -l | tr -d ' ')
BEARER_HITS=$(grep -rl "Authorization.*Bearer" "$HOOKS_DIR" "$CMDS_DIR" 2>/dev/null | wc -l | tr -d ' ')

echo "X_API_FILE_COUNT=$X_API_HITS"
echo "BEARER_FILE_COUNT=$BEARER_HITS"

# Per-hook check
for h in engram-inject.sh engram-git-write.sh engram-precompact.sh engram-session-write.sh engram-heartbeat.py; do
  f="$HOOKS_DIR/$h"
  if [ -f "$f" ]; then
    grep -q "X-API-Key" "$f" && echo "${h}_HAS_XAPI=yes" || echo "${h}_HAS_XAPI=no"
  else
    echo "${h}_HAS_XAPI=missing"
  fi
done

# Slash command
if [ -f "$CMDS_DIR/engram.md" ]; then
  grep -q "X-API-Key" "$CMDS_DIR/engram.md" && echo "SLASH_HAS_XAPI=yes" || echo "SLASH_HAS_XAPI=no"
  grep -q "Authorization.*Bearer" "$CMDS_DIR/engram.md" && echo "SLASH_HAS_BEARER=yes" || echo "SLASH_HAS_BEARER=no"
else
  echo "SLASH_HAS_XAPI=missing"
fi
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

assert_eq "$(get X_API_FILE_COUNT)" "0" "no installed hook/command file contains X-API-Key"
[[ "$(get BEARER_FILE_COUNT)" -ge 4 ]] && pass "at least 4 hook/command files use Bearer auth" \
  || fail "Bearer auth file count too low: $(get BEARER_FILE_COUNT)"

for h in engram-inject.sh engram-git-write.sh engram-precompact.sh engram-session-write.sh engram-heartbeat.py; do
  v="$(get "${h}_HAS_XAPI")"
  assert_eq "$v" "no" "hook $h does NOT use X-API-Key"
done

assert_eq "$(get SLASH_HAS_XAPI)" "no" "/engram slash command does NOT use X-API-Key"
assert_eq "$(get SLASH_HAS_BEARER)" "yes" "/engram slash command uses Bearer"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
