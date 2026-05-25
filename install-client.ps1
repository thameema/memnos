# engram client installer for Windows (PowerShell)
#
# Installs Claude Code automation hooks on Windows.
# Works whether engram runs locally or on a remote server.
#
# Usage:
#   .\install-client.ps1
#   .\install-client.ps1 -Server http://host:8766 -Key engram-abc123
#   .\install-client.ps1 -Server http://localhost:8766 -Key engram-abc123 -Namespace "personal:me"
#
# Requirements: PowerShell 5.1+ (Windows 10/11 built-in) or PowerShell 7+
#               git for Windows, Claude Code for Windows

[CmdletBinding()]
param(
    [string]$Server    = "",
    [string]$Key       = "",
    [string]$Namespace = ""
)

$ErrorActionPreference = "Stop"

# ─── Colors / helpers ─────────────────────────────────────────────────────────
function Write-Info    { param($msg) Write-Host "  --> $msg" -ForegroundColor Cyan }
function Write-Success { param($msg) Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Warn    { param($msg) Write-Host "  [!] $msg"  -ForegroundColor Yellow }
function Write-Step    { param($msg) Write-Host ""; Write-Host ">>> $msg" -ForegroundColor White }
function Read-Input {
    param($Prompt, $Default = "")
    if ($Default) { $shown = "$Prompt [$Default]: " } else { $shown = "$Prompt: " }
    $val = Read-Host $shown
    if ([string]::IsNullOrWhiteSpace($val) -and $Default) { return $Default }
    return $val
}
function Read-YN {
    param($Prompt, $Default = "Y")
    $val = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($val)) { $val = $Default }
    return ($val -imatch '^y')
}

# ─── Banner ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "   ___  ____   ___  ____   ____  __  __ " -ForegroundColor Blue
Write-Host "  / __)(  _ \ / __)(  _ \ / _  \(  \/  )" -ForegroundColor Blue
Write-Host " ( (__  )   /( (__  )   // /_\ / )    / " -ForegroundColor Blue
Write-Host "  \___)(_)\_) \___)(____/ \___/ (_/\/\_) " -ForegroundColor Blue
Write-Host "  Client Installer  (Claude Code hooks)" -ForegroundColor Blue
Write-Host ""

# ─── Paths ────────────────────────────────────────────────────────────────────
$ClaudeDir      = Join-Path $env:USERPROFILE ".claude"
$HooksDir       = Join-Path $ClaudeDir "hooks"
$CommandsDir    = Join-Path $ClaudeDir "commands"
$GitHooksDir    = Join-Path $env:USERPROFILE ".git-hooks"
$SettingsFile   = Join-Path $ClaudeDir "settings.json"

# ─── Check Claude Code ────────────────────────────────────────────────────────
Write-Step "Checking Claude Code"
if (Test-Path $SettingsFile) {
    Write-Success "Claude Code found: $SettingsFile"
} else {
    Write-Warn "$SettingsFile not found."
    Write-Warn "Install Claude Code first: https://claude.ai/code"
    Write-Warn "Hooks will be installed but not auto-registered."
    $SettingsFile = $null
}

# ─── Collect config ───────────────────────────────────────────────────────────
Write-Step "engram connection"

$EngramServer = if ($Server) { $Server } else { Read-Input "engram server URL" "http://localhost:8766" }
$EngramServer = $EngramServer.TrimEnd("/")

$EngramKey = if ($Key) { $Key } else { Read-Input "engram API key" }
if ([string]::IsNullOrWhiteSpace($EngramKey)) {
    throw "API key required. Get it from the server's .env file."
}

$DefaultNS = if ($Namespace) { $Namespace } else { Read-Input "Default namespace" "personal:me" }

# ─── Test connection ──────────────────────────────────────────────────────────
Write-Step "Testing server connection"
try {
    $null = Invoke-RestMethod "$EngramServer/api/v1/admin/health" `
        -Headers @{"X-API-Key" = $EngramKey} -TimeoutSec 5
    Write-Success "Connected to engram at $EngramServer"
} catch {
    Write-Warn "Could not reach $EngramServer — hooks will still be installed."
    Write-Warn "Hooks fail silently when server is unreachable."
}

# ─── Create directories ───────────────────────────────────────────────────────
Write-Step "Creating directories"
@($HooksDir, $CommandsDir, $GitHooksDir) | ForEach-Object {
    if (-not (Test-Path $_)) { New-Item -ItemType Directory -Path $_ | Out-Null }
}
Write-Success "Directories ready"

# ─── Write engram.env ─────────────────────────────────────────────────────────
Write-Step "Writing hook config"
$EnvContent = @"
# engram hook config — edit to change server, key, or default namespace.
ENGRAM_API=$EngramServer
ENGRAM_KEY=$EngramKey
ENGRAM_DEFAULT_NS=$DefaultNS
ENGRAM_TOP_K=5
"@
$EnvFile = Join-Path $HooksDir "engram.env"
Set-Content -Path $EnvFile -Value $EnvContent -Encoding UTF8
Write-Success "Config: $EnvFile"

# ─── Copy PowerShell hook scripts from repo (or download them) ────────────────
Write-Step "Installing PowerShell hook scripts"

# Find source directory (where this script lives)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WinHooksSource = Join-Path $ScriptDir "hooks\windows"

$HookFiles = @("engram-inject.ps1", "engram-session-write.ps1")

foreach ($hookFile in $HookFiles) {
    $dest = Join-Path $HooksDir $hookFile
    $src  = Join-Path $WinHooksSource $hookFile

    if (Test-Path $src) {
        Copy-Item $src $dest -Force
        Write-Success "Copied: $dest"
    } else {
        # Fallback: download from GitHub
        $url = "https://raw.githubusercontent.com/thameema/engram/main/hooks/windows/$hookFile"
        try {
            Invoke-WebRequest $url -OutFile $dest -UseBasicParsing
            Write-Success "Downloaded: $dest"
        } catch {
            Write-Warn "Could not install $hookFile — download manually from $url"
        }
    }
}

# ─── Install global git post-commit hook ──────────────────────────────────────
Write-Step "Installing global git post-commit hook"

$GitCommitSrc  = Join-Path $WinHooksSource "post-commit.ps1"
$GitCommitDest = Join-Path $GitHooksDir "post-commit.ps1"

if (Test-Path $GitCommitSrc) {
    Copy-Item $GitCommitSrc $GitCommitDest -Force
    Write-Success "Copied: $GitCommitDest"
}

# Wrapper .bat that PowerShell invokes (git calls post-commit, not post-commit.ps1)
$WrapperContent = "@echo off`r`npowershell.exe -NonInteractive -NoProfile -File `"$GitCommitDest`"`r`n"
$WrapperDest = Join-Path $GitHooksDir "post-commit"
Set-Content -Path $WrapperDest -Value $WrapperContent -Encoding ASCII

# Set global hooks path
git config --global core.hooksPath $GitHooksDir
Write-Success "git config --global core.hooksPath $GitHooksDir"

# ─── Install /engram slash command ────────────────────────────────────────────
Write-Step "Installing /engram slash command"
$SlashCmd = @'
Run these commands immediately and format results as shown.

```powershell
$env:ENGRAM_KEY = (Get-Content "$env:USERPROFILE\.claude\hooks\engram.env" | Where-Object { $_ -match '^ENGRAM_KEY=' }) -replace '^ENGRAM_KEY=',''
$env:ENGRAM_API = (Get-Content "$env:USERPROFILE\.claude\hooks\engram.env" | Where-Object { $_ -match '^ENGRAM_API=' }) -replace '^ENGRAM_API=',''
$env:ENGRAM_NS  = (Get-Content "$env:USERPROFILE\.claude\hooks\engram.env" | Where-Object { $_ -match '^ENGRAM_DEFAULT_NS=' }) -replace '^ENGRAM_DEFAULT_NS=',''

try { $ns = Invoke-RestMethod "$env:ENGRAM_API/api/v1/admin/namespaces" -Headers @{"X-API-Key"=$env:ENGRAM_KEY} } catch { $ns = @() }
$ns | ForEach-Object { $_.name }

$repoRoot = git rev-parse --show-toplevel 2>$null
if ($repoRoot -and (Test-Path (Join-Path $repoRoot ".engram"))) {
    $line = Get-Content (Join-Path $repoRoot ".engram") | Where-Object { $_ -match '^namespace=' }
    "source:file"; ($line -split '=',2)[1]
} else { "source:default"; $env:ENGRAM_NS }

$search = Invoke-RestMethod "$env:ENGRAM_API/api/v1/memory/search?q=session+commit+work&ns=$env:ENGRAM_NS&top_k=5" -Headers @{"X-API-Key"=$env:ENGRAM_KEY}
$search | ForEach-Object { "[$($_.memory_type)] $([math]::Round($_.score,2)) — $($_.content.Substring(0,[math]::Min(120,$_.content.Length)))" }
```

Show:
**engram status**
- **Namespaces** — bullet list
- **Active namespace** — name + how resolved (.engram file / default)
  If $ARGUMENTS contains `ns:something`: show `"namespace=something" | Set-Content .engram`
- **Recent memories** — up to 5 as: `[type] score — first 120 chars`
'@
$SlashFile = Join-Path $CommandsDir "engram.md"
Set-Content -Path $SlashFile -Value $SlashCmd -Encoding UTF8
Write-Success "Slash command /engram: $SlashFile"

# ─── Patch Claude Code settings.json ─────────────────────────────────────────
Write-Step "Registering hooks in Claude Code"

if ($SettingsFile -and (Test-Path $SettingsFile)) {
    try {
        $settings = Get-Content $SettingsFile -Raw | ConvertFrom-Json

        $injectCmd  = "powershell.exe -NonInteractive -NoProfile -File `"$(Join-Path $HooksDir 'engram-inject.ps1')`""
        $sessionCmd = "powershell.exe -NonInteractive -NoProfile -File `"$(Join-Path $HooksDir 'engram-session-write.ps1')`""

        # Add hooks block if missing
        if (-not $settings.hooks) {
            $settings | Add-Member -NotePropertyName hooks -NotePropertyValue ([PSCustomObject]@{})
        }

        # UserPromptSubmit
        if (-not $settings.hooks.UserPromptSubmit) {
            $settings.hooks | Add-Member -NotePropertyName UserPromptSubmit -NotePropertyValue @(@{hooks=@()})
        }
        $ups = $settings.hooks.UserPromptSubmit
        $alreadyInject = $ups | ForEach-Object { $_.hooks } | Where-Object { $_.command -eq $injectCmd }
        if (-not $alreadyInject) {
            $ups[0].hooks = @(@{type="command";command=$injectCmd;timeout=8}) + $ups[0].hooks
        }

        # Stop
        if (-not $settings.hooks.Stop) {
            $settings.hooks | Add-Member -NotePropertyName Stop -NotePropertyValue @(@{hooks=@()})
        }
        $stops = $settings.hooks.Stop
        $alreadySession = $stops | ForEach-Object { $_.hooks } | Where-Object { $_.command -eq $sessionCmd }
        if (-not $alreadySession) {
            $stops[0].hooks += @{type="command";command=$sessionCmd;timeout=8;async=$true}
        }

        $settings | ConvertTo-Json -Depth 10 | Set-Content $SettingsFile -Encoding UTF8
        Write-Success "Hooks registered in $SettingsFile"
    } catch {
        Write-Warn "Could not patch settings.json: $_"
        Write-Warn "Add hooks manually — see docs/claude-code-setup.md"
    }
} else {
    Write-Warn "settings.json not found — skipping auto-registration."
}

# ─── Done ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host "  engram client hooks installed!" -ForegroundColor Green
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host ""
Write-Host "  What was installed:" -ForegroundColor White
Write-Host "    $HooksDir\engram.env             — config"
Write-Host "    $HooksDir\engram-inject.ps1      — context injection"
Write-Host "    $HooksDir\engram-session-write.ps1 — session state"
Write-Host "    $GitHooksDir\post-commit.ps1     — commit memory"
Write-Host "    $CommandsDir\engram.md           — /engram slash command"
Write-Host ""
Write-Host "  Server    : $EngramServer" -ForegroundColor Cyan
Write-Host "  Namespace : $DefaultNS" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Per-project namespace override:" -ForegroundColor White
Write-Host '    "namespace=project:myname" | Set-Content .engram'
Write-Host ""
Write-Host "  Restart Claude Code (quit and reopen) to activate." -ForegroundColor Yellow
Write-Host ""
