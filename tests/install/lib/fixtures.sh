# Shared fixtures and discovery helpers.

# REPO_ROOT — absolute path to the engram source repo, computed from this lib.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
export REPO_ROOT

# All shell scripts the test suite cares about.
ALL_SHELL_SCRIPTS=(
  "${REPO_ROOT}/install.sh"
  "${REPO_ROOT}/install-server.sh"
  "${REPO_ROOT}/install-client.sh"
  "${REPO_ROOT}/tools/verify-install.sh"
  "${REPO_ROOT}/hooks/bash/engram-inject.sh"
  "${REPO_ROOT}/hooks/bash/engram-git-write.sh"
  "${REPO_ROOT}/hooks/bash/engram-session-write.sh"
  "${REPO_ROOT}/hooks/bash/engram-precompact.sh"
)

# All Python scripts in the repo's runtime + installer surface (not tests).
ALL_PYTHON_SCRIPTS=(
  "${REPO_ROOT}/hooks/bash/engram-heartbeat.py"
  "${REPO_ROOT}/hooks/windows/engram-heartbeat.py"
)

# Test data namespace prefix — used to scope any writes and clean up afterwards.
TEST_NS_PREFIX="verify-install-test"

# Build a temp dir for the current test run; auto-cleaned by trap in run.sh.
make_tempdir() {
  local d
  d="$(mktemp -d "${TMPDIR:-/tmp}/engram-test-XXXXXX")"
  echo "$d"
}
