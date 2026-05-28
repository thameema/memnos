#!/usr/bin/env bash
# Integration: prove an LLM agent (or any pipe-driven caller) can install
# engram without a tty by piping answers via stdin. Tests both:
#   1. install-server.sh standalone with answers piped to stdin
#   2. install.sh orchestrator with menu choice + sub-installer answers piped
#
# This unblocks the "ask your AI to install engram" UX — the agent reads the
# README to know the prompt order, then calls:
#     printf "ans1\nans2\n..." | curl ... | bash
# Without the [ -t 0 ] branch in ask()/ask_yn(), the installer ignores stdin
# and waits forever on /dev/tty.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"
source "${ROOT}/lib/docker_test.sh"

describe "non-tty stdin pipe install (agent-friendly)"

SCRIPT="$(mktemp)"; trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<'SCENARIO'
set -uo pipefail
# NOTE: we deliberately do NOT sed-strip </dev/tty here — the whole point of
# this test is to prove the runtime [ -t 0 ] branch works without that hack.
cp /src/install-server.sh /tmp/install-server.sh
cp /src/install-client.sh /tmp/install-client.sh
cp /src/install.sh        /tmp/install.sh
export HOME=/test-home; mkdir -p "$HOME"

# ── A: install-server.sh standalone, answers piped (no tty)
# Prompts: DATA_DIR, ENGRAM_API_KEY, ARCADEDB_PASSWORD, ANTHROPIC_API_KEY,
# OPENAI_API_KEY (empty → local), USE_QDRANT (N)
printf '\n\n\n\n\nN\n' | bash /tmp/install-server.sh --version master --mode full >/tmp/a.log 2>&1
echo "A_EXIT=$?"
[ -f "$HOME/.engram/.env" ] && echo "A_ENV_WRITTEN=yes" || echo "A_ENV_WRITTEN=no"
grep -q "^ENGRAM_API_KEY=engram-" "$HOME/.engram/.env" 2>/dev/null \
  && echo "A_KEY_AUTOGEN=yes" || echo "A_KEY_AUTOGEN=no"

# Confirm no answers got lost / no prompt hung
grep -q "Data directory" /tmp/a.log && echo "A_SAW_DATA_PROMPT=yes" || echo "A_SAW_DATA_PROMPT=no"
grep -q "Enable Qdrant" /tmp/a.log && echo "A_SAW_QDRANT_PROMPT=yes" || echo "A_SAW_QDRANT_PROMPT=no"
grep -q "engram server installed" /tmp/a.log && echo "A_FINISHED=yes" || echo "A_FINISHED=no"

# ── B: install.sh orchestrator (menu pick + sub-installer answers)
# Prompts: menu(1=both), DATA_DIR, ENGRAM_API_KEY, ARCADEDB_PASSWORD,
#          ANTHROPIC, OPENAI, USE_QDRANT(N), CLIENT_DEFAULT_NS
rm -rf "$HOME/.engram" "$HOME/.engram-src" "$HOME/.claude"
# Disable SCRIPT_DIR detection so install.sh exercises the curl-pipe path
sed -i 's|SCRIPT_DIR="\$(cd .*|SCRIPT_DIR=/tmp|' /tmp/install.sh
printf '1\n\n\n\n\n\nN\n\n' | bash /tmp/install.sh --version master >/tmp/b.log 2>&1
echo "B_EXIT=$?"
[ -f "$HOME/.engram/.env" ] && echo "B_SERVER_INSTALLED=yes" || echo "B_SERVER_INSTALLED=no"
[ -d "$HOME/.claude/hooks" ] && echo "B_CLIENT_INSTALLED=yes" || echo "B_CLIENT_INSTALLED=no"
grep -q "What would you like to install?" /tmp/b.log \
  && echo "B_SAW_MENU=yes" || echo "B_SAW_MENU=no"
SCENARIO

OUT="$(docker_run_scenario "$SCRIPT" 2>&1)"
get() { echo "$OUT" | grep "^$1=" | cut -d= -f2-; }

# A: standalone install-server.sh via stdin pipe
assert_eq "$(get A_EXIT)"             "0"   "A: install-server.sh exits 0 with piped stdin"
assert_eq "$(get A_ENV_WRITTEN)"      "yes" "A: .env was written → ask() read from stdin successfully"
assert_eq "$(get A_KEY_AUTOGEN)"      "yes" "A: empty input fell back to default (auto-generated key)"
assert_eq "$(get A_SAW_DATA_PROMPT)"  "yes" "A: first prompt (DATA_DIR) was reached"
assert_eq "$(get A_SAW_QDRANT_PROMPT)" "yes" "A: last prompt (Qdrant) was reached → no prompt hung"
assert_eq "$(get A_FINISHED)"         "yes" "A: install ran to completion"

# B: install.sh orchestrator via stdin pipe (covers the menu choice read + dispatch)
assert_eq "$(get B_EXIT)"              "0"   "B: install.sh orchestrator exits 0 with piped stdin"
assert_eq "$(get B_SAW_MENU)"          "yes" "B: menu was shown (top-level prompt reached)"
assert_eq "$(get B_SERVER_INSTALLED)"  "yes" "B: server side completed (~/.engram/.env exists)"
assert_eq "$(get B_CLIENT_INSTALLED)"  "yes" "B: client side completed (~/.claude/hooks exists)"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
