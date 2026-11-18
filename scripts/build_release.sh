#!/usr/bin/env bash
# Build the stripped, path-remapped `memnos` release binary so no builder `$HOME`,
# username, cargo/rustup directory, or internal `rust/src/` prefix is embedded in the binary.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CARGO_DIR="${CARGO_HOME:-$HOME/.cargo}"
RUSTUP_DIR="${RUSTUP_HOME:-$HOME/.rustup}"

REMAP_FLAGS=(
  "--remap-path-prefix=${HOME}=/build"
  "--remap-path-prefix=${CARGO_DIR}=/cargo"
  "--remap-path-prefix=${RUSTUP_DIR}=/toolchain"
  "--remap-path-prefix=${REPO_ROOT}=."
  "--remap-path-prefix=${REPO_ROOT}/rust/src=src"
  "--remap-path-prefix=rust/src=src"
)

export RUSTFLAGS="${REMAP_FLAGS[*]} ${RUSTFLAGS:-}"
cargo build --release "$@"
