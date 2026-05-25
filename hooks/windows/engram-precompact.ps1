# engram-precompact.ps1
#
# Claude Code PreCompact hook — saves session state to engram before context compact.
# Fires when Claude Code's context window approaches its limit.
# Uses `claude --print` for LLM summarization — no API key needed.
#
# Installation: add to Claude Code settings.json hooks → PreCompact (async: true)
#   "command": "powershell.exe -NonInteractive -NoProfile -File \"C:\\path\\to\\engram-precompact.ps1\""

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

# claude CLI required for summaries
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { exit 0 }

# ── Read stdin ────────────────────────────────────────────────────────────────
try {
    $RawInput  = [Console]::In.ReadToEnd()
    $HookData  = $RawInput | ConvertFrom-Json
    $Cwd       = if ($HookData.cwd)           { $HookData.cwd }           else { "" }
    $SessionId = if ($HookData.session_id)    { $HookData.session_id }    else { "" }
    $Transcript= if ($HookData.transcript_path){ $HookData.transcript_path } else { "" }
} catch {
    exit 0
}

if ([string]::IsNullOrWhiteSpace($SessionId)) { exit 0 }

# ── Resolve namespace ─────────────────────────────────────────────────────────
$EngNS = $ENGRAM_DEFAULT_NS
try {
    $RepoRoot = & git -C $Cwd rev-parse --show-toplevel 2>$null
    if ($RepoRoot) {
        $DotEngram = Join-Path $RepoRoot.Trim() ".engram"
        if (Test-Path $DotEngram) {
            foreach ($line in (Get-Content $DotEngram)) {
                $idx = $line.IndexOf("=")
                if ($idx -lt 1) { continue }
                $k = $line.Substring(0, $idx).Trim()
                $v = $line.Substring($idx + 1).Trim()
                if ($k -eq "namespace" -and $v -ne "") { $EngNS = $v; break }
            }
        }
    }
} catch { }

$Project = if ($Cwd) { Split-Path $Cwd -Leaf } else { "unknown" }
$Branch  = ""
try { $Branch = (& git -C $Cwd rev-parse --abbrev-ref HEAD 2>$null).Trim() } catch { }

# ── Find transcript ───────────────────────────────────────────────────────────
if ([string]::IsNullOrWhiteSpace($Transcript) -or !(Test-Path $Transcript)) {
    $Slug       = $Cwd -replace '\\', '-' -replace '/', '-' -replace ':', ''
    $Transcript = Join-Path $env:USERPROFILE ".claude\projects\$Slug\$SessionId.jsonl"
}
if (!(Test-Path $Transcript)) { exit 0 }

# ── Read last 12 turns ────────────────────────────────────────────────────────
$turns = @()
foreach ($line in (Get-Content $Transcript -Encoding UTF8 -ErrorAction SilentlyContinue)) {
    try {
        $d = $line.Trim() | ConvertFrom-Json -ErrorAction Stop
        $role = $d.type
        if ($role -ne "user" -and $role -ne "assistant") { continue }
        $content = $d.message.content
        $text = ""
        if ($content -is [string]) {
            $text = $content
        } elseif ($content -is [array]) {
            foreach ($c in $content) {
                if ($c.type -eq "text") { $text += $c.text }
            }
        }
        $text = $text.Trim()
        if ($text.Length -gt 20) {
            $turns += "$($role.ToUpper()): $($text.Substring(0, [Math]::Min(500, $text.Length)))"
        }
    } catch { continue }
}
$turns = $turns | Select-Object -Last 12
if ($turns.Count -lt 2) { exit 0 }

# ── Generate summary via claude --print ──────────────────────────────────────
$promptText = "Project: $Project" + $(if ($Branch) { "  branch: $Branch" } else { "" }) +
    "`n`n[PRE-COMPACT — context window approaching limit]`n`n" +
    ($turns -join "`n`n") +
    "`n`nCapture this in-progress dev session before context is compacted. Write a dense, specific summary: what has been done, what is currently in progress, decisions made, errors encountered, exact current state. Name tickets, files, functions. Be concise (max 200 words). End with ""STATUS: <in-progress|blocked|complete>""."

try {
    $Summary = ($promptText | & claude --print --no-session-persistence --strict-mcp-config --tools "" 2>$null) -join "`n"
    $Summary = $Summary.Trim()
} catch {
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Summary)) { exit 0 }

# ── Write to engram ───────────────────────────────────────────────────────────
$content = "[pre-compact] $Project" + $(if ($Branch) { " | $Branch" } else { "" }) + " — $Summary"

$payload = @{
    content     = $content
    namespace   = $EngNS
    memory_type = "session"
    tags        = @("session-summary", "auto-compact", "real-time", $Project)
    metadata    = @{ session_id = $SessionId; project = $Project; source = "pre-compact-hook" }
    provenance  = @{ tool = "engram-precompact-hook"; agent_id = $SessionId }
} | ConvertTo-Json -Depth 5

try {
    $null = Invoke-RestMethod `
        -Uri "$ENGRAM_API/api/v1/memory/" `
        -Method Post `
        -Headers @{
            "Content-Type"    = "application/json"
            "X-API-Key"       = $ENGRAM_KEY
            "X-Engram-Tool"   = "precompact-hook"
        } `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
        -TimeoutSec 8
} catch { }

exit 0
