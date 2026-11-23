#!/usr/bin/env bash
# Supported cross-OS release build (`scripts/build_release.sh`) for macOS, Linux, and Windows.
# Strips symbols (`[profile.release]` in Cargo.toml) and remaps all POSIX & Windows native
# builder paths (`HOME`, `USERPROFILE`, `cygpath -w`, `.rustup`, `.cargo/registry`, workspace)
# via `RUSTFLAGS` (general -> specific so rustc's last-matching rule wins).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HOME_DIR="${HOME:-}"
if [[ -z "$HOME_DIR" ]] && command -v id >/dev/null 2>&1; then
  UNAME_USR="$(id -un 2>/dev/null || true)"
  if [[ -n "$UNAME_USR" ]]; then
    HOME_DIR="$(eval printf '%s' "~$UNAME_USR" 2>/dev/null || true)"
  fi
fi

CARGO_DIR="${CARGO_HOME:-${HOME_DIR:+$HOME_DIR/.cargo}}"
RUSTUP_DIR="${RUSTUP_HOME:-${HOME_DIR:+$HOME_DIR/.rustup}}"

REMAP_FLAGS=()
add_remap() {
  local from="$1"
  local to="$2"
  if [[ -n "$from" ]]; then
    REMAP_FLAGS+=("--remap-path-prefix=${from}=${to}")
  fi
}

# 1. General home/user roots first (POSIX + Windows native forms)
add_remap "/Users" "/u"
add_remap "/home" "/u"
add_remap "$HOME_DIR" "/build"
add_remap "${USERPROFILE:-}" "/build"

if command -v cygpath >/dev/null 2>&1; then
  [[ -n "$HOME_DIR" ]] && add_remap "$(cygpath -w "$HOME_DIR" 2>/dev/null || true)" "/build"
  [[ -n "$RUSTUP_DIR" ]] && add_remap "$(cygpath -w "$RUSTUP_DIR" 2>/dev/null || true)" "/toolchain"
  if [[ -n "$CARGO_DIR" ]]; then
    WIN_CARGO="$(cygpath -w "$CARGO_DIR" 2>/dev/null || true)"
    [[ -n "$WIN_CARGO" ]] && add_remap "${WIN_CARGO}\\registry" "/deps"
    add_remap "$WIN_CARGO" "/cargo"
  fi
  WIN_REPO="$(cygpath -w "$REPO_ROOT" 2>/dev/null || true)"
  [[ -n "$WIN_REPO" ]] && add_remap "$WIN_REPO" "."
  [[ -n "$WIN_REPO" ]] && add_remap "${WIN_REPO}\\rust\\src" "src"
fi

if [[ -n "${USERPROFILE:-}" ]]; then
  add_remap "${USERPROFILE}\\.rustup" "/toolchain"
  add_remap "${USERPROFILE}\\.cargo\\registry" "/deps"
  add_remap "${USERPROFILE}\\.cargo" "/cargo"
fi

# 2. Specific toolchain, cargo registry, and workspace prefixes last (wins over HOME)
add_remap "$RUSTUP_DIR" "/toolchain"
[[ -n "$CARGO_DIR" ]] && add_remap "${CARGO_DIR}/registry" "/deps"
add_remap "$CARGO_DIR" "/cargo"
add_remap "$REPO_ROOT" "."
add_remap "${REPO_ROOT}/rust/src" "src"
add_remap "rust\\src" "src"
add_remap "rust/src" "src"

IFS=$'\x1f'
ENCODED_REMAP="${REMAP_FLAGS[*]}"
unset IFS
if [[ -n "${CARGO_ENCODED_RUSTFLAGS:-}" ]]; then
  export CARGO_ENCODED_RUSTFLAGS="${ENCODED_REMAP}"$'\x1f'"${CARGO_ENCODED_RUSTFLAGS}"
else
  export CARGO_ENCODED_RUSTFLAGS="${ENCODED_REMAP}"
fi
exec cargo build --release "$@"
