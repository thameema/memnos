# memnos native single-binary installer (Windows PowerShell — zero Python required).
#
# Usage:
#   irm https://raw.githubusercontent.com/thameema/memnos/master/install.ps1 | iex
$ErrorActionPreference = "Stop"

$repo = if ($env:MEMNOS_GITHUB_REPO) { $env:MEMNOS_GITHUB_REPO } else { "thameema/memnos" }
$binDir = if ($env:MEMNOS_INSTALL_DIR) { $env:MEMNOS_INSTALL_DIR } else { Join-Path $env:USERPROFILE ".local\bin" }
New-Item -ItemType Directory -Force -Path $binDir | Out-Null

$archive = "memnos-windows-amd64.zip"
if ($env:MEMNOS_VERSION) {
    $tag = if ($env:MEMNOS_VERSION.StartsWith("v")) { $env:MEMNOS_VERSION } else { "v$($env:MEMNOS_VERSION)" }
    $url = "https://github.com/$repo/releases/download/$tag/$archive"
} else {
    $url = "https://github.com/$repo/releases/latest/download/$archive"
}

$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("memnos-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
try {
    $zipPath = Join-Path $tmpDir $archive
    Write-Host "[memnos] downloading $archive ..."
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $tmpDir -Force

    Copy-Item -Path (Join-Path $tmpDir "memnos.exe") -Destination (Join-Path $binDir "memnos.exe") -Force
    $desktopExe = Join-Path $tmpDir "memnos-desktop.exe"
    if (Test-Path $desktopExe) {
        Copy-Item -Path $desktopExe -Destination (Join-Path $binDir "memnos-desktop.exe") -Force
    }
} finally {
    Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (($userPath -split ";") -notcontains $binDir) {
    $newUserPath = if ($userPath) { "$binDir;$userPath" } else { $binDir }
    [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
}
if (($env:PATH -split ";") -notcontains $binDir) {
    $env:PATH = "$binDir;$env:PATH"
}

$ver = & (Join-Path $binDir "memnos.exe") --version
Write-Host "[memnos] OK installed: $ver ($binDir\memnos.exe)"
Write-Host @"

Next steps:
  memnos setup     # configure embedded PostgreSQL+pgvector (zero dependencies) or your own Postgres
  memnos start     # start the background server on http://127.0.0.1:8900
  memnos admin     # bootstrap or inspect your admin token
  memnos app       # launch the native MEMNOS Desktop workspace
"@
