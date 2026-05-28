#!/usr/bin/env bash
# Integration: if engram.yaml on disk is a DIRECTORY (Docker auto-created it
# from a failed prior run), installer detects and recovers.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "stale engram.yaml directory recovery"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-server.sh /tmp/install-server.sh
sed -i 's|</dev/tty||g' /tmp/install-server.sh
export HOME=/test-home; mkdir -p "$HOME"

# Phase 1: clean install so we have a clone
printf '\n\n\n\n\nY\nN\n' | bash /tmp/install-server.sh --version master >/tmp/p1.log 2>&1

# Corrupt engram.yaml into a directory (Docker bind-mount bug simulation)
rm -f "$HOME/.engram/engram.yaml"
mkdir -p "$HOME/.engram/engram.yaml/leak"
[ -d "$HOME/.engram/engram.yaml" ] && echo "PRE_CORRUPTED=yes" || echo "PRE_CORRUPTED=no"

# Phase 2: re-run installer (upgrade) — should detect + remove directory + restore file
printf '1\n' | bash /tmp/install-server.sh --version master >/tmp/p2.log 2>&1
echo "EXIT=$?"

[ -f "$HOME/.engram/engram.yaml" ] && echo "RESTORED_AS_FILE=yes" || echo "RESTORED_AS_FILE=no"
grep -q "is a directory" /tmp/p2.log && echo "DETECTED=yes" || echo "DETECTED=no"
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

assert_eq "$(get PRE_CORRUPTED)" "yes" "corruption simulated (engram.yaml became a directory)"
assert_eq "$(get DETECTED)" "yes" "installer detected and reported the corruption"
assert_eq "$(get RESTORED_AS_FILE)" "yes" "engram.yaml restored to a file"
assert_eq "$(get EXIT)" "0" "recovery completed successfully"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
