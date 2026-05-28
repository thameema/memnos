# Helper for running an install scenario inside a hermetic Debian container.
# Used by Layer 2 integration tests so each test starts with a clean filesystem,
# clean settings.json, clean ~/.engram*, and a mocked `docker` binary.

# Run a test scenario script in a fresh container. The scenario script gets
# REPO_ROOT mounted read-only at /src and a writeable HOME at /test-home.
#
#   docker_run_scenario SCRIPT_PATH [extra docker args...]
#
# The scenario script must:
#   - Be a stand-alone bash script
#   - Set HOME=/test-home if it needs to interact with ~/.engram* paths
#   - Print 'PASSES=N FAILS=N WARNS=N' on its last line
docker_run_scenario() {
  local script="$1"; shift
  : "${SCENARIO_MOUNT_OPTS:=}"

  docker run --rm \
    -v "${REPO_ROOT}:/src:ro" \
    -v "${ROOT}/lib/setup-container.sh:/setup.sh:ro" \
    -v "${script}:/scenario.sh:ro" \
    $SCENARIO_MOUNT_OPTS \
    "$@" \
    debian:12-slim \
    bash /setup.sh
}

# Aggregate counts from a scenario log
count_from_log() {
  local field="$1" file="$2"
  grep -oE "${field}=[0-9]+" "$file" | tail -1 | cut -d= -f2
}
