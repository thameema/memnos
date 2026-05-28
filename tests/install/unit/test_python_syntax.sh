#!/usr/bin/env bash
# Unit: every Python script must parse cleanly. Catches typos in the heartbeat
# daemon and the hooks/windows/ Python copy.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"

describe "python compile on hook scripts"
for f in "${ALL_PYTHON_SCRIPTS[@]}"; do
  rel="${f#${REPO_ROOT}/}"
  if python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
    pass "$rel"
  else
    fail "$rel — $(python3 -c "import ast; ast.parse(open('$f').read())" 2>&1 | tail -1)"
  fi
done

# Also extract every python3 heredoc from install-client.sh and check it parses.
describe "python heredocs inside install-client.sh"
EXTRACT_DIR="$(mktemp -d)"
trap 'rm -rf "$EXTRACT_DIR"' EXIT

python3 <<PY
import re, pathlib
src = pathlib.Path("${REPO_ROOT}/install-client.sh").read_text()
# Match: python3 - <<TAG ... TAG  or  python3 -c "..."  or  python3 <<'TAG' ... TAG
# We look for heredoc form: python3 ... <<...TAG\n...\nTAG
pattern = re.compile(r"python3[^\n<]*<<-?\s*'?(\w+)'?\n(.*?)\n\1\b", re.DOTALL)
out_dir = pathlib.Path("${EXTRACT_DIR}")
for i, m in enumerate(pattern.finditer(src)):
    tag, body = m.group(1), m.group(2)
    (out_dir / f"py_{i}_{tag}.py").write_text(body)
print(f"extracted {sum(1 for _ in out_dir.glob('*.py'))} python heredocs")
PY

count=0
for pyfile in "$EXTRACT_DIR"/*.py; do
  [[ -f "$pyfile" ]] || continue
  count=$((count+1))
  if python3 -c "import ast; ast.parse(open('$pyfile').read())" 2>/dev/null; then
    pass "heredoc $(basename "$pyfile")"
  else
    fail "heredoc $(basename "$pyfile"): $(python3 -c "import ast; ast.parse(open('$pyfile').read())" 2>&1 | tail -1)"
    head -10 "$pyfile" | sed 's/^/      /'
  fi
done
[[ $count -eq 0 ]] && warn "no python heredocs found in install-client.sh (extraction regex may be stale)"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
