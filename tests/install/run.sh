#!/usr/bin/env bash
# engram test-suite runner.
#
# Usage:
#   bash tests/install/run.sh [LAYER] [FILTER]
#     LAYER:  unit | integration | e2e | all   (default: unit)
#     FILTER: substring match on test file name (default: all)
#
# Examples:
#   bash tests/install/run.sh                      # all unit tests
#   bash tests/install/run.sh unit                 # same
#   bash tests/install/run.sh integration          # docker sandbox tests
#   bash tests/install/run.sh e2e                  # real-docker E2E
#   bash tests/install/run.sh all                  # everything
#   bash tests/install/run.sh unit auth            # unit tests matching 'auth'
#
# Exit code: 0 = all green, 1 = at least one failure.

set -uo pipefail

LAYER="${1:-unit}"
FILTER="${2:-}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/lib/assert.sh"
# shellcheck disable=SC1091
source "${ROOT}/lib/fixtures.sh"

if [[ -t 1 ]]; then
  R_GREEN=$'\033[0;32m'; R_RED=$'\033[0;31m'; R_YELLOW=$'\033[1;33m'
  R_BOLD=$'\033[1m'; R_DIM=$'\033[2m'; R_NC=$'\033[0m'
else
  R_GREEN=""; R_RED=""; R_YELLOW=""; R_BOLD=""; R_DIM=""; R_NC=""
fi

run_layer() {
  local layer="$1"
  local layer_dir="${ROOT}/${layer}"
  [[ -d "$layer_dir" ]] || { echo "${R_YELLOW}no tests in ${layer_dir}${R_NC}"; return 0; }

  local files=()
  while IFS= read -r f; do files+=("$f"); done < <(find "$layer_dir" -maxdepth 1 -name "test_*.sh" -type f | sort)
  [[ ${#files[@]} -eq 0 ]] && { echo "${R_DIM}(no test_*.sh files in $layer)${R_NC}"; return 0; }

  echo ""
  echo "${R_BOLD}━━━ ${layer} layer ━━━${R_NC}"
  local total_pass=0 total_fail=0 total_warn=0 ran=0
  for f in "${files[@]}"; do
    local name
    name="$(basename "$f" .sh)"
    [[ -n "$FILTER" && "$name" != *"$FILTER"* ]] && continue
    ran=$((ran+1))
    echo ""
    echo "${R_BOLD}▶ ${name}${R_NC}"
    # Run each test in a subshell so PASSES/FAILS reset cleanly.
    local out
    out="$(PASSES=0 FAILS=0 WARNS=0 bash "$f" 2>&1)"
    echo "$out"
    # Parse counts from the test's final line (we'll require tests to print them).
    local p f_ w
    p=$(echo "$out" | grep -oE "PASSES=[0-9]+" | tail -1 | cut -d= -f2 || echo 0)
    f_=$(echo "$out" | grep -oE "FAILS=[0-9]+" | tail -1 | cut -d= -f2 || echo 0)
    w=$(echo "$out" | grep -oE "WARNS=[0-9]+" | tail -1 | cut -d= -f2 || echo 0)
    total_pass=$((total_pass + ${p:-0}))
    total_fail=$((total_fail + ${f_:-0}))
    total_warn=$((total_warn + ${w:-0}))
  done

  echo ""
  echo "${R_BOLD}── ${layer} summary ──${R_NC}"
  echo "  files run : $ran"
  echo "  ${R_GREEN}pass: $total_pass${R_NC}"
  [[ $total_warn -gt 0 ]] && echo "  ${R_YELLOW}warn: $total_warn${R_NC}"
  [[ $total_fail -gt 0 ]] && echo "  ${R_RED}fail: $total_fail${R_NC}"

  return "$total_fail"
}

OVERALL_FAIL=0
case "$LAYER" in
  unit)        run_layer unit        || OVERALL_FAIL=$? ;;
  integration) run_layer integration || OVERALL_FAIL=$? ;;
  e2e)         run_layer e2e         || OVERALL_FAIL=$? ;;
  all)
    run_layer unit        || OVERALL_FAIL=$((OVERALL_FAIL + $?))
    run_layer integration || OVERALL_FAIL=$((OVERALL_FAIL + $?))
    run_layer e2e         || OVERALL_FAIL=$((OVERALL_FAIL + $?))
    ;;
  *)
    echo "unknown layer: $LAYER (use unit | integration | e2e | all)"
    exit 2 ;;
esac

echo ""
if [[ $OVERALL_FAIL -eq 0 ]]; then
  echo "${R_GREEN}${R_BOLD}✓ all green${R_NC}"
else
  echo "${R_RED}${R_BOLD}✗ ${OVERALL_FAIL} failure(s)${R_NC}"
fi
exit "$OVERALL_FAIL"
