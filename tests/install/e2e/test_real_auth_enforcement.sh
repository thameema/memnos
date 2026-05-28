#!/usr/bin/env bash
# E2E: prove that the API rejects X-API-Key and no-auth, accepts Bearer.
# This is the regression guard for today's auth-header bug — at the
# protocol level, not just at the test-file level.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/e2e_helpers.sh"

describe "real engram auth enforcement (Bearer required, X-API-Key rejected)"

if ! e2e_up; then
  fail "e2e stack down"
  echo ""; echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"; exit 1
fi

KEY="$(e2e_key)"

# 1. No auth header — must be 401
CODE=$(curl -s -o /dev/null -w "%{http_code}" "${E2E_API}/api/v1/admin/namespaces")
assert_eq "$CODE" "401" "no-auth → 401"

# 2. Bearer with right key — must be 200
CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  "${E2E_API}/api/v1/admin/namespaces" -H "Authorization: Bearer ${KEY}")
assert_eq "$CODE" "200" "Bearer (valid) → 200"

# 3. Bearer with wrong key — must be 401
CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  "${E2E_API}/api/v1/admin/namespaces" -H "Authorization: Bearer wrong-key-xyz")
assert_eq "$CODE" "401" "Bearer (invalid) → 401"

# 4. X-API-Key header — must be 401 (server is Bearer-only)
CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  "${E2E_API}/api/v1/admin/namespaces" -H "X-API-Key: ${KEY}")
assert_eq "$CODE" "401" "X-API-Key (engram never accepts) → 401"

# 5. Bearer without 'Bearer ' prefix — must be 401
CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  "${E2E_API}/api/v1/admin/namespaces" -H "Authorization: ${KEY}")
assert_eq "$CODE" "401" "Authorization without Bearer scheme → 401"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
