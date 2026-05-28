#!/usr/bin/env bash
# Unit: engram.yaml.example must use ${ARCADEDB_HOST:-localhost} interpolation,
# not a literal "host: localhost". This was the root cause of every
# httpx.ConnectError on fresh Docker installs.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"

YAML="${REPO_ROOT}/engram.yaml.example"
describe "engram.yaml.example uses env-var interpolation"
assert_file "$YAML"

# Extract host/port/password from the arcadedb: section specifically — there
# are other 'host:' lines in the file (server, gateway, etc).
ARC_BLOCK="$(python3 - "$YAML" <<'PY'
import sys, pathlib
# Read line by line, track which top-level section we are in.
# Intentionally NOT using PyYAML: engram.yaml has bash-style env-var
# interpolation that PyYAML rejects, and GitHub Actions ubuntu runner
# does not ship PyYAML by default.
lines = pathlib.Path(sys.argv[1]).read_text().splitlines()
in_arc = False
out = []
for ln in lines:
    if ln.startswith("arcadedb:"):
        in_arc = True; out.append(ln); continue
    if in_arc and ln and not ln.startswith((" ", "\t")):
        break
    if in_arc:
        out.append(ln)
print("\n".join(out))
PY
)"

HOST_LINE="$(echo "$ARC_BLOCK" | grep -E '^\s+host:' | head -1)"
if echo "$HOST_LINE" | grep -q '\${ARCADEDB_HOST'; then
  pass "arcadedb.host uses \${ARCADEDB_HOST...} (works in Docker)"
else
  fail "arcadedb.host does NOT use env-var interpolation: $HOST_LINE"
fi

PORT_LINE="$(echo "$ARC_BLOCK" | grep -E '^\s+port:' | head -1)"
if echo "$PORT_LINE" | grep -qE '\${ARCADEDB_PORT|port:\s+2480'; then
  pass "arcadedb.port present (env-var or default literal)"
else
  fail "arcadedb.port unusable: $PORT_LINE"
fi

PASS_LINE="$(echo "$ARC_BLOCK" | grep -E '^\s+password:' | head -1)"
if echo "$PASS_LINE" | grep -q '\${ARCADEDB_PASSWORD'; then
  pass "arcadedb.password uses \${ARCADEDB_PASSWORD}"
else
  fail "arcadedb.password not env-var driven: $PASS_LINE"
fi

# .env.example sanity — every variable engram.yaml interpolates should be
# defined (or commented) in .env.example.
ENV_EX="${REPO_ROOT}/.env.example"
describe ".env.example references match engram.yaml.example"
assert_file "$ENV_EX"
for var in ARCADEDB_PASSWORD ENGRAM_API_KEY ENGRAM_VAULT_KEY; do
  if grep -qE "^#?\s*${var}=" "$ENV_EX"; then
    pass "${var} present in .env.example"
  else
    fail "${var} missing from .env.example"
  fi
done

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
