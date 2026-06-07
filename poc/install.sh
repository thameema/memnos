#!/usr/bin/env bash
# memnos installer (macOS / Linux). Postgres is a PREREQUISITE — this does NOT install it.
# Installs the `memnos` CLI+server (one package) and runs the setup wizard.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "Python 3.10+ is required. Install it and re-run."; exit 1; }

echo "[memnos] installing the 'memnos' command (CLI + server) ..."
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$DIR"
else
  python3 -m pip install --user pipx >/dev/null 2>&1 && python3 -m pipx ensurepath >/dev/null 2>&1 \
    && python3 -m pipx install --force "$DIR" \
    || python3 -m pip install --user "$DIR"
fi

cat <<'EOF'

[memnos] installed.

Prerequisite: a running PostgreSQL with the pgvector extension available
(the memnos user needs rights to CREATE EXTENSION vector + a database).

Next steps:
  memnos setup     # enter your Postgres connection — creates schema + an admin token
  memnos serve     # start the server, then open http://127.0.0.1:8900/admin

If 'memnos' isn't found, restart your shell (pipx PATH) or use: python3 -m memnos_cli
EOF
