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
#               Python 3.8+ (for heartbeat daemon)
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
$ClaudeDir    = Join-Path $env:USERPROFILE ".claude"
$HooksDir     = Join-Path $ClaudeDir "hooks"
$CommandsDir  = Join-Path $ClaudeDir "commands"
$GitHooksDir  = Join-Path $env:USERPROFILE ".git-hooks"
$SettingsFile = Join-Path $ClaudeDir "settings.json"

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
$EnvLines = @(
    "# engram hook config — edit to change server, key, namespace, or tuning.",
    "ENGRAM_API=$EngramServer",
    "ENGRAM_KEY=$EngramKey",
    "ENGRAM_DEFAULT_NS=$DefaultNS",
    "ENGRAM_TOP_K=8",
    "ENGRAM_MIN_SCORE=0.50",
    "ENGRAM_AUTOSAVE_MINUTES=10",
    "ENGRAM_HEARTBEAT_MINUTES=10",
    "# LLM summaries use claude --print (no API key needed)"
)
$EnvFile = Join-Path $HooksDir "engram.env"
$EnvLines | Set-Content -Path $EnvFile -Encoding UTF8
Write-Success "Config: $EnvFile"

# ─── Install hook scripts ──────────────────────────────────────────────────────
Write-Step "Installing hook scripts"

$ScriptDir      = Split-Path -Parent $MyInvocation.MyCommand.Path
$WinHooksSource = Join-Path $ScriptDir "hooks\windows"

function Install-HookFile {
    param([string]$FileName, [string]$Description)
    $dest = Join-Path $HooksDir $FileName
    $src  = Join-Path $WinHooksSource $FileName
    if (Test-Path $src) {
        Copy-Item $src $dest -Force
        Write-Success "$Description`: $dest"
    } else {
        $url = "https://raw.githubusercontent.com/thameema/engram/master/hooks/windows/$FileName"
        try {
            Invoke-WebRequest $url -OutFile $dest -UseBasicParsing -TimeoutSec 15
            Write-Success "$Description (downloaded): $dest"
        } catch {
            Write-Warn "Could not install $FileName — download manually from $url"
        }
    }
}

# PowerShell hook scripts
Install-HookFile "engram-inject.ps1"       "Inject hook (UserPromptSubmit)"
Install-HookFile "engram-session-write.ps1" "Session hook (Stop)"
Install-HookFile "engram-precompact.ps1"   "PreCompact hook"
Install-HookFile "engram-git-write.ps1"    "Git+periodic hook (PostToolUse)"

# ─── Install heartbeat daemon (cross-platform Python) ─────────────────────────
Write-Step "Installing heartbeat daemon"

$HeartbeatSrc  = Join-Path $ScriptDir "hooks\windows\engram-heartbeat.py"
$HeartbeatDest = Join-Path $HooksDir "engram-heartbeat.py"

# Try to copy from repo first; fall back to downloading
if (!(Test-Path $HeartbeatSrc)) {
    # Also try root of repo (shared file)
    $HeartbeatSrc = Join-Path $ScriptDir "engram-heartbeat.py"
}

if (Test-Path $HeartbeatSrc) {
    Copy-Item $HeartbeatSrc $HeartbeatDest -Force
    Write-Success "Heartbeat daemon: $HeartbeatDest"
} else {
    $url = "https://raw.githubusercontent.com/thameema/engram/master/hooks/windows/engram-heartbeat.py"
    try {
        Invoke-WebRequest $url -OutFile $HeartbeatDest -UseBasicParsing -TimeoutSec 15
        Write-Success "Heartbeat daemon (downloaded): $HeartbeatDest"
    } catch {
        Write-Warn "Could not install engram-heartbeat.py — periodic saves will not work on abrupt exits."
        Write-Warn "Download manually from $url"
    }
}

# Check Python is available
$pythonAvail = $false
foreach ($py in @("python3", "python")) {
    try {
        $ver = & $py --version 2>&1
        if ($ver -match "Python 3") { $pythonAvail = $true; Write-Info "Python: $ver"; break }
    } catch { }
}
if (-not $pythonAvail) {
    Write-Warn "Python 3 not found — heartbeat daemon requires Python 3.8+."
    Write-Warn "Install from https://www.python.org/downloads/"
}

# ─── Install global git post-commit hook ──────────────────────────────────────
Write-Step "Installing global git post-commit hook"

$GitCommitSrc  = Join-Path $WinHooksSource "post-commit.ps1"
$GitCommitDest = Join-Path $GitHooksDir "post-commit.ps1"

if (Test-Path $GitCommitSrc) {
    Copy-Item $GitCommitSrc $GitCommitDest -Force
    Write-Success "Copied: $GitCommitDest"
}

$WrapperContent = "@echo off`r`npowershell.exe -NonInteractive -NoProfile -File `"$GitCommitDest`"`r`n"
$WrapperDest = Join-Path $GitHooksDir "post-commit"
Set-Content -Path $WrapperDest -Value $WrapperContent -Encoding ASCII

git config --global core.hooksPath $GitHooksDir
Write-Success "git config --global core.hooksPath $GitHooksDir"

# ─── Install /engram slash command ────────────────────────────────────────────
Write-Step "Installing /engram slash command"
$SlashCmd = @'
# /engram [save|status|ns:<namespace>]

---

## /engram status

Run these commands immediately and format results as shown.

```powershell
$KEY = (Get-Content "$env:USERPROFILE\.claude\hooks\engram.env" | Where-Object { $_ -match '^ENGRAM_KEY=' }) -replace '^ENGRAM_KEY=',''
$API = (Get-Content "$env:USERPROFILE\.claude\hooks\engram.env" | Where-Object { $_ -match '^ENGRAM_API=' }) -replace '^ENGRAM_API=',''
$NS  = (Get-Content "$env:USERPROFILE\.claude\hooks\engram.env" | Where-Object { $_ -match '^ENGRAM_DEFAULT_NS=' }) -replace '^ENGRAM_DEFAULT_NS=',''

# All namespaces
try { $ns_list = Invoke-RestMethod "$API/api/v1/admin/namespaces" -Headers @{"X-API-Key"=$KEY} } catch { $ns_list = @() }
$ns_list | ForEach-Object { $_.name }

# Current namespace
$repoRoot = git rev-parse --show-toplevel 2>$null
if ($repoRoot -and (Test-Path (Join-Path $repoRoot ".engram"))) {
    $line = Get-Content (Join-Path $repoRoot ".engram") | Where-Object { $_ -match '^namespace=' }
    "source:file"; ($line -split '=',2)[1]
} else { "source:default"; $NS }

# Recent memories
$search = Invoke-RestMethod "$API/api/v1/memory/search?q=session+commit+work&ns=$NS&top_k=5" -Headers @{"X-API-Key"=$KEY}
$search | ForEach-Object { "[$($_.memory_type)] $([math]::Round($_.score,2)) — $($_.content.Substring(0,[math]::Min(120,$_.content.Length)))" }
```

**engram status**
- **Namespaces** — bullet list
- **Active namespace** — name + how resolved (.engram file / default)
  If $ARGUMENTS contains `ns:something`: show `"namespace=something" | Set-Content .engram`
- **Recent memories** — up to 5 as: `[type] score — first 120 chars`

---

## /engram save

Persist this entire session to engram as raw, searchable chunks.

**Use the conversation in your current context window. Do NOT read transcript files.**
Write content as-is — do NOT summarize or compress. Cover the full session chronologically.
Do NOT stop early — every task, finding, decision, error, and fix must be captured.

### Steps

**1. Read config:**
```powershell
$KEY = (Get-Content "$env:USERPROFILE\.claude\hooks\engram.env" | Where-Object { $_ -match '^ENGRAM_KEY=' }) -replace '^ENGRAM_KEY=',''
$API = (Get-Content "$env:USERPROFILE\.claude\hooks\engram.env" | Where-Object { $_ -match '^ENGRAM_API=' }) -replace '^ENGRAM_API=',''
$NS  = (Get-Content "$env:USERPROFILE\.claude\hooks\engram.env" | Where-Object { $_ -match '^ENGRAM_DEFAULT_NS=' }) -replace '^ENGRAM_DEFAULT_NS=',''
$repoRoot = git rev-parse --show-toplevel 2>$null
if ($repoRoot -and (Test-Path (Join-Path $repoRoot ".engram"))) {
    $NS = ((Get-Content (Join-Path $repoRoot ".engram") | Where-Object { $_ -match '^namespace=' }) -split '=',2)[1]
}
$PROJECT = Split-Path (git rev-parse --show-toplevel 2>$null) -Leaf
$BRANCH  = git rev-parse --abbrev-ref HEAD 2>$null
Write-Host "NS=$NS  PROJECT=$PROJECT  BRANCH=$BRANCH"
```

**2. Split the session into ~300-word raw chunks**, then write each using Python:
```python
import json, urllib.request, os

def cfg(key, default=''):
    try:
        env_path = os.path.join(os.environ['USERPROFILE'], '.claude', 'hooks', 'engram.env')
        for line in open(env_path).read().splitlines():
            if line.startswith(key + '='): return line.split('=', 1)[1].strip()
    except: pass
    return default

api  = cfg('ENGRAM_API',        'http://localhost:8766')
akey = cfg('ENGRAM_KEY',        '')
ns   = cfg('ENGRAM_DEFAULT_NS', 'personal:me')
import subprocess
try:
    root = subprocess.check_output(['git','rev-parse','--show-toplevel'],
        stderr=subprocess.DEVNULL, text=True).strip()
    for line in open(f'{root}/.engram').read().splitlines():
        if line.startswith('namespace='): ns = line.split('=',1)[1].strip()
except: pass
project = os.path.basename(subprocess.run(['git','rev-parse','--show-toplevel'],
    capture_output=True, text=True).stdout.strip() or os.getcwd())

# ── FILL IN: one entry per ~300-word raw segment of the session ──────────────
chunks = [
    "CHUNK_1_CONTENT_HERE",
    "CHUNK_2_CONTENT_HERE",
    # ...
]
# ─────────────────────────────────────────────────────────────────────────────

for i, chunk in enumerate(chunks, 1):
    payload = json.dumps({
        'content':     f'[chunk {i}/{len(chunks)}] {chunk}',
        'namespace':   ns,
        'memory_type': 'session',
        'tags':        ['session-chunk', 'manual-save', project],
        'metadata':    {'project': project, 'chunk': i, 'total': len(chunks), 'source': 'save-command'},
    }).encode()
    req = urllib.request.Request(f'{api}/api/v1/memory/', data=payload,
        headers={'Content-Type':'application/json','X-API-Key':akey}, method='POST')
    r = json.loads(urllib.request.urlopen(req, timeout=5).read())
    print(f'  chunk {i}: {r.get("id","?")[:8]}')

print(f'Written {len(chunks)} chunks to {ns}')
```

**3. Write a session index memory** (fill in PROJECT, BRANCH, SUMMARY, N_CHUNKS):
```python
# (same config setup as above, then:)
content = f'[session-index] PROJECT | BRANCH — BRIEF_SUMMARY | chunks: N_CHUNKS'
payload = json.dumps({'content': content, 'namespace': ns, 'memory_type': 'session',
    'tags': ['session-index', 'manual-save', project]}).encode()
req = urllib.request.Request(f'{api}/api/v1/memory/', data=payload,
    headers={'Content-Type':'application/json','X-API-Key':akey}, method='POST')
print('Index:', json.loads(urllib.request.urlopen(req,timeout=5).read()).get('id','?')[:8])
```

**4. Report:** `Saved N chunks + 1 index to <namespace>`

---

## /engram ns:<namespace>

To set a permanent namespace for a project:
```powershell
"namespace=<namespace>" | Set-Content .engram
```
Then confirm with `/engram status` that the new namespace is active.
'@
$SlashFile = Join-Path $CommandsDir "engram.md"
Set-Content -Path $SlashFile -Value $SlashCmd -Encoding UTF8
Write-Success "Slash command /engram: $SlashFile"

# ─── Patch Claude Code settings.json ─────────────────────────────────────────
Write-Step "Registering hooks in Claude Code"

if ($SettingsFile -and (Test-Path $SettingsFile)) {
    try {
        $settings = Get-Content $SettingsFile -Raw | ConvertFrom-Json

        $injectCmd     = "powershell.exe -NonInteractive -NoProfile -File `"$(Join-Path $HooksDir 'engram-inject.ps1')`""
        $precompactCmd = "powershell.exe -NonInteractive -NoProfile -File `"$(Join-Path $HooksDir 'engram-precompact.ps1')`""
        $gitwriteCmd   = "powershell.exe -NonInteractive -NoProfile -File `"$(Join-Path $HooksDir 'engram-git-write.ps1')`""
        $sessionCmd    = "powershell.exe -NonInteractive -NoProfile -File `"$(Join-Path $HooksDir 'engram-session-write.ps1')`""

        if (-not $settings.hooks) {
            $settings | Add-Member -NotePropertyName hooks -NotePropertyValue ([PSCustomObject]@{})
        }

        function Register-Hook {
            param($hookList, $command, $timeout, $async = $false)
            $exists = $hookList | ForEach-Object { $_.hooks } | Where-Object { $_.command -eq $command }
            if (-not $exists) {
                $entry = @{ type = "command"; command = $command; timeout = $timeout }
                if ($async) { $entry.async = $true }
                $hookList[0].hooks += $entry
            }
        }

        # UserPromptSubmit — inject
        if (-not $settings.hooks.UserPromptSubmit) {
            $settings.hooks | Add-Member -NotePropertyName UserPromptSubmit -NotePropertyValue @(@{ hooks = @() })
        }
        $ups = $settings.hooks.UserPromptSubmit
        $alreadyInject = $ups | ForEach-Object { $_.hooks } | Where-Object { $_.command -eq $injectCmd }
        if (-not $alreadyInject) {
            $ups[0].hooks = @(@{ type="command"; command=$injectCmd; timeout=8 }) + $ups[0].hooks
        }

        # PreCompact — precompact (async)
        if (-not $settings.hooks.PreCompact) {
            $settings.hooks | Add-Member -NotePropertyName PreCompact -NotePropertyValue @(@{ hooks = @() })
        }
        $pcs = $settings.hooks.PreCompact
        $alreadyPC = $pcs | ForEach-Object { $_.hooks } | Where-Object { $_.command -eq $precompactCmd }
        if (-not $alreadyPC) {
            $pcs[0].hooks += @{ type="command"; command=$precompactCmd; timeout=30; async=$true }
        }

        # PostToolUse — git-write (async)
        if (-not $settings.hooks.PostToolUse) {
            $settings.hooks | Add-Member -NotePropertyName PostToolUse -NotePropertyValue @(@{ hooks = @() })
        }
        $ptus = $settings.hooks.PostToolUse
        $alreadyGW = $ptus | ForEach-Object { $_.hooks } | Where-Object { $_.command -eq $gitwriteCmd }
        if (-not $alreadyGW) {
            $ptus[0].hooks += @{ type="command"; command=$gitwriteCmd; timeout=6; async=$true }
        }

        # Stop — session-write (async)
        if (-not $settings.hooks.Stop) {
            $settings.hooks | Add-Member -NotePropertyName Stop -NotePropertyValue @(@{ hooks = @() })
        }
        $stops = $settings.hooks.Stop
        $alreadySession = $stops | ForEach-Object { $_.hooks } | Where-Object { $_.command -eq $sessionCmd }
        if (-not $alreadySession) {
            $stops[0].hooks += @{ type="command"; command=$sessionCmd; timeout=8; async=$true }
        }

        $settings | ConvertTo-Json -Depth 10 | Set-Content $SettingsFile -Encoding UTF8
        Write-Success "4 hooks registered in $SettingsFile"
    } catch {
        Write-Warn "Could not patch settings.json: $_"
        Write-Warn "Add hooks manually — see the README."
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
Write-Host "    $HooksDir\engram.env                — config (server, key, namespace)"
Write-Host "    $HooksDir\engram-inject.ps1         — context injection (UserPromptSubmit)"
Write-Host "    $HooksDir\engram-heartbeat.py       — background daemon (abrupt-exit safety)"
Write-Host "    $HooksDir\engram-git-write.ps1      — git commits + periodic save (PostToolUse)"
Write-Host "    $HooksDir\engram-precompact.ps1     — save before context compact (PreCompact)"
Write-Host "    $HooksDir\engram-session-write.ps1  — session summary on exit (Stop)"
Write-Host "    $GitHooksDir\post-commit.ps1        — commit memory on every git commit"
Write-Host "    $CommandsDir\engram.md              — /engram slash command"
Write-Host ""
Write-Host "  Hook pipeline:" -ForegroundColor White
Write-Host "    UserPromptSubmit → inject context"
Write-Host "    PostToolUse      → capture git commits, periodic auto-save (every 10min)"
Write-Host "    PreCompact       → save before context window compact"
Write-Host "    Stop             → full session summary on exit"
Write-Host "    Heartbeat daemon → safety net for Ctrl+C / power loss (every 10min)"
Write-Host ""
Write-Host "  Server    : $EngramServer" -ForegroundColor Cyan
Write-Host "  Namespace : $DefaultNS"   -ForegroundColor Cyan
Write-Host "  LLM summaries: via claude --print (built-in)" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Per-project namespace:" -ForegroundColor White
Write-Host '    "namespace=project:myname" | Set-Content .engram'
Write-Host ""
Write-Host "  Restart Claude Code (quit and reopen) to activate." -ForegroundColor Yellow
Write-Host ""
