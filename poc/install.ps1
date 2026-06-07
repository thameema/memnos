# memnos installer (Windows / PowerShell). Postgres is a PREREQUISITE — this does NOT install it.
# Installs the `memnos` CLI+server (one package) and points you at the setup wizard.
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python 3.10+ is required. Install it (python.org) and re-run."
    exit 1
}

Write-Host "[memnos] installing the 'memnos' command (CLI + server) ..."
if (Get-Command pipx -ErrorAction SilentlyContinue) {
    pipx install --force $dir
} else {
    python -m pip install --user pipx
    python -m pipx ensurepath
    python -m pipx install --force $dir
}

Write-Host @"

[memnos] installed.

Prerequisite: a running PostgreSQL with the pgvector extension available
(the memnos user needs rights to CREATE EXTENSION vector + a database).

Next steps:
  memnos setup     # enter your Postgres connection - creates schema + an admin token
  memnos serve     # start the server, then open http://127.0.0.1:8900/admin

If 'memnos' isn't found, restart your shell (pipx PATH) or use: python -m memnos_cli
"@
