#!/usr/bin/env bash
# Integration: --mode full vs --mode server-only must patch open_mode in
# engram.yaml correctly (full → true, server-only → false).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "--mode flag controls open_mode in engram.yaml"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-server.sh /tmp/install-server.sh
sed -i 's|</dev/tty||g' /tmp/install-server.sh

# A: --mode server-only (default) → open_mode: false
export HOME=/test-home-a; mkdir -p "$HOME"
printf '\n\n\n\n\nN\n' | bash /tmp/install-server.sh --version master --mode server-only >/tmp/a.log 2>&1
A_OPEN=$(grep -E "^  open_mode:" "$HOME/.engram/engram.yaml" 2>/dev/null | head -1 | awk '{print $2}')
echo "A_OPEN_MODE=$A_OPEN"
grep -q "open_mode=false" /tmp/a.log && echo "A_LOG_REPORTS=yes" || echo "A_LOG_REPORTS=no"

# B: --mode full → open_mode: true
export HOME=/test-home-b; mkdir -p "$HOME"
printf '\n\n\n\n\nN\n' | bash /tmp/install-server.sh --version master --mode full >/tmp/b.log 2>&1
B_OPEN=$(grep -E "^  open_mode:" "$HOME/.engram/engram.yaml" 2>/dev/null | head -1 | awk '{print $2}')
echo "B_OPEN_MODE=$B_OPEN"
grep -q "open_mode=true" /tmp/b.log && echo "B_LOG_REPORTS=yes" || echo "B_LOG_REPORTS=no"

# C: no --mode → defaults to server-only
export HOME=/test-home-c; mkdir -p "$HOME"
printf '\n\n\n\n\nN\n' | bash /tmp/install-server.sh --version master >/tmp/c.log 2>&1
C_OPEN=$(grep -E "^  open_mode:" "$HOME/.engram/engram.yaml" 2>/dev/null | head -1 | awk '{print $2}')
echo "C_OPEN_MODE=$C_OPEN"

# D: bad --mode value → exit nonzero
export HOME=/test-home-d; mkdir -p "$HOME"
printf '\n' | bash /tmp/install-server.sh --version master --mode garbage >/tmp/d.log 2>&1
echo "D_EXIT=$?"
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

assert_eq "$(get A_OPEN_MODE)" "false" "--mode server-only → open_mode: false"
assert_eq "$(get A_LOG_REPORTS)" "yes"  "log mentions open_mode=false for server-only"
assert_eq "$(get B_OPEN_MODE)" "true"  "--mode full → open_mode: true"
assert_eq "$(get B_LOG_REPORTS)" "yes"  "log mentions open_mode=true for full"
assert_eq "$(get C_OPEN_MODE)" "false" "no --mode defaults to server-only (secure default)"
[[ "$(get D_EXIT)" != "0" ]] && pass "bad --mode value rejected" || fail "bad --mode value accepted"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
