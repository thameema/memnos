#!/usr/bin/env bash
# Integration: install-client.sh backs up CLAUDE.md before mutating, appends
# the engram section, and is idempotent — re-running does NOT duplicate.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "CLAUDE.md backup + idempotent append"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-client.sh /tmp/install-client.sh
sed -i 's|</dev/tty||g' /tmp/install-client.sh
export HOME=/test-home; mkdir -p "$HOME/.claude"

# Existing user content in CLAUDE.md
cat > "$HOME/.claude/CLAUDE.md" <<'PRE'
# My existing instructions

I have lots of custom workflow notes here.
This should NEVER be lost.
PRE

# Phase 1: fresh client install. Existing CLAUDE.md triggers prompt — pipe Y.
printf 'Y\n' | bash /tmp/install-client.sh --server http://localhost:8766 --key engram-test-1 >/tmp/p1.log 2>&1
echo "P1_EXIT=$?"
ls "$HOME/.claude/CLAUDE.md".before-engram-* >/dev/null 2>&1 && echo "P1_BACKUP=yes" || echo "P1_BACKUP=no"
grep -q "lots of custom workflow notes" "$HOME/.claude/CLAUDE.md" && echo "P1_USER_PRESERVED=yes" || echo "P1_USER_PRESERVED=no"
grep -q "engram — Persistent Memory MCP" "$HOME/.claude/CLAUDE.md" && echo "P1_ENGRAM_APPENDED=yes" || echo "P1_ENGRAM_APPENDED=no"

# Phase 2: re-run — answer Y to overwrite prompt
echo Y | bash /tmp/install-client.sh --server http://localhost:8766 --key engram-test-2 >/tmp/p2.log 2>&1
COUNT=$(grep -c "engram — Persistent Memory MCP" "$HOME/.claude/CLAUDE.md")
echo "P2_ENGRAM_COUNT=$COUNT"
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

assert_eq "$(get P1_EXIT)" "0" "first install exited 0"
assert_eq "$(get P1_BACKUP)" "yes" "CLAUDE.md backup created"
assert_eq "$(get P1_USER_PRESERVED)" "yes" "user's CLAUDE.md content preserved"
assert_eq "$(get P1_ENGRAM_APPENDED)" "yes" "engram section appended"
assert_eq "$(get P2_ENGRAM_COUNT)" "1" "engram section NOT duplicated on re-run"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
