#!/usr/bin/env bash
# E2E: write a memory, restart the stack (down + up, NOT down -v), verify
# the memory survives. Catches data-loss bugs from container-scoped volumes.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/e2e_helpers.sh"

describe "real data persistence across compose restart"

if ! e2e_up; then
  fail "e2e stack down"
  echo ""; echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"; exit 1
fi

KEY="$(e2e_key)"
NS="e2e-persistence-$(date +%s%N)"
MARKER="persistence-marker-$(date +%s%N)"

# Write a marker memory
WRITE=$(curl -s -X POST "${E2E_API}/api/v1/memory/" \
  -H "Authorization: Bearer ${KEY}" -H "Content-Type: application/json" \
  -d "{\"content\":\"${MARKER}\",\"namespace\":\"${NS}\",\"memory_type\":\"fact\",\"tags\":[\"persistence\"]}")
MEM_ID=$(echo "$WRITE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))")
[[ -n "$MEM_ID" ]] || { fail "could not write test memory"; echo ""; echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"; exit 1; }
pass "wrote marker memory (id=${MEM_ID:0:8}...)"

# Restart — down WITHOUT -v (keep volumes), then up
e2e_compose down >/dev/null 2>&1
sleep 1
e2e_compose up -d >/dev/null 2>&1
pass "compose down + up completed"

# Wait for engram healthy again
i=0
while ! curl -sf "${E2E_API}/api/v1/admin/health" -H "Authorization: Bearer ${KEY}" >/dev/null 2>&1; do
  sleep 3; i=$((i+1))
  [ $i -ge 30 ] && { fail "engram never came back healthy after restart"; echo ""; echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"; exit 1; }
done
pass "engram healthy again after restart"

# Search for the marker
SEARCH=$(curl -s -G "${E2E_API}/api/v1/memory/search" \
  --data-urlencode "q=${MARKER}" --data-urlencode "ns=${NS}" --data-urlencode "top_k=3" \
  -H "Authorization: Bearer ${KEY}")
FOUND=$(echo "$SEARCH" | python3 -c "
import json, sys
d = json.load(sys.stdin)
items = d if isinstance(d, list) else d.get('results', d.get('items', []))
mid = '$MEM_ID'
print('yes' if any((it.get('id') if isinstance(it, dict) else '') == mid for it in items) else 'no')
" 2>/dev/null || echo "no")
[[ "$FOUND" == "yes" ]] && pass "memory survived compose restart" \
  || fail "memory NOT found after restart — data lost? Response: $(echo "$SEARCH" | head -c 200)"

# Cleanup
curl -s -X DELETE "${E2E_API}/api/v1/memory/${MEM_ID}" -H "Authorization: Bearer ${KEY}" >/dev/null 2>&1 || true

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
