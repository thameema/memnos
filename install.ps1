# memnos installer (Windows / PowerShell). Installs the SINGLE memnos package (server +
# CLI + client) as an isolated app and guarantees the `memnos` command is on your PATH for
# every new terminal. PostgreSQL (with pgvector) is a PREREQUISITE; this does NOT install it.
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python 3.10+ is required. Install it from python.org (check 'Add Python to PATH') and re-run."
    exit 1
}

Write-Host "[memnos] installing 'memnos' (server + CLI + client, isolated via pipx) ..."

# 1. ensure pipx
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    try { python -m pipx --version | Out-Null } catch { python -m pip install --user --quiet pipx }
}

# 2. put pipx's bin dir on PATH for FUTURE terminals (persists to user PATH), then install
python -m pipx ensurepath | Out-Null
python -m pipx install --force $dir

# 3. make `memnos` usable in THIS session + verify
$binDir = (python -m pipx environment --value PIPX_BIN_DIR 2>$null)
if (-not $binDir) { $binDir = Join-Path $env:USERPROFILE ".local\bin" }
if (($env:PATH -split ';') -notcontains $binDir) { $env:PATH = "$binDir;$env:PATH" }

Write-Host ""
if (Get-Command memnos -ErrorAction SilentlyContinue) {
    Write-Host "[memnos] OK installed and on PATH: $((Get-Command memnos).Source)"
} else {
    Write-Host "[memnos] installed to: $binDir\memnos.exe"
    Write-Host "[memnos] Open a NEW terminal so PATH refreshes (pipx ensurepath updated it)."
}

Write-Host @"

Next steps (a PostgreSQL with the pgvector extension is a prerequisite - not installed for you):
  memnos setup     # enter your Postgres connection - creates the schema + an admin token
  memnos start     # start the server, then open http://127.0.0.1:8900/admin

Every new terminal will have 'memnos' on PATH.
"@
