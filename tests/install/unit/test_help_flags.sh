#!/usr/bin/env bash
# Unit: every installer must respond to --help / -h with usage text and exit 0.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"

describe "install.sh --help"
out="$(bash "${REPO_ROOT}/install.sh" --help 2>&1)"
ec=$?
assert_zero $ec "exit 0"
assert_contains "$out" "Usage:" "shows Usage:"
assert_contains "$out" "--version" "documents --version flag"
assert_contains "$out" "--server" "documents --server"
assert_contains "$out" "--client" "documents --client"

describe "install-server.sh --help"
out="$(bash "${REPO_ROOT}/install-server.sh" --help 2>&1)"
ec=$?
assert_zero $ec "exit 0"
assert_contains "$out" "--version" "documents --version flag"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
