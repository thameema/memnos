#!/usr/bin/env bash
# memnos installer (macOS / Linux). Installs the SINGLE memnos package (server + CLI +
# client) as an isolated app and guarantees the `memnos` command is on your PATH for every
# new terminal — no manual wiring. PostgreSQL (with pgvector) is a PREREQUISITE; this does
# NOT install Postgres.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "Python 3.10+ is required. Install it and re-run."; exit 1; }

echo "[memnos] installing 'memnos' (server + CLI + client, isolated via pipx) ..."

# 1. ensure pipx — the standard cross-OS way to put a Python CLI app on PATH in its own env
if ! command -v pipx >/dev/null 2>&1 && ! python3 -m pipx --version >/dev/null 2>&1; then
  echo "[memnos] installing pipx ..."
  python3 -m pip install --user --quiet pipx
fi
PIPX="pipx"; command -v pipx >/dev/null 2>&1 || PIPX="python3 -m pipx"

# 2. put pipx's bin dir on PATH for FUTURE shells (writes to your shell profile)
$PIPX ensurepath >/dev/null 2>&1 || true

# 3. install the package (creates the `memnos` console command)
$PIPX install --force "$DIR"

# 4. make `memnos` usable in THIS shell immediately, then verify it resolves
BIN_DIR="$($PIPX environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) export PATH="$BIN_DIR:$PATH" ;; esac

echo
if command -v memnos >/dev/null 2>&1 && memnos --help >/dev/null 2>&1; then
  echo "[memnos] ✓ installed and on PATH: $(command -v memnos)"
else
  echo "[memnos] installed to: $BIN_DIR/memnos"
  echo "[memnos] '$BIN_DIR' isn't active in THIS shell yet — open a NEW terminal,"
  echo "         or run:  export PATH=\"$BIN_DIR:\$PATH\""
fi

cat <<EOF

Next steps (a PostgreSQL with the pgvector extension is a prerequisite — not installed for you):
  memnos setup     # enter your Postgres connection — creates the schema + an admin token
  memnos serve     # start the server, then open http://127.0.0.1:8900/admin

Every new terminal will have 'memnos' on PATH. If one doesn't, run '$PIPX ensurepath' and reopen it.
EOF
