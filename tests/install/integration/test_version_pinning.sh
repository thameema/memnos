#!/usr/bin/env bash
# Integration: --version flag pins the source clone to that ref, ENGRAM_REF
# env var also works, and an invalid ref fails with a clear error.
#
# We don't try to fully install old release tags — master's install-server.sh
# may be incompatible with v1.0/v1.1 source. We only verify that:
#   * the clone lands at the requested ref
#   * the installer reports which ref it pinned
#   * a bad ref fails fast with a clear error
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "--version flag and ENGRAM_REF env"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-server.sh /tmp/install-server.sh
sed -i 's|</dev/tty||g' /tmp/install-server.sh

# Helper: replace start_services and everything after resolve_source with a no-op
# so we only exercise ref resolution + the git clone, not the full install.
# This isolates the test to JUST the version-pinning behavior.
sed -i 's|^  start_services$|  echo "STUB: skip start_services for version-pin test"|' /tmp/install-server.sh

# A: explicit --version v1.1.0 — clone must land at v1.1.0
export HOME=/test-home-a; mkdir -p "$HOME"
printf '\n\n\n\n\nY\nN\n' | bash /tmp/install-server.sh --version v1.1.0 >/tmp/a.log 2>&1 || true
TAG_A=$(cd "$HOME/.engram-src" 2>/dev/null && git describe --tags --exact-match 2>/dev/null || echo "(no clone)")
echo "A_TAG=$TAG_A"
grep -q "Pinning to ref from --version: .*v1.1.0" /tmp/a.log && echo "A_REPORTED=yes" || echo "A_REPORTED=no"

# B: ENGRAM_REF env — should honour
export HOME=/test-home-b; mkdir -p "$HOME"
ENGRAM_REF=v1.2.0 printf '\n\n\n\n\nY\nN\n' | ENGRAM_REF=v1.2.0 bash /tmp/install-server.sh >/tmp/b.log 2>&1 || true
TAG_B=$(cd "$HOME/.engram-src" 2>/dev/null && git describe --tags --exact-match 2>/dev/null || echo "(no clone)")
echo "B_TAG=$TAG_B"
grep -q "from ENGRAM_REF env" /tmp/b.log && echo "B_REPORTED=yes" || echo "B_REPORTED=no"

# C: bad ref — must fail with clear error
export HOME=/test-home-c; mkdir -p "$HOME"
printf '\n\n\n\n\nY\nN\n' | bash /tmp/install-server.sh --version this-ref-does-not-exist-xyz >/tmp/c.log 2>&1 || true
echo "C_EXIT=$?"
grep -q "git clone failed for ref" /tmp/c.log && echo "C_CLEAR_ERR=yes" || echo "C_CLEAR_ERR=no"

# D: no --version, no env — must default to master
export HOME=/test-home-d; mkdir -p "$HOME"
printf '\n\n\n\n\nY\nN\n' | bash /tmp/install-server.sh >/tmp/d.log 2>&1 || true
grep -q "Installing from .*master.* (always-current default)" /tmp/d.log && echo "D_DEFAULT_MASTER=yes" || echo "D_DEFAULT_MASTER=no"
TAG_D=$(cd "$HOME/.engram-src" 2>/dev/null && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
echo "D_BRANCH=$TAG_D"
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

assert_eq "$(get A_TAG)" "v1.1.0" "--version v1.1.0 clones at v1.1.0 tag"
assert_eq "$(get A_REPORTED)" "yes" "installer reports --version pin"
assert_eq "$(get B_TAG)" "v1.2.0" "ENGRAM_REF env pins clone to that tag"
assert_eq "$(get B_REPORTED)" "yes" "installer reports ENGRAM_REF env pin"
assert_eq "$(get C_CLEAR_ERR)" "yes" "bad ref produces clear error"
assert_eq "$(get D_DEFAULT_MASTER)" "yes" "no flag → defaults to master (always-current)"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
