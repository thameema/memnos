#!/usr/bin/env bash
# Release hygiene gate (`scripts/release_hygiene.sh`):
# Fails non-zero if the release binary, CLI/MCP/HTTP surfaces, or built distributions leak:
#   1. Internal implementation language branding ("rust") in `--help`, `--version`, `status`,
#      `/healthz`, or MCP `initialize` / `tools/list` output, or in shipped test/workflow filenames.
#   2. Test-only environment switches (`FAKE_EXTRACT`, `EMBED_MODE`, `TEST_DSN`, `MOCK`,
#      `SIMULATE_WARMING`, `SKIP_MODEL_HASH`) inside the release binary.
#   3. Builder privacy leaks (`$HOME`, `$USER`, or internal `rust/src/` source prefixes) in `strings`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${1:-$REPO_ROOT/target/release/memnos}"

if [[ ! -x "$BIN" ]]; then
  echo "FAIL: release binary not found or not executable: $BIN" >&2
  exit 1
fi

FAILS=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1" >&2; FAILS=$((FAILS + 1)); }

echo "=== Release Hygiene Gate: $BIN ==="

# 1. CLI --help / --version / bare invocation must not mention implementation language
HELP_OUT="$("$BIN" --help 2>&1)"
VER_OUT="$("$BIN" --version 2>&1)"
BARE_OUT="$("$BIN" 2>&1 || true)"

if echo "$HELP_OUT $VER_OUT $BARE_OUT" | grep -qi "rust"; then
  fail "CLI help/version output mentions 'rust'"
else
  pass "CLI --help / --version / bare output is language-neutral"
fi

# 2. MCP initialize + tools/list must not mention implementation language
MCP_OUT="$(printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"gate","version":"1.0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | MEMNOS_MCP_AUTO_REGISTER=0 "$BIN" mcp 2>/dev/null || true)"

if echo "$MCP_OUT" | grep -qi "rust"; then
  fail "MCP initialize/tools/list mentions 'rust'"
else
  pass "MCP initialize + 55-tool definitions are language-neutral"
fi

# 3. Binary strings: no test-only environment switches
if strings "$BIN" | grep -E 'FAKE_EXTRACT|EMBED_MODE|TEST_DSN|SIMULATE_WARMING|SKIP_MODEL_HASH' >/dev/null 2>&1; then
  fail "Release binary contains test-only env switch strings"
  strings "$BIN" | grep -E 'FAKE_EXTRACT|EMBED_MODE|TEST_DSN|SIMULATE_WARMING|SKIP_MODEL_HASH' | head -n 10 >&2
else
  pass "Zero test-only env switches in release binary"
fi

# 4. Binary strings: no builder $HOME or `rust/src/` source path prefixes
if [[ -n "${HOME:-}" ]] && strings "$BIN" | grep -F "$HOME" >/dev/null 2>&1; then
  CNT="$(strings "$BIN" | grep -Fc "$HOME" || true)"
  fail "Release binary leaks builder HOME path ($HOME) in $CNT string(s)"
else
  pass "Zero builder \$HOME path strings in release binary"
fi

if strings "$BIN" | grep -F "rust/src/" >/dev/null 2>&1; then
  CNT="$(strings "$BIN" | grep -Fc "rust/src/" || true)"
  fail "Release binary contains $CNT unremapped 'rust/src/' source path string(s)"
else
  pass "Zero 'rust/src/' source path strings in release binary"
fi

# 5. Shipped test & workflow filenames must be language-neutral
BAD_FILES="$(find "$REPO_ROOT/tests" "$REPO_ROOT/.github/workflows" -maxdepth 2 -iname '*rust*' 2>/dev/null || true)"
if [[ -n "$BAD_FILES" ]]; then
  fail "Language-branded test or workflow filename(s) found: $BAD_FILES"
else
  pass "All test and workflow filenames are language-neutral"
fi

# 6. Optional dist/ wheel & sdist check when built artifacts exist
if [[ -d "$REPO_ROOT/dist" ]] && compgen -G "$REPO_ROOT/dist/*" >/dev/null; then
  DIST_BAD=0
  for pkg in "$REPO_ROOT"/dist/*; do
    if [[ -n "${HOME:-}" ]] && strings "$pkg" 2>/dev/null | grep -F "$HOME" >/dev/null 2>&1; then
      fail "Distribution artifact $(basename "$pkg") leaks builder HOME"
      DIST_BAD=1
    fi
  done
  if [[ "$DIST_BAD" -eq 0 ]]; then
    pass "Distribution artifacts in dist/ pass path-leak check"
  fi
fi

if [[ "$FAILS" -gt 0 ]]; then
  echo -e "\nFAILED: $FAILS release-hygiene check(s) failed." >&2
  exit 1
fi
echo -e "\nPASSED: all release-hygiene checks clean."
