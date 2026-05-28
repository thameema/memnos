#!/usr/bin/env bash
# Unit: every shell script in the install/hook surface must parse cleanly.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"

describe "bash -n on every install/hook script"
for f in "${ALL_SHELL_SCRIPTS[@]}"; do
  rel="${f#${REPO_ROOT}/}"
  if bash -n "$f" 2>/dev/null; then
    pass "$rel"
  else
    fail "$rel — $(bash -n "$f" 2>&1 | head -1)"
  fi
done

# Also check the verify-install script we just added.
describe "verify-install.sh syntax"
if bash -n "${REPO_ROOT}/tools/verify-install.sh" 2>/dev/null; then
  pass "tools/verify-install.sh"
else
  fail "tools/verify-install.sh — $(bash -n "${REPO_ROOT}/tools/verify-install.sh" 2>&1 | head -1)"
fi

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
