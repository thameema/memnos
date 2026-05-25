# engram-session-write.ps1
#
# Claude Code Stop hook — writes session state to engram after every
# Claude Code session ends.
#
# Claude Code writes JSON to stdin:
#   { "cwd": "...", "session_id": "...", "prompt": "..." }
#
# Namespace resolved identically to engram-inject.ps1.
#
# Installation: add to Claude Code settings.json hooks → Stop
#   "command": "powershell.exe -NonInteractive -File C:\\path\\to\\engram-session-write.ps1"

# ── Load config ────────────────────────────────────────────────────────────────
$EnvFile = Join-Path $env:USERPROFILE ".claude\hooks\engram.env"

$ENGRAM_API        = "http://localhost:8766"
$ENGRAM_KEY        = ""
$ENGRAM_DEFAULT_NS = "personal:me"

if (Test-Path $EnvFile) {
    foreach ($line in (Get-Content $EnvFile)) {
        $line = $line.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { continue }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) { continue }
        $key   = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()
        switch ($key) {
            "ENGRAM_API"        { $ENGRAM_API        = $value }
            "ENGRAM_KEY"        { $ENGRAM_KEY        = $value }
            "ENGRAM_DEFAULT_NS" { $ENGRAM_DEFAULT_NS = $value }
        }
    }
}

# ── Read stdin ────────────────────────────────────────────────────────────────
try {
    $RawInput  = [Console]::In.ReadToEnd()
    $HookData  = $RawInput | ConvertFrom-Json
    $Cwd       = if ($HookData.cwd)        { $HookData.cwd }        else { "" }
    $SessionId = if ($HookData.session_id) { $HookData.session_id } else { "" }
} catch {
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Cwd)) { exit 0 }

# ── Check engram health ───────────────────────────────────────────────────────
try {
    $HealthUrl = "$ENGRAM_API/api/v1/admin/health"
    $Headers   = @{ "X-API-Key" = $ENGRAM_KEY }
    $null = Invoke-RestMethod -Uri $HealthUrl -Headers $Headers `
        -Method Get -TimeoutSec 2 -ErrorAction Stop
} catch {
    exit 0
}

# ── Resolve namespace ─────────────────────────────────────────────────────────
$EngNS = $ENGRAM_DEFAULT_NS

try {
    $RepoRoot = & git -C $Cwd rev-parse --show-toplevel 2>$null
    if ($RepoRoot) {
        $DotEngram = Join-Path $RepoRoot.Trim() ".engram"
        if (Test-Path $DotEngram) {
            foreach ($line in (Get-Content $DotEngram)) {
                $line = $line.Trim()
                $idx  = $line.IndexOf("=")
                if ($idx -lt 1) { continue }
                $k = $line.Substring(0, $idx).Trim()
                $v = $line.Substring($idx + 1).Trim()
                if ($k -eq "namespace" -and $v -ne "") {
                    $EngNS = $v
                    break
                }
            }
        }
    }
} catch {
    # not a git repo — keep default namespace
}

# ── Project label ─────────────────────────────────────────────────────────────
$Project = Split-Path -Leaf $Cwd

# ── Git context ───────────────────────────────────────────────────────────────
$Branch        = ""
$RecentCommits = ""
$Uncommitted   = 0

try {
    $null = & git -C $Cwd rev-parse --git-dir 2>$null
    if ($LASTEXITCODE -eq 0) {
        $Branch        = (& git -C $Cwd rev-parse --abbrev-ref HEAD 2>$null) -join ""
        $RecentCommits = (& git -C $Cwd log --oneline -5 --no-decorate 2>$null) -join "`n"
        $StatusLines   = & git -C $Cwd status --short 2>$null
        $Uncommitted   = if ($StatusLines) { @($StatusLines).Count } else { 0 }
    }
} catch {
    # git unavailable or not a repo
}

# ── Build content string ──────────────────────────────────────────────────────
$BranchPart = if ($Branch) { " | branch: $Branch" } else { "" }
$Header     = "session ended | project: $Project | dir: $Cwd$BranchPart | uncommitted: $Uncommitted"

if ($RecentCommits) {
    $Content = "$Header`nRecent commits:`n$RecentCommits"
} else {
    $Content = "session ended | project: $Project | dir: $Cwd"
}

# ── Build POST payload ────────────────────────────────────────────────────────
try {
    $Payload = [ordered]@{
        content     = $Content
        namespace   = $EngNS
        memory_type = "fact"
        tags        = @("session-log", "auto", $Project)
        metadata    = [ordered]@{
            session_id = $SessionId
            project    = $Project
            source     = "claude-code-stop-hook"
        }
    }
    $PayloadJson = $Payload | ConvertTo-Json -Depth 5 -Compress
} catch {
    exit 0
}

# ── POST to engram ────────────────────────────────────────────────────────────
try {
    $PostUrl = "$ENGRAM_API/api/v1/memory/"
    $Headers = @{
        "Content-Type" = "application/json"
        "X-API-Key"    = $ENGRAM_KEY
    }
    $null = Invoke-RestMethod -Uri $PostUrl -Headers $Headers `
        -Method Post -Body $PayloadJson -TimeoutSec 5 -ErrorAction Stop
} catch {
    exit 0
}

exit 0
