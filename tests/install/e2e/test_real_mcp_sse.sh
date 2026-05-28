#!/usr/bin/env bash
# E2E: the MCP /sse endpoint is reachable. We don't subscribe to the full
# event stream (it's open-ended); we just verify the port is open and the
# server speaks something HTTP-ish.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/e2e_helpers.sh"

describe "real MCP /sse endpoint"

if ! e2e_up; then
  fail "e2e stack down"
  echo ""; echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"; exit 1
fi

# HEAD on /sse — most MCP servers return either 200 (event-stream open) or
# 405 (method not allowed for HEAD). Both prove the endpoint exists.
SSE_HEAD=$(curl -sI --max-time 3 "${E2E_MCP}/sse" 2>&1 | head -1)
if echo "$SSE_HEAD" | grep -qE "200|405"; then
  pass "MCP /sse reachable: $(echo "$SSE_HEAD" | tr -d '\r')"
else
  fail "MCP /sse not responding: $SSE_HEAD"
fi

# Trying a GET with timeout — should immediately start an event-stream
# (we kill it after 2s). Empty body is fine; what we don't want is connection
# refused.
GET_RESULT=$(curl -s --max-time 2 "${E2E_MCP}/sse" 2>&1 | head -c 1 || true)
# curl exits nonzero from the timeout, but if it got bytes the connection worked.
if [[ -n "$GET_RESULT" || $(curl -sI --max-time 2 "${E2E_MCP}/sse" 2>&1 | wc -l | tr -d ' ') -gt 0 ]]; then
  pass "MCP server actively serving on :${E2E_MCP_PORT}"
else
  fail "MCP server not responding on :${E2E_MCP_PORT}"
fi

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
