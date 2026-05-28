#!/usr/bin/env bash
# Integration: detect previous install and choose Upgrade / Fresh / Abort.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "Upgrade / Fresh / Abort detection"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-server.sh /tmp/install-server.sh
sed -i 's|</dev/tty||g' /tmp/install-server.sh
export HOME=/test-home; mkdir -p "$HOME"

# Phase 1: fresh install
printf '\n\n\n\n\nY\nN\n' | bash /tmp/install-server.sh --version master >/tmp/p1.log 2>&1
OLD_KEY=$(grep "^ENGRAM_API_KEY=" "$HOME/.engram/.env" | cut -d= -f2)
echo "P1_EXIT=$?"
echo "P1_KEY_LEN=${#OLD_KEY}"

# Phase 2: re-run, choose Upgrade
printf '1\n' | bash /tmp/install-server.sh --version master >/tmp/p2.log 2>&1
echo "P2_EXIT=$?"
NEW_KEY=$(grep "^ENGRAM_API_KEY=" "$HOME/.engram/.env" | cut -d= -f2)
[ "$OLD_KEY" = "$NEW_KEY" ] && echo "P2_KEY_PRESERVED=yes" || echo "P2_KEY_PRESERVED=no"
grep -q "Mode: upgrade" /tmp/p2.log && echo "P2_UPGRADE_MODE=yes" || echo "P2_UPGRADE_MODE=no"
# Upgrade must NOT re-prompt for DATA_DIR
grep -q "Data directory (" /tmp/p2.log && echo "P2_REPROMPTED=yes" || echo "P2_REPROMPTED=no"

# Phase 3: re-run, choose Fresh (option 2)
printf '2\n\n\n\n\n\nY\nN\n' | bash /tmp/install-server.sh --version master >/tmp/p3.log 2>&1
echo "P3_EXIT=$?"
grep -q "Mode: fresh install" /tmp/p3.log && echo "P3_FRESH_MODE=yes" || echo "P3_FRESH_MODE=no"
grep -q "Data directory" /tmp/p3.log && echo "P3_REPROMPTED=yes" || echo "P3_REPROMPTED=no"

# Phase 4: re-run, choose Abort (option 3)
echo '3' | bash /tmp/install-server.sh --version master >/tmp/p4.log 2>&1
echo "P4_EXIT=$?"
grep -q "Aborted by user" /tmp/p4.log && echo "P4_ABORTED=yes" || echo "P4_ABORTED=no"
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

assert_eq "$(get P1_EXIT)" "0" "phase 1: fresh install ok"
[[ "$(get P1_KEY_LEN)" -gt 30 ]] && pass "phase 1: API key generated" || fail "phase 1: API key short"

assert_eq "$(get P2_EXIT)" "0" "phase 2: upgrade exited 0"
assert_eq "$(get P2_UPGRADE_MODE)" "yes" "phase 2: chose upgrade mode"
assert_eq "$(get P2_KEY_PRESERVED)" "yes" "phase 2: API key preserved"
assert_eq "$(get P2_REPROMPTED)" "no" "phase 2: did NOT re-prompt for config"

assert_eq "$(get P3_EXIT)" "0" "phase 3: fresh re-install exited 0"
assert_eq "$(get P3_FRESH_MODE)" "yes" "phase 3: chose fresh mode"
assert_eq "$(get P3_REPROMPTED)" "yes" "phase 3: did re-prompt"

# Abort exits nonzero
[[ "$(get P4_EXIT)" != "0" ]] && pass "phase 4: abort exited nonzero" || fail "phase 4: should have aborted"
assert_eq "$(get P4_ABORTED)" "yes" "phase 4: abort message shown"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
