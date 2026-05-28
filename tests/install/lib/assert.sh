# Test assertion helpers — sourced by every test file.
# Tests set FAILS=0 and call pass/fail/warn/skip. The runner aggregates results.

# Colors — only when stdout is a terminal
if [[ -t 1 ]]; then
  T_GREEN=$'\033[0;32m'; T_RED=$'\033[0;31m'; T_YELLOW=$'\033[1;33m'
  T_BOLD=$'\033[1m'; T_DIM=$'\033[2m'; T_NC=$'\033[0m'
else
  T_GREEN=""; T_RED=""; T_YELLOW=""; T_BOLD=""; T_DIM=""; T_NC=""
fi

: "${PASSES:=0}"
: "${FAILS:=0}"
: "${WARNS:=0}"
: "${CURRENT_TEST:=}"

pass()  { PASSES=$((PASSES+1)); echo "  ${T_GREEN}✓${T_NC} $*"; }
fail()  { FAILS=$((FAILS+1));   echo "  ${T_RED}✗${T_NC} $*"; }
warn()  { WARNS=$((WARNS+1));   echo "  ${T_YELLOW}!${T_NC} $*"; }
skip()  {                       echo "  ${T_DIM}- $* (skipped)${T_NC}"; }
note()  {                       echo "  ${T_DIM}· $*${T_NC}"; }

# describe "..." — used by individual tests as a section header
describe() {
  CURRENT_TEST="$1"
  echo ""
  echo "${T_BOLD}── ${CURRENT_TEST} ──${T_NC}"
}

# Convenience assertions
assert_file()     { [[ -f "$1" ]]      && pass "${2:-file exists: $1}"      || fail "${2:-missing file: $1}"; }
assert_dir()      { [[ -d "$1" ]]      && pass "${2:-dir exists: $1}"       || fail "${2:-missing dir: $1}"; }
assert_exec()     { [[ -x "$1" ]]      && pass "${2:-executable: $1}"       || fail "${2:-not executable: $1}"; }
assert_not_file() { [[ ! -f "$1" ]]    && pass "${2:-absent: $1}"           || fail "${2:-should not exist: $1}"; }
assert_eq()       { [[ "$1" == "$2" ]] && pass "${3:-equal}"                || fail "${3:-not equal}: expected [$2], got [$1]"; }
assert_contains() { echo "$1" | grep -qF -- "$2" && pass "${3:-contains}"   || fail "${3:-missing substring}: [$2]"; }
assert_match()    { echo "$1" | grep -qE -- "$2" && pass "${3:-matches}"    || fail "${3:-no regex match}: [$2]"; }
assert_zero()     { [[ "$1" -eq 0 ]]   && pass "${2:-exit 0}"               || fail "${2:-exit nonzero}: $1"; }
assert_nonzero()  { [[ "$1" -ne 0 ]]   && pass "${2:-exit nonzero}"         || fail "${2:-unexpected exit 0}"; }
