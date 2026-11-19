: << 'WINDOWS_CMD'
@echo off
setlocal
set "REPO_ROOT=%~dp0.."
for %%I in ("%REPO_ROOT%") do set "REPO_ROOT=%%~fI"
set "USER_DIR=%USERPROFILE%"
if not defined USER_DIR if defined HOMEDRIVE if defined HOMEPATH set "USER_DIR=%HOMEDRIVE%%HOMEPATH%"
set "REMAP_ARGS="
if defined USER_DIR set "REMAP_ARGS=%REMAP_ARGS% --remap-path-prefix=%USER_DIR%=/build"
if defined HOME set "REMAP_ARGS=%REMAP_ARGS% --remap-path-prefix=%HOME%=/build"
if defined USER_DIR (
  set "REMAP_ARGS=%REMAP_ARGS% --remap-path-prefix=%USER_DIR%\.rustup=/toolchain --remap-path-prefix=%USER_DIR%\.cargo\registry=/deps --remap-path-prefix=%USER_DIR%\.cargo=/cargo"
)
if defined RUSTUP_HOME set "REMAP_ARGS=%REMAP_ARGS% --remap-path-prefix=%RUSTUP_HOME%=/toolchain"
if defined CARGO_HOME set "REMAP_ARGS=%REMAP_ARGS% --remap-path-prefix=%CARGO_HOME%\registry=/deps --remap-path-prefix=%CARGO_HOME%=/cargo"
set "REMAP_ARGS=%REMAP_ARGS% --remap-path-prefix=%REPO_ROOT%=. --remap-path-prefix=%REPO_ROOT%\rust\src=src --remap-path-prefix=rust\src=src --remap-path-prefix=rust/src=src"
%* %REMAP_ARGS%
exit /b %ERRORLEVEL%
WINDOWS_CMD

# POSIX /bin/sh branch (macOS & Linux via posix_spawnp / execvp)
RUSTC_BIN="$1"
shift
REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
HOME_DIR="${HOME:-}"
if [ -z "$HOME_DIR" ]; then
  UNAME_USR="$(id -un 2>/dev/null || true)"
  if [ -n "$UNAME_USR" ]; then
    HOME_DIR="$(eval printf '%s' "~$UNAME_USR" 2>/dev/null || true)"
  fi
fi
CARGO_DIR="${CARGO_HOME:-${HOME_DIR:+$HOME_DIR/.cargo}}"
RUSTUP_DIR="${RUSTUP_HOME:-${HOME_DIR:+$HOME_DIR/.rustup}}"

# Order general -> specific so rustc's last-matching --remap-path-prefix wins
set -- "$@" --remap-path-prefix=/Users=/u --remap-path-prefix=/home=/u
if [ -n "$HOME_DIR" ]; then
  set -- "$@" "--remap-path-prefix=${HOME_DIR}=/build"
fi
if [ -n "$RUSTUP_DIR" ]; then
  set -- "$@" "--remap-path-prefix=${RUSTUP_DIR}=/toolchain"
fi
if [ -n "$CARGO_DIR" ]; then
  set -- "$@" "--remap-path-prefix=${CARGO_DIR}/registry=/deps" "--remap-path-prefix=${CARGO_DIR}=/cargo"
fi
set -- "$@" "--remap-path-prefix=${REPO_ROOT}=." "--remap-path-prefix=${REPO_ROOT}/rust/src=src" "--remap-path-prefix=rust/src=src"
exec "$RUSTC_BIN" "$@"
