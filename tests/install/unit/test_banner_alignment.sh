#!/usr/bin/env bash
# Unit: the ENGRAM banner in install.sh, install-server.sh, install-client.sh,
# and install-client.ps1 must all have 4 rows of identical character width.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT}/lib/assert.sh"
source "${ROOT}/lib/fixtures.sh"

check_banner_sh() {
  local file="$1"
  local rel="${file#${REPO_ROOT}/}"
  describe "banner: $rel"

  # Use python for reliable heredoc extraction.
  local body
  body="$(python3 - "$file" <<'PY'
import re, pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text()
m = re.search(r"cat <<'BANNER'\n(.*?)\nBANNER\b", src, re.DOTALL)
print(m.group(1) if m else "")
PY
)"
  if [[ -z "$body" ]]; then
    fail "could not extract banner heredoc"
    return
  fi

  # Keep only lines that contain ASCII-art glyphs (drop subtitle text), then
  # compare widths after rstrip — trailing whitespace doesn't affect visual
  # alignment and gets routinely stripped by editors.
  local art
  art="$(echo "$body" | grep -E '[|_/\\]' || true)"
  local nrows
  nrows="$(echo "$art" | wc -l | tr -d ' ')"
  assert_eq "$nrows" "4" "banner is 4 rows of ASCII art"

  local widths=()
  while IFS= read -r line; do
    # rstrip
    line="${line%"${line##*[![:space:]]}"}"
    widths+=("${#line}")
  done <<<"$art"

  # Compute min and max of row widths. ≤1 char drift is acceptable (editors
  # strip trailing whitespace from rows that end in a space-glyph). >1 means
  # actual content is missing — a real bug.
  local min=${widths[0]:-0} max=${widths[0]:-0}
  for w in "${widths[@]}"; do
    [[ $w -lt $min ]] && min=$w
    [[ $w -gt $max ]] && max=$w
  done
  local diff=$((max - min))
  if [[ $min -eq 0 ]]; then
    fail "empty banner"
  elif [[ $diff -eq 0 ]]; then
    pass "all 4 rows are ${min} chars wide"
  elif [[ $diff -eq 1 ]]; then
    warn "row widths differ by 1 (likely editor-stripped trailing space): ${widths[*]}"
  else
    fail "row widths differ by $diff (real content mismatch): ${widths[*]}"
  fi
}

check_banner_sh "${REPO_ROOT}/install.sh"
check_banner_sh "${REPO_ROOT}/install-server.sh"
check_banner_sh "${REPO_ROOT}/install-client.sh"

# PowerShell banner — Write-Host lines that contain ASCII-art glyphs.
describe "banner: install-client.ps1"
PS_BANNER="$(grep -E '^Write-Host "[ ]*[|_/\\]' "${REPO_ROOT}/install-client.ps1" 2>/dev/null || true)"
PS_ROWS=$(echo "$PS_BANNER" | wc -l | tr -d ' ')
assert_eq "$PS_ROWS" "4" "PowerShell banner is 4 rows"

echo ""
echo "PASSES=$PASSES FAILS=$FAILS WARNS=$WARNS"
exit "$FAILS"
