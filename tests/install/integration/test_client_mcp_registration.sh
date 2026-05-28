#!/usr/bin/env bash
# Integration: install-client.sh writes engram MCP to BOTH ~/.claude/settings.json
# (legacy) and ~/.claude.json (Claude Code v2 — primary). Preserves siblings.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "client MCP registration in ~/.claude.json + settings.json"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
cp /src/install-client.sh /tmp/install-client.sh
sed -i 's|</dev/tty||g' /tmp/install-client.sh
export HOME=/test-home; mkdir -p "$HOME/.claude"

# Pre-existing ~/.claude.json with sibling state
cat > "$HOME/.claude.json" <<'CJ'
{
  "userID": "preserve-me",
  "numStartups": 7,
  "mcpServers": {
    "sibling-mcp": {"type": "stdio", "command": "/bin/true"}
  }
}
CJ
echo '{}' > "$HOME/.claude/settings.json"

# pipe Y to the overwrite prompt (triggered because ~/.claude.json has a sibling MCP)
printf 'Y\n' | bash /tmp/install-client.sh --server http://localhost:8766 --key engram-TEST-KEY-xyz --namespace personal:me >/tmp/cli.log 2>&1
echo "EXIT=$?"

# Validate ~/.claude.json
python3 - <<PY
import json, sys
d = json.load(open("/test-home/.claude.json"))
mcps = d.get("mcpServers", {})
print("V2_HAS_ENGRAM=" + ("yes" if "engram" in mcps else "no"))
print("V2_PRESERVED_SIBLING=" + ("yes" if "sibling-mcp" in mcps else "no"))
print("V2_PRESERVED_USERID=" + ("yes" if d.get("userID") == "preserve-me" else "no"))
e = mcps.get("engram", {})
auth = e.get("headers", {}).get("Authorization", "")
print("V2_BEARER=" + ("yes" if auth.startswith("Bearer ") else "no"))
print("V2_URL_HAS_SSE=" + ("yes" if "/sse" in e.get("url", "") else "no"))
print("V2_URL_PORT_8765=" + ("yes" if ":8765" in e.get("url", "") else "no"))
PY

# Validate settings.json (legacy)
python3 - <<PY
import json
d = json.load(open("/test-home/.claude/settings.json"))
print("LEGACY_HAS_ENGRAM=" + ("yes" if "engram" in d.get("mcpServers", {}) else "no"))
PY

# Backups created
ls "$HOME/.claude.json".before-engram-* >/dev/null 2>&1 && echo "CJ_BACKUP=yes" || echo "CJ_BACKUP=no"
ls "$HOME/.claude/settings.json".before-engram-* >/dev/null 2>&1 && echo "SETTINGS_BACKUP=yes" || echo "SETTINGS_BACKUP=no"
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

assert_eq "$(get EXIT)" "0" "client installer exits 0"
assert_eq "$(get V2_HAS_ENGRAM)" "yes" "engram registered in ~/.claude.json (v2 location)"
assert_eq "$(get V2_PRESERVED_SIBLING)" "yes" "sibling MCP server preserved"
assert_eq "$(get V2_PRESERVED_USERID)" "yes" "top-level userID preserved"
assert_eq "$(get V2_BEARER)" "yes" "MCP auth uses Bearer scheme"
assert_eq "$(get V2_URL_HAS_SSE)" "yes" "MCP URL ends in /sse"
assert_eq "$(get V2_URL_PORT_8765)" "yes" "MCP URL uses port 8765 (mapped from 8766 api)"
assert_eq "$(get LEGACY_HAS_ENGRAM)" "yes" "engram also written to settings.json (legacy)"
assert_eq "$(get CJ_BACKUP)" "yes" "backup of ~/.claude.json created"
assert_eq "$(get SETTINGS_BACKUP)" "yes" "backup of settings.json created"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
