#!/usr/bin/env bash
# Inside-container setup: install deps, create a mocked docker binary, then
# exec the test scenario at /scenario.sh.
set -uo pipefail

apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq curl python3 ca-certificates git >/dev/null 2>&1

cat > /usr/local/bin/docker <<'DOCK'
#!/bin/bash
case "$*" in
  *--version*)         echo "Docker 99.0.0 (test stub)" ;;
  *info*)              echo "Server Version: stub"; exit 0 ;;
  *"compose version"*) echo "Docker Compose v2.99.0 (test stub)" ;;
  *" ps"*)             echo "NAME STATUS engram healthy" ;;
  *) : ;;
esac
exit 0
DOCK
chmod +x /usr/local/bin/docker

exec bash /scenario.sh
