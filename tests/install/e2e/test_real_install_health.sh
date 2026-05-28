#!/usr/bin/env bash
# E2E: bring up the real engram stack and verify the /admin/health endpoint
# returns 200 with Bearer auth and the basic shape we expect.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/e2e_helpers.sh"

describe "real engram stack: /admin/health"

if ! e2e_up; then
  fail "could not bring up e2e stack"
  echo ""; echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"; exit 1
fi

KEY="$(e2e_key)"

# Bearer → 200
CODE=$(curl -s -o /tmp/health.json -w "%{http_code}" \
  "${E2E_API}/api/v1/admin/health" -H "Authorization: Bearer ${KEY}")
assert_eq "$CODE" "200" "Bearer auth → 200"

# Response is JSON
python3 -c "import json; json.load(open('/tmp/health.json'))" 2>/dev/null \
  && pass "health response is valid JSON" \
  || fail "health response is not JSON: $(head -c 200 /tmp/health.json)"

# Containers all healthy (per docker compose ps)
HEALTHY=$(e2e_compose ps --format json 2>/dev/null | python3 -c "
import json, sys
n_healthy = 0
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get('Health', '') == 'healthy' or 'healthy' in d.get('Status', '').lower():
        n_healthy += 1
print(n_healthy)
" 2>/dev/null || echo 0)
[[ "$HEALTHY" -ge 2 ]] && pass "at least 2 containers healthy (engram + arcadedb)" \
  || warn "only $HEALTHY containers reported healthy"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
