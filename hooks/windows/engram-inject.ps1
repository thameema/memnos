# engram-inject.ps1
#
# Claude Code UserPromptSubmit hook — injects relevant engram memories as
# additional context before every Claude Code prompt.
#
# Claude Code writes JSON to stdin:
#   { "cwd": "...", "prompt": "...", "session_id": "..." }
#
# Namespace priority (highest → lowest):
#   1. .engram file in repo root  (namespace=...)
#   2. ENGRAM_DEFAULT_NS in engram.env
#
# Installation: add to Claude Code settings.json hooks → UserPromptSubmit
#   "command": "powershell.exe -NonInteractive -File C:\\path\\to\\engram-inject.ps1"

# ── Load config ────────────────────────────────────────────────────────────────
$EnvFile = Join-Path $env:USERPROFILE ".claude\hooks\engram.env"

$ENGRAM_API        = "http://localhost:8766"
$ENGRAM_KEY        = ""
$ENGRAM_DEFAULT_NS = "personal:me"
$ENGRAM_TOP_K      = 5

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
            "ENGRAM_TOP_K"      { $ENGRAM_TOP_K      = [int]$value }
        }
    }
}

# ── Read stdin ────────────────────────────────────────────────────────────────
try {
    $RawInput = [Console]::In.ReadToEnd()
    $HookData = $RawInput | ConvertFrom-Json
    $Cwd      = if ($HookData.cwd)    { $HookData.cwd }    else { "" }
    $Prompt   = if ($HookData.prompt) { $HookData.prompt } else { "" }
} catch {
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Prompt)) { exit 0 }

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

# Check .engram file in repo root (highest priority)
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
    # not a git repo or git unavailable — keep default namespace
}

# ── Build search query ────────────────────────────────────────────────────────
$QueryRaw  = $Prompt.Substring(0, [Math]::Min(200, $Prompt.Length))
$QueryEnc  = [System.Uri]::EscapeDataString($QueryRaw)

if ([string]::IsNullOrWhiteSpace($QueryEnc)) { exit 0 }

# ── Query engram ──────────────────────────────────────────────────────────────
try {
    $SearchUrl = "$ENGRAM_API/api/v1/memory/search?q=$QueryEnc&ns=$EngNS&top_k=$ENGRAM_TOP_K"
    $Headers   = @{ "X-API-Key" = $ENGRAM_KEY }
    $Response  = Invoke-RestMethod -Uri $SearchUrl -Headers $Headers `
        -Method Get -TimeoutSec 5 -ErrorAction Stop
} catch {
    exit 0
}

# ── Format results ────────────────────────────────────────────────────────────
try {
    # Response may be an array directly or an object with a .results property
    if ($Response -is [System.Array]) {
        $Results = $Response
    } elseif ($Response.results) {
        $Results = $Response.results
    } else {
        exit 0
    }

    if ($Results.Count -eq 0) { exit 0 }

    $Lines = [System.Collections.Generic.List[string]]::new()
    $Lines.Add("[engram: relevant past context]")

    foreach ($item in $Results) {
        $mem     = if ($item.memory) { $item.memory } else { $item }
        $mtype   = if ($mem.memory_type) { $mem.memory_type } else { "fact" }
        $content = if ($mem.content)     { $mem.content.Trim() } else { "" }
        $score   = $item.score

        if ([string]::IsNullOrWhiteSpace($content)) { continue }

        $truncated = $content.Substring(0, [Math]::Min(280, $content.Length))

        $scoreStr = ""
        if ($null -ne $score -and $score -is [double]) {
            $scoreStr = " (similarity: $($score.ToString('F2')))"
        } elseif ($null -ne $score -and $score -is [decimal]) {
            $scoreStr = " (similarity: $($score.ToString('F2')))"
        }

        $Lines.Add("[$mtype]$scoreStr $truncated")
    }

    if ($Lines.Count -le 1) { exit 0 }

    $Context = $Lines -join "`n"
} catch {
    exit 0
}

# ── Emit output JSON ──────────────────────────────────────────────────────────
try {
    $Output = [ordered]@{
        hookSpecificOutput = [ordered]@{
            hookEventName     = "UserPromptSubmit"
            additionalContext = $Context
        }
    }
    $Output | ConvertTo-Json -Depth 5 -Compress | Write-Output
} catch {
    exit 0
}

exit 0
