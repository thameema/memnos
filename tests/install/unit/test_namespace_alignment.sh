#!/usr/bin/env bash
# Unit: server-side default user_id (engram.yaml.example) must match the
# client-side default namespace prefix (install-client.sh).
#
# If they disagree, a clean install gives users a namespace mismatch:
#   - server auto-creates personal:<user_id> for the admin api_key
#   - client's hooks write to personal:<client-default> from engram.env
# When those differ, every write goes to a namespace the server doesn't
# own → noise + permission warnings on every '/engram status' call.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"

describe "server user_id matches client default namespace"

# Extract the admin api_key's user_id from engram.yaml.example
SERVER_USER_ID="$(python3 - "${REPO_ROOT}/engram.yaml.example" <<'PY'
import sys, pathlib
lines = pathlib.Path(sys.argv[1]).read_text().splitlines()
in_auth = False
in_admin = False
for ln in lines:
    if ln.startswith("auth:"):
        in_auth = True; continue
    if in_auth and ln and not ln.startswith((" ", "\t")):
        break
    if "key:" in ln and "${ENGRAM_API_KEY}" in ln:
        in_admin = True; continue
    if in_admin:
        s = ln.strip()
        if s.startswith("user_id:"):
            print(s.split(":", 1)[1].strip())
            break
        if s.startswith("- key:") or s.startswith("# "):
            break
PY
)"

# Extract the client default namespace from install-client.sh
CLIENT_DEFAULT_NS="$(grep -E 'ask DEFAULT_NS.*"personal:' "${REPO_ROOT}/install-client.sh" \
  | head -1 \
  | grep -oE 'personal:[a-zA-Z0-9_-]+')"
CLIENT_PREFIX="${CLIENT_DEFAULT_NS#personal:}"

note "engram.yaml.example user_id: '${SERVER_USER_ID}'"
note "install-client.sh default namespace: '${CLIENT_DEFAULT_NS}'"
note "(client prefix after 'personal:': '${CLIENT_PREFIX}')"

if [[ "$SERVER_USER_ID" == "$CLIENT_PREFIX" ]]; then
  pass "server user_id '${SERVER_USER_ID}' matches client namespace prefix '${CLIENT_PREFIX}'"
else
  fail "MISMATCH: server user_id '${SERVER_USER_ID}' ≠ client prefix '${CLIENT_PREFIX}' — fresh installs will show 'namespace mismatch' on every /engram status"
fi

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
