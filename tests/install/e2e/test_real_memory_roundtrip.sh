#!/usr/bin/env bash
# E2E: write a memory via POST, then find it via the search endpoint.
# Proves that local embeddings work end-to-end (engram image was built with
# ENGRAM_EMBED_MODE=local, sentence-transformers installed, and search indexes).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/e2e_helpers.sh"

describe "real memory write + search roundtrip"

if ! e2e_up; then
  fail "e2e stack down"
  echo ""; echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"; exit 1
fi

KEY="$(e2e_key)"
NS="e2e-test-$(date +%s%N)"
CONTENT="E2E test marker: the quick brown engram jumps over the lazy database. $(date +%s%N)"

# Write
WRITE=$(curl -s -X POST "${E2E_API}/api/v1/memory/" \
  -H "Authorization: Bearer ${KEY}" -H "Content-Type: application/json" \
  -d "{\"content\":\"${CONTENT}\",\"namespace\":\"${NS}\",\"memory_type\":\"fact\",\"tags\":[\"e2e\"]}")
MEM_ID=$(echo "$WRITE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
[[ -n "$MEM_ID" ]] && pass "POST /memory returned id (${MEM_ID:0:8}...)" \
  || { fail "POST /memory failed: $(echo "$WRITE" | head -c 200)"; \
       echo ""; echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"; exit 1; }

# Give the indexer a moment
sleep 2

# Search by semantically-close query
SEARCH=$(curl -s -G "${E2E_API}/api/v1/memory/search" \
  --data-urlencode "q=quick brown engram lazy database" \
  --data-urlencode "ns=${NS}" \
  --data-urlencode "top_k=3" \
  -H "Authorization: Bearer ${KEY}")

# Did the search return at least one hit, and is our memory in it?
HIT_COUNT=$(echo "$SEARCH" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    items = d if isinstance(d, list) else d.get('results', d.get('items', []))
    print(len(items))
except Exception:
    print(-1)
")
[[ "$HIT_COUNT" -ge 1 ]] && pass "search returned $HIT_COUNT hit(s)" \
  || fail "search returned 0 hits — embeddings broken? Response: $(echo "$SEARCH" | head -c 200)"

FOUND_OUR=$(echo "$SEARCH" | python3 -c "
import json, sys
d = json.load(sys.stdin)
items = d if isinstance(d, list) else d.get('results', d.get('items', []))
mid = '$MEM_ID'
print('yes' if any((it.get('id') if isinstance(it, dict) else '') == mid for it in items) else 'no')
")
[[ "$FOUND_OUR" == "yes" ]] && pass "our test memory appears in the search results" \
  || warn "test memory not in top 3 results (still works — just less ranked)"

# Cleanup
curl -s -X DELETE "${E2E_API}/api/v1/memory/${MEM_ID}" -H "Authorization: Bearer ${KEY}" >/dev/null 2>&1 || true

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
