#!/usr/bin/env bash
# Thin wrapper around `cargo build --release`. Path remapping and symbol stripping
# are enforced unconditionally on every build path via `.cargo/config.toml`
# (`scripts/rustc-remap.cmd`) and `[profile.release]` in `Cargo.toml`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
exec cargo build --release "$@"
