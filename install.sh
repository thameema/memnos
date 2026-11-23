#!/usr/bin/env bash
# memnos native single-binary installer (macOS & Linux — zero Python required).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/thameema/memnos/master/install.sh | sh
#   MEMNOS_VERSION=v0.2.0 curl -fsSL https://raw.githubusercontent.com/thameema/memnos/master/install.sh | sh
#   ./install.sh --local ./target/release
set -eu

REPO="${MEMNOS_GITHUB_REPO:-thameema/memnos}"
BIN_DIR="${MEMNOS_INSTALL_DIR:-$HOME/.local/bin}"
LOCAL_BIN_SRC=""

while [ $# -gt 0 ]; do
  case "$1" in
    --local)
      LOCAL_BIN_SRC="$2"
      shift 2
      ;;
    *)
      echo "[memnos] unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

mkdir -p "$BIN_DIR"

install_atomic() {
  src="$1"
  dst="$2"
  tmp="${dst}.tmp.$$"
  install -m 755 "$src" "$tmp"
  mv -f "$tmp" "$dst"
}

if [ -n "$LOCAL_BIN_SRC" ]; then
  echo "[memnos] installing local binaries from $LOCAL_BIN_SRC -> $BIN_DIR ..."
  install_atomic "$LOCAL_BIN_SRC/memnos" "$BIN_DIR/memnos"
  if [ -f "$LOCAL_BIN_SRC/memnos-desktop" ]; then
    install_atomic "$LOCAL_BIN_SRC/memnos-desktop" "$BIN_DIR/memnos-desktop"
  fi
else
  OS="$(uname -s)"
  ARCH="$(uname -m)"
  case "$OS:$ARCH" in
    Darwin:arm64|Darwin:aarch64) TARGET="darwin-arm64" ;;
    Linux:x86_64|Linux:amd64)    TARGET="linux-amd64" ;;
    *)
      echo "[memnos] prebuilt archive not available for $OS/$ARCH — install via 'uv tool install memnos' or build from source." >&2
      exit 1
      ;;
  esac

  ARCHIVE="memnos-${TARGET}.tar.gz"
  if [ -n "${MEMNOS_VERSION:-}" ]; then
    TAG="$MEMNOS_VERSION"
    case "$TAG" in v*) ;; *) TAG="v$TAG" ;; esac
    URL="https://github.com/${REPO}/releases/download/${TAG}/${ARCHIVE}"
  else
    URL="https://github.com/${REPO}/releases/latest/download/${ARCHIVE}"
  fi

  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT INT TERM

  echo "[memnos] downloading ${ARCHIVE} ..."
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$TMP_DIR/$ARCHIVE"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TMP_DIR/$ARCHIVE" "$URL"
  else
    echo "[memnos] error: curl or wget is required." >&2
    exit 1
  fi

  tar -xzf "$TMP_DIR/$ARCHIVE" -C "$TMP_DIR"
  install_atomic "$TMP_DIR/memnos" "$BIN_DIR/memnos"
  if [ -f "$TMP_DIR/memnos-desktop" ]; then
    install_atomic "$TMP_DIR/memnos-desktop" "$BIN_DIR/memnos-desktop"
  fi
fi

# Ensure BIN_DIR is on PATH for future shells
case ":${PATH:-}:" in
  *":$BIN_DIR:"*) ;;
  *)
    EXPORT_LINE="export PATH=\"$BIN_DIR:\$PATH\""
    for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
      if [ -f "$rc" ] && ! grep -F "$BIN_DIR" "$rc" >/dev/null 2>&1; then
        printf '\n# memnos\n%s\n' "$EXPORT_LINE" >> "$rc"
      fi
    done
    export PATH="$BIN_DIR:${PATH:-}"
    ;;
esac

echo "[memnos] ✓ installed: $("$BIN_DIR/memnos" --version)"
cat <<EOF

Next steps:
  memnos setup     # configure embedded PostgreSQL+pgvector (zero dependencies) or your own Postgres
  memnos start     # start the background server & zero-downtime gateway on http://127.0.0.1:8900
  memnos admin     # bootstrap or inspect your admin token
  memnos app       # launch the native MEMNOS Desktop workspace
EOF
