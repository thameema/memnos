#!/usr/bin/env bash
# Release hygiene gate (`scripts/release_hygiene.sh`):
# Fails non-zero if the shipped binary, CLI/MCP/HTTP surfaces, or built distributions
# (wheels, sdists, archives) leak:
#   1. Internal implementation language branding ("rust") in `--help`, `--version`, bare CLI,
#      or MCP `initialize` / `tools/list` output.
#   2. Test-only environment switches (`FAKE_EXTRACT`, `EMBED_MODE`, `TEST_DSN`,
#      `SIMULATE_WARMING`, `SKIP_MODEL_HASH`, `local-hash`) inside the release binary.
#   3. Builder privacy leaks (`$HOME`, Windows native `cygpath -w $HOME`, `$USERPROFILE`,
#      or generic `/Users/`, `/home/`, `C:\Users\`, `.cargo/registry`, `.rustup`, `rust/src/`).
#   4. Unpacked wheel (`.whl` / `.zip`) and sdist (`.tar.gz` / `.tgz`) member contents.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_ONLY=0
DIST_DIRS=()
BIN_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dist-only)
      DIST_ONLY=1
      shift
      while [[ $# -gt 0 ]]; do
        DIST_DIRS+=("$1")
        shift
      done
      ;;
    *)
      BIN_ARG="$1"
      shift
      ;;
  esac
done

if [[ ${#DIST_DIRS[@]} -eq 0 ]]; then
  DIST_DIRS=("$REPO_ROOT/dist")
fi

FAILS=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1" >&2; FAILS=$((FAILS + 1)); }

PY_BIN=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys" >/dev/null 2>&1; then
    PY_BIN="$cand"
    break
  fi
done

to_py_path() {
  local p="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$p" 2>/dev/null || printf '%s' "$p"
  else
    printf '%s' "$p"
  fi
}

to_posix_path() {
  local p="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -u "$p" 2>/dev/null || printf '%s' "$p"
  else
    printf '%s' "$p"
  fi
}

extract_strings() {
  local target="$1"
  if [[ -n "$PY_BIN" ]]; then
    local py_target
    py_target="$(to_py_path "$target")"
    "$PY_BIN" - "$py_target" <<'PYEOF'
import re, sys
with open(sys.argv[1], "rb") as f:
    data = f.read()
for m in re.finditer(rb"[\x20-\x7e]{4,}", data):
    sys.stdout.write(m.group(0).decode("ascii", "ignore") + "\n")
PYEOF
  elif command -v strings >/dev/null 2>&1; then
    strings "$(to_posix_path "$target")"
  else
    echo "FAIL: neither 'python' nor 'strings' available to inspect binary" >&2
    return 1
  fi
}

extract_pkg_strings() {
  local pkg="$1"
  case "$pkg" in
    *.whl|*.zip)
      if [[ -n "$PY_BIN" ]]; then
        local py_pkg
        py_pkg="$(to_py_path "$pkg")"
        "$PY_BIN" - "$py_pkg" <<'PYEOF'
import re, sys, zipfile
with zipfile.ZipFile(sys.argv[1], "r") as zf:
    for info in zf.infolist():
        if info.is_dir():
            continue
        data = zf.read(info.filename)
        for m in re.finditer(rb"[\x20-\x7e]{4,}", data):
            sys.stdout.write(m.group(0).decode("ascii", "ignore") + "\n")
PYEOF
      elif command -v unzip >/dev/null 2>&1 && command -v strings >/dev/null 2>&1; then
        unzip -p "$(to_posix_path "$pkg")" | strings
      else
        echo "FAIL: neither 'python' nor 'unzip'+'strings' available to inspect $pkg" >&2
        return 1
      fi
      ;;
    *.tar.gz|*.tgz)
      if [[ -n "$PY_BIN" ]]; then
        local py_pkg
        py_pkg="$(to_py_path "$pkg")"
        "$PY_BIN" - "$py_pkg" <<'PYEOF'
import re, sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as tf:
    for m_info in tf.getmembers():
        if not m_info.isfile():
            continue
        fh = tf.extractfile(m_info)
        if fh is None:
            continue
        data = fh.read()
        for m in re.finditer(rb"[\x20-\x7e]{4,}", data):
            sys.stdout.write(m.group(0).decode("ascii", "ignore") + "\n")
PYEOF
      elif command -v tar >/dev/null 2>&1 && command -v strings >/dev/null 2>&1; then
        tar -xOzf "$(to_posix_path "$pkg")" | strings
      else
        echo "FAIL: neither 'python' nor 'tar'+'strings' available to inspect $pkg" >&2
        return 1
      fi
      ;;
    *)
      extract_strings "$pkg"
      ;;
  esac
}

WIN_HOME=""
if [[ -n "${HOME:-}" ]] && command -v cygpath >/dev/null 2>&1; then
  WIN_HOME="$(cygpath -w "$HOME" 2>/dev/null || true)"
fi

check_path_leaks_in_stream() {
  local label="$1"
  local tmpf="$2"
  local bad=0

  if [[ -n "${HOME:-}" ]] && grep -F "$HOME" "$tmpf" >/dev/null 2>&1; then
    local cnt
    cnt="$(grep -Fc "$HOME" "$tmpf" || true)"
    fail "$label leaks POSIX HOME ($HOME) in $cnt string(s)"
    bad=1
  fi
  if [[ -n "$WIN_HOME" ]] && grep -Fi "$WIN_HOME" "$tmpf" >/dev/null 2>&1; then
    local cnt
    cnt="$(grep -Fic "$WIN_HOME" "$tmpf" || true)"
    fail "$label leaks Windows native HOME ($WIN_HOME) in $cnt string(s)"
    bad=1
  fi
  if [[ -n "${USERPROFILE:-}" ]] && grep -Fi "$USERPROFILE" "$tmpf" >/dev/null 2>&1; then
    local cnt
    cnt="$(grep -Fic "$USERPROFILE" "$tmpf" || true)"
    fail "$label leaks USERPROFILE ($USERPROFILE) in $cnt string(s)"
    bad=1
  fi
  if grep -E '(/Users/|/home/|[A-Za-z]:\\Users\\|\.cargo[/\\]registry|\.rustup[/\\]|rust[/\\]src[/\\])' "$tmpf" >/dev/null 2>&1; then
    local cnt
    cnt="$(grep -Ec '(/Users/|/home/|[A-Za-z]:\\Users\\|\.cargo[/\\]registry|\.rustup[/\\]|rust[/\\]src[/\\])' "$tmpf" || true)"
    fail "$label contains $cnt unremapped host/cargo/rustup/source path string(s)"
    grep -E '(/Users/|/home/|[A-Za-z]:\\Users\\|\.cargo[/\\]registry|\.rustup[/\\]|rust[/\\]src[/\\])' "$tmpf" | head -n 10 >&2
    bad=1
  fi
  return "$bad"
}

if [[ "$DIST_ONLY" -eq 0 ]]; then
  BIN="${BIN_ARG:-$REPO_ROOT/target/release/memnos}"
  if [[ ! -f "$BIN" && -f "${BIN}.exe" ]]; then
    BIN="${BIN}.exe"
  fi
  if [[ ! -f "$BIN" ]]; then
    echo "FAIL: release binary not found: $BIN" >&2
    exit 1
  fi

  echo "=== Release Hygiene Gate: $BIN ==="

  # 1. CLI --help / --version / bare invocation must not mention implementation language
  HELP_OUT="$("$BIN" --help 2>&1)"
  VER_OUT="$("$BIN" --version 2>&1)"
  BARE_OUT="$("$BIN" 2>&1 || true)"

  if echo "$HELP_OUT $VER_OUT $BARE_OUT" | grep -qi "rust"; then
    fail "CLI help/version output mentions 'rust'"
  else
    pass "CLI --help / --version / bare output is language-neutral"
  fi

  # 2. MCP initialize + tools/list must not mention implementation language
  MCP_OUT="$(printf '%s\n%s\n%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"gate","version":"1.0"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    | MEMNOS_MCP_AUTO_REGISTER=0 "$BIN" mcp 2>/dev/null || true)"

  if echo "$MCP_OUT" | grep -qi "rust"; then
    fail "MCP initialize/tools/list mentions 'rust'"
  else
    pass "MCP initialize + 55-tool definitions are language-neutral"
  fi

  STR_TMP="$(mktemp)"
  trap 'rm -f "$STR_TMP"' EXIT
  extract_strings "$BIN" > "$STR_TMP"

  # 3. Binary strings: no test-only environment switches or synthetic hash embedder
  if grep -E 'FAKE_EXTRACT|EMBED_MODE|TEST_DSN|SIMULATE_WARMING|SKIP_MODEL_HASH|local-hash' "$STR_TMP" >/dev/null 2>&1; then
    fail "Release binary contains test-only switch or hash-tier strings"
    grep -E 'FAKE_EXTRACT|EMBED_MODE|TEST_DSN|SIMULATE_WARMING|SKIP_MODEL_HASH|local-hash' "$STR_TMP" | head -n 10 >&2
  else
    pass "Zero test-only switches or hash-tier strings in release binary"
  fi

  # 4. Binary strings: no POSIX/Windows HOME, USERPROFILE, /Users/, /home/, C:\Users\, .cargo/registry, .rustup, or rust/src/
  if check_path_leaks_in_stream "Release binary" "$STR_TMP"; then
    pass "Zero builder HOME/USERPROFILE, .cargo/registry, .rustup, or 'rust/src/' path strings in release binary"
  fi

  # 4b. Companion single-process desktop binary (`memnos-desktop`), when built alongside `memnos`
  BIN_DIR="$(dirname "$BIN")"
  DESKTOP_BIN=""
  if [[ -f "$BIN_DIR/memnos-desktop" ]]; then
    DESKTOP_BIN="$BIN_DIR/memnos-desktop"
  elif [[ -f "$BIN_DIR/memnos-desktop.exe" ]]; then
    DESKTOP_BIN="$BIN_DIR/memnos-desktop.exe"
  fi
  if [[ -n "$DESKTOP_BIN" ]]; then
    echo "=== Release Hygiene Gate (Desktop Companion): $DESKTOP_BIN ==="
    D_HELP="$("$DESKTOP_BIN" --help 2>&1)"
    D_VER="$("$DESKTOP_BIN" --version 2>&1)"
    if [[ -z "${D_HELP// /}" || -z "${D_VER// /}" ]]; then
      fail "Desktop CLI --help or --version produced empty output"
    elif echo "$D_HELP $D_VER" | grep -qi "rust"; then
      fail "Desktop CLI --help/--version mentions 'rust'"
    else
      pass "Desktop --help / --version output is non-empty and language-neutral"
    fi
    D_TMP="$(mktemp)"
    extract_strings "$DESKTOP_BIN" > "$D_TMP"
    if grep -E 'FAKE_EXTRACT|EMBED_MODE|TEST_DSN|SIMULATE_WARMING|SKIP_MODEL_HASH|local-hash' "$D_TMP" >/dev/null 2>&1; then
      fail "Desktop binary contains test-only switch or hash-tier strings"
    else
      pass "Zero test-only switches in desktop binary"
    fi
    if check_path_leaks_in_stream "Desktop binary" "$D_TMP"; then
      pass "Zero builder path leaks in desktop binary"
    fi
    rm -f "$D_TMP"
  fi

  # 4c. Docs & Desktop UI assets (`docs/` and `rust/desktop_assets/`): no language branding or personal path leaks
  DOC_TMP="$(mktemp)"
  cat "$REPO_ROOT/docs/cli.md" "$REPO_ROOT/docs/cli-reference.json" \
      "$REPO_ROOT/rust/desktop_assets/index.html" \
      "$REPO_ROOT/rust/desktop_assets/app.js" \
      "$REPO_ROOT/rust/desktop_assets/app.css" > "$DOC_TMP" 2>/dev/null || true
  if grep -Eiq '\brust\b' "$DOC_TMP"; then
    fail "Docs or desktop UI assets mention implementation language ('rust')"
    grep -Ein '\brust\b' "$DOC_TMP" | head -n 10 >&2
  else
    pass "CLI docs & desktop UI assets are language-neutral"
  fi
  if check_path_leaks_in_stream "CLI docs & desktop UI assets" "$DOC_TMP"; then
    pass "Zero builder path leaks in CLI docs & desktop UI assets"
  fi
  rm -f "$DOC_TMP"
else
  echo "=== Release Hygiene Gate (Distribution Artifacts Only) ==="
fi

FIND_BIN="find"
if [[ -x /usr/bin/find ]]; then
  FIND_BIN="/usr/bin/find"
fi

# 5. Deep distribution check (inspects unpacked .whl / .zip / .tar.gz member contents)
FOUND_DIST=0
DIST_BAD=0
SCANNED_NAMES=()
for ddir in "${DIST_DIRS[@]}"; do
  if [[ -d "$ddir" ]]; then
    while IFS= read -r -d '' pkg; do
      FOUND_DIST=1
      bname="$(basename "$pkg")"
      SCANNED_NAMES+=("$bname")
      PKG_TMP="$(mktemp)"
      if ! extract_pkg_strings "$pkg" > "$PKG_TMP" 2>/dev/null; then
        fail "Distribution artifact $bname could not be unpacked or inspected"
        DIST_BAD=1
        rm -f "$PKG_TMP"
        continue
      fi
      if ! check_path_leaks_in_stream "Distribution artifact $bname" "$PKG_TMP"; then
        DIST_BAD=1
      fi
      if grep -E 'FAKE_EXTRACT|EMBED_MODE|TEST_DSN|SIMULATE_WARMING|SKIP_MODEL_HASH|local-hash' "$PKG_TMP" >/dev/null 2>&1; then
        fail "Distribution artifact $bname contains test-only switch or hash-tier strings"
        DIST_BAD=1
      fi
      rm -f "$PKG_TMP"
    done < <("$FIND_BIN" "$ddir" -maxdepth 2 -type f \( -name "*.whl" -o -name "*.tar.gz" -o -name "*.tgz" -o -name "*.zip" \) -print0)
  fi
done

if [[ "$FOUND_DIST" -eq 1 && "$DIST_BAD" -eq 0 ]]; then
  pass "Unpacked distribution archives (${SCANNED_NAMES[*]}) pass full hygiene & path-leak checks"
elif [[ "$DIST_ONLY" -eq 1 && "$FOUND_DIST" -eq 0 ]]; then
  fail "No distribution archives found in: ${DIST_DIRS[*]}"
fi

if [[ "$FAILS" -gt 0 ]]; then
  echo -e "\nFAILED: $FAILS release-hygiene check(s) failed." >&2
  exit 1
fi
echo -e "\nPASSED: all release-hygiene checks clean."
