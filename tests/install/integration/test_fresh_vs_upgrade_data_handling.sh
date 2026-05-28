#!/usr/bin/env bash
# Integration: prove the destructive/non-destructive contract.
#
# Fresh install must:
#   • Refuse to proceed without typing 'yes' when data dir has content
#   • Actually wipe ~/.engram/{arcadedb,qdrant,.env,engram.yaml} on 'yes'
#   • Generate a new ENGRAM_API_KEY / ENGRAM_VAULT_KEY
#
# Upgrade must:
#   • NEVER touch arcadedb data
#   • Preserve every value in .env (API key, vault key, ArcadeDB password)
#   • Preserve the existing open_mode in engram.yaml
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "Fresh = destructive (with confirmation), Upgrade = non-destructive"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-server.sh /tmp/install-server.sh
sed -i 's|</dev/tty||g' /tmp/install-server.sh
export HOME=/test-home; mkdir -p "$HOME"

# ── Phase 1: initial Full install — open_mode: true, capture all keys
printf '\n\n\n\n\nN\n' | bash /tmp/install-server.sh --version master --mode full >/tmp/p1.log 2>&1
ORIG_API_KEY=$(grep "^ENGRAM_API_KEY=" "$HOME/.engram/.env" | cut -d= -f2)
ORIG_VAULT_KEY=$(grep "^ENGRAM_VAULT_KEY=" "$HOME/.engram/.env" | cut -d= -f2)
ORIG_OPEN_MODE=$(grep -E "^  open_mode:" "$HOME/.engram/engram.yaml" | awk '{print $2}')
# Plant a fake arcadedb db file so we can tell if it got wiped
mkdir -p "$HOME/.engram/arcadedb/engram"
echo "fake-memory-data" > "$HOME/.engram/arcadedb/engram/marker.txt"
echo "P1_KEY_LEN=${#ORIG_API_KEY}"
echo "P1_OPEN_MODE=$ORIG_OPEN_MODE"

# ── Phase 2: Upgrade (option 1) — must preserve EVERYTHING
printf '1\n' | bash /tmp/install-server.sh --version master >/tmp/p2.log 2>&1
NEW_API_KEY=$(grep "^ENGRAM_API_KEY=" "$HOME/.engram/.env" | cut -d= -f2)
NEW_VAULT_KEY=$(grep "^ENGRAM_VAULT_KEY=" "$HOME/.engram/.env" | cut -d= -f2)
NEW_OPEN_MODE=$(grep -E "^  open_mode:" "$HOME/.engram/engram.yaml" | awk '{print $2}')
[ "$ORIG_API_KEY" = "$NEW_API_KEY" ] && echo "P2_API_KEY_PRESERVED=yes" || echo "P2_API_KEY_PRESERVED=no"
[ "$ORIG_VAULT_KEY" = "$NEW_VAULT_KEY" ] && echo "P2_VAULT_KEY_PRESERVED=yes" || echo "P2_VAULT_KEY_PRESERVED=no"
[ "$ORIG_OPEN_MODE" = "$NEW_OPEN_MODE" ] && echo "P2_OPEN_MODE_PRESERVED=yes" || echo "P2_OPEN_MODE_PRESERVED=no"
[ -f "$HOME/.engram/arcadedb/engram/marker.txt" ] && echo "P2_DATA_PRESERVED=yes" || echo "P2_DATA_PRESERVED=no"
grep -q "detected existing deploy mode = .*full" /tmp/p2.log && echo "P2_AUTO_DETECTED_FULL=yes" || echo "P2_AUTO_DETECTED_FULL=no"
grep -q "preserving .*\.env" /tmp/p2.log && echo "P2_REPORTS_PRESERVE=yes" || echo "P2_REPORTS_PRESERVE=no"

# ── Phase 3: Fresh (option 2) — but user types 'no' → MUST abort + keep data
printf '2\nno\n' | bash /tmp/install-server.sh --version master >/tmp/p3.log 2>&1
P3_EXIT=$?
echo "P3_EXIT=$P3_EXIT"
grep -q "Fresh install aborted" /tmp/p3.log && echo "P3_REFUSED=yes" || echo "P3_REFUSED=no"
[ -f "$HOME/.engram/arcadedb/engram/marker.txt" ] && echo "P3_DATA_KEPT=yes" || echo "P3_DATA_KEPT=no"
[ -f "$HOME/.engram/.env" ] && echo "P3_ENV_KEPT=yes" || echo "P3_ENV_KEPT=no"

# ── Phase 4: Fresh (option 2) — user types 'yes' → MUST wipe + regen
printf '2\nyes\n\n\n\n\n\nN\n' | bash /tmp/install-server.sh --version master --mode full >/tmp/p4.log 2>&1
echo "P4_EXIT=$?"
[ ! -f "$HOME/.engram/arcadedb/engram/marker.txt" ] && echo "P4_DATA_WIPED=yes" || echo "P4_DATA_WIPED=no"
NEW2_API_KEY=$(grep "^ENGRAM_API_KEY=" "$HOME/.engram/.env" 2>/dev/null | cut -d= -f2)
[ "$ORIG_API_KEY" != "$NEW2_API_KEY" ] && [ -n "$NEW2_API_KEY" ] && echo "P4_NEW_KEY=yes" || echo "P4_NEW_KEY=no"
grep -q "Wiping" /tmp/p4.log && echo "P4_REPORTS_WIPE=yes" || echo "P4_REPORTS_WIPE=no"
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

# Initial install
[[ "$(get P1_KEY_LEN)" -gt 30 ]] && pass "phase 1: API key generated" || fail "phase 1: API key short"
assert_eq "$(get P1_OPEN_MODE)" "true" "phase 1: --mode full → open_mode: true"

# Upgrade preserves everything
assert_eq "$(get P2_API_KEY_PRESERVED)"   "yes" "upgrade: ENGRAM_API_KEY unchanged"
assert_eq "$(get P2_VAULT_KEY_PRESERVED)" "yes" "upgrade: ENGRAM_VAULT_KEY unchanged"
assert_eq "$(get P2_OPEN_MODE_PRESERVED)" "yes" "upgrade: open_mode unchanged (still true)"
assert_eq "$(get P2_DATA_PRESERVED)"      "yes" "upgrade: arcadedb data untouched"
assert_eq "$(get P2_AUTO_DETECTED_FULL)"  "yes" "upgrade: auto-detected existing 'full' mode"
assert_eq "$(get P2_REPORTS_PRESERVE)"    "yes" "upgrade: log says it's preserving .env"

# Fresh + 'no' → abort, keep data
[[ "$(get P3_EXIT)" != "0" ]] && pass "fresh+'no': exited nonzero" || fail "fresh+'no': should have aborted"
assert_eq "$(get P3_REFUSED)"     "yes" "fresh+'no': abort message shown"
assert_eq "$(get P3_DATA_KEPT)"   "yes" "fresh+'no': data NOT wiped (user said no)"
assert_eq "$(get P3_ENV_KEPT)"    "yes" "fresh+'no': .env preserved"

# Fresh + 'yes' → wipe + regen
assert_eq "$(get P4_EXIT)"        "0"   "fresh+'yes': completed successfully"
assert_eq "$(get P4_DATA_WIPED)"  "yes" "fresh+'yes': arcadedb data wiped"
assert_eq "$(get P4_NEW_KEY)"     "yes" "fresh+'yes': brand-new ENGRAM_API_KEY generated"
assert_eq "$(get P4_REPORTS_WIPE)" "yes" "fresh+'yes': log reports wipe"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
