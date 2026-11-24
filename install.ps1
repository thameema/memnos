# memnos native single-binary installer (Windows PowerShell — zero Python required).
#
# Usage:
#   irm https://raw.githubusercontent.com/thameema/memnos/master/install.ps1 | iex
$ErrorActionPreference = "Stop"

$repo = if ($env:MEMNOS_GITHUB_REPO) { $env:MEMNOS_GITHUB_REPO } else { "thameema/memnos" }
$binDir = if ($env:MEMNOS_INSTALL_DIR) { $env:MEMNOS_INSTALL_DIR } else { Join-Path $env:USERPROFILE ".local\bin" }
New-Item -ItemType Directory -Force -Path $binDir | Out-Null

$existingExe = Join-Path $binDir "memnos.exe"
$existingCfg = Join-Path $env:USERPROFILE ".memnos\config.json"
if ((Test-Path $existingExe) -and (Test-Path $existingCfg) -and (-not $env:MEMNOS_CI) -and (-not $env:CI)) {
    Write-Host "[memnos] taking pre-upgrade database & config snapshot ..."
    try { & $existingExe backup | Out-Null } catch {}
}

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

$slShim = Join-Path $binDir "statusline.cmd"
if ((-not (Test-Path $slShim)) -or ((Get-Content $slShim -Raw -ErrorAction SilentlyContinue) -match "memnos")) {
    Set-Content -Path $slShim -Value "@echo off`r`n`"$binDir\memnos.exe`" hook statusline %*`r`n" -Encoding ASCII
}

$ver = & (Join-Path $binDir "memnos.exe") --version
Write-Host "[memnos] OK installed: $ver ($binDir\memnos.exe)"

$cfgPath = Join-Path $env:USERPROFILE ".memnos\config.json"
if ((Test-Path $cfgPath) -and (-not $env:MEMNOS_CI) -and (-not $env:CI)) {
    Write-Host "[memnos] auto-discovering & wiring installed AI harnesses (MCP, hooks, statusLine) ..."
    try { & (Join-Path $binDir "memnos.exe") agent-setup all --namespace auto --force } catch {}
    $gwState = Join-Path $env:USERPROFILE ".memnos\gateway_state.json"
    if (Test-Path $gwState) {
        try { & (Join-Path $binDir "memnos.exe") restart } catch {}
    }
} else {
    Write-Host @"

Next steps:
  memnos setup     # configure PostgreSQL+pgvector & auto-wire all installed AI harnesses
  memnos start     # start the background server on http://127.0.0.1:8900
  memnos admin     # bootstrap or inspect your admin token
  memnos app       # launch the native MEMNOS Desktop workspace
"@
}
