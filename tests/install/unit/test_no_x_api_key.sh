#!/usr/bin/env bash
# Unit: regression guard for today's auth-header bug. The string "X-API-Key"
# must not appear anywhere in the shipping install/hook/SDK surface.
# Tools and tests are exempted (see ALLOWLIST below).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"

# Paths that may legitimately mention X-API-Key (offline tools, tests, error
# message strings used for log filtering). Anything else is a regression.
# Paths that may legitimately mention X-API-Key. tools/ is fully exempt — those
# are scripts the user runs ad-hoc, not the shipping install surface.
ALLOWLIST_RE='(^\./tools/|tests/|/e2e/conftest|knowledge\.py.*invalid x-api-key|tests/install/unit/test_no_x_api_key\.sh|skill_packs\.py)'

describe "no X-API-Key in shipping surface"
HITS="$(cd "$REPO_ROOT" && grep -rn "X-API-Key\|X-Api-Key\|x-api-key" \
  --include='*.py' --include='*.sh' --include='*.md' --include='*.yml' \
  --include='*.yaml' --include='*.ps1' --include='*.ts' --include='*.js' \
  --include='*.html' \
  --exclude-dir='.venv' --exclude-dir='node_modules' --exclude-dir='.git' \
  --exclude-dir='__pycache__' --exclude-dir='.pytest_cache' \
  . 2>/dev/null \
  | grep -v -E "$ALLOWLIST_RE" || true)"

if [[ -z "$HITS" ]]; then
  pass "no X-API-Key references in shipping code/docs/installers/hooks"
else
  fail "X-API-Key found in shipping surface (auth header regression):"
  echo "$HITS" | sed 's/^/      /'
fi

describe "Authorization: Bearer is used"
# Sanity: confirm at least the installer + verify-install + one hook use Bearer.
BEARER_COUNT=$(grep -rc "Authorization.*Bearer" \
  "${REPO_ROOT}/install-client.sh" \
  "${REPO_ROOT}/install-server.sh" \
  "${REPO_ROOT}/tools/verify-install.sh" \
  "${REPO_ROOT}/hooks/bash/engram-inject.sh" 2>/dev/null | awk -F: '{s+=$2} END{print s}')
if [[ "$BEARER_COUNT" -ge 5 ]]; then
  pass "Bearer auth in $BEARER_COUNT places across installers + hooks + verify"
else
  fail "expected Bearer auth references >= 5, got $BEARER_COUNT"
fi

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
