#!/usr/bin/env bash
# memnos installer (macOS / Linux). Installs the SINGLE memnos package (server + CLI +
# client) into its OWN isolated environment and puts the `memnos` command on your PATH —
# no manual wiring, and it never pollutes (or inherits the breakage of) your system Python.
# Prefers `uv` (fastest), falls back to `pipx`. PostgreSQL (with pgvector) is a PREREQUISITE;
# this does NOT install Postgres.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "Python 3.10+ is required. Install it and re-run."; exit 1; }

# 1. pick an isolated installer that actually WORKS. Prefer uv; else a working pipx (a stale
#    pipx launcher can exist on PATH but be broken, so test --version, don't just check it
#    exists); else bootstrap pipx.
if uv --version >/dev/null 2>&1; then
  INSTALLER="uv"
elif pipx --version >/dev/null 2>&1; then
  INSTALLER="pipx"
elif python3 -m pipx --version >/dev/null 2>&1; then
  INSTALLER="python3 -m pipx"
else
  echo "[memnos] installing pipx ..."
  python3 -m pip install --user --quiet pipx
  INSTALLER="python3 -m pipx"
fi

echo "[memnos] installing 'memnos' (isolated, via ${INSTALLER%% *}) ..."

if [ "$INSTALLER" = "uv" ]; then
  uv tool install --force "$DIR"
  uv tool update-shell >/dev/null 2>&1 || true
  BIN_DIR="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
else
  $INSTALLER ensurepath >/dev/null 2>&1 || true          # persist bin dir on PATH for future shells
  $INSTALLER install --force "$DIR"
  BIN_DIR="$($INSTALLER environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")"
fi

# make `memnos` usable in THIS shell immediately (clear any stale path hash), then verify
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) export PATH="$BIN_DIR:$PATH" ;; esac
hash -r 2>/dev/null || true

echo
if command -v memnos >/dev/null 2>&1 && memnos --help >/dev/null 2>&1; then
  echo "[memnos] ✓ installed and on PATH: $(command -v memnos)"
else
  echo "[memnos] installed to: $BIN_DIR/memnos"
  echo "[memnos] '$BIN_DIR' isn't active in THIS shell yet — open a NEW terminal,"
  echo "         or run:  hash -r; export PATH=\"$BIN_DIR:\$PATH\""
fi

cat <<EOF

Next steps (a PostgreSQL with the pgvector extension is a prerequisite — not installed for you):
  memnos setup     # enter your Postgres connection — creates the schema + an admin token
  memnos serve     # start the server, then open http://127.0.0.1:8900/admin

New terminals will have 'memnos' on PATH. If one doesn't, open a fresh terminal (the installer
already updated your shell profile).
EOF
