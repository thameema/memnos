# memnos-inject.ps1
#
# Claude Code UserPromptSubmit hook — injects relevant memnos memories as
# additional context before every Claude Code prompt.
#
# Claude Code writes JSON to stdin:
#   { "cwd": "...", "prompt": "...", "session_id": "..." }
#
# Namespace priority (highest → lowest):
#   1. .memnos file in repo root  (namespace=...)
#   2. MEMNOS_DEFAULT_NS in memnos.env
#
# Installation: add to Claude Code settings.json hooks → UserPromptSubmit
#   "command": "powershell.exe -NonInteractive -File C:\\path\\to\\memnos-inject.ps1"

# ── Launch heartbeat daemon (cross-platform Python, handles abrupt exits) ──────
$HeartbeatScript = Join-Path $env:USERPROFILE ".claude\hooks\memnos-heartbeat.py"
if (Test-Path $HeartbeatScript) {
    try {
        Start-Process -FilePath "python3" -ArgumentList "`"$HeartbeatScript`"" `
            -WindowStyle Hidden -ErrorAction SilentlyContinue
    } catch {
        try {
            Start-Process -FilePath "python" -ArgumentList "`"$HeartbeatScript`"" `
                -WindowStyle Hidden -ErrorAction SilentlyContinue
        } catch { }
    }
}

# ── Load config ────────────────────────────────────────────────────────────────
$EnvFile = Join-Path $env:USERPROFILE ".claude\hooks\memnos.env"

$MEMNOS_API        = "http://localhost:8766"
$MEMNOS_KEY        = ""
$MEMNOS_DEFAULT_NS = "personal:default"
$MEMNOS_TOP_K      = 8
$MEMNOS_MIN_SCORE  = 0.50

if (Test-Path $EnvFile) {
    foreach ($line in (Get-Content $EnvFile)) {
        $line = $line.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { continue }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) { continue }
        $key   = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()
        switch ($key) {
            "MEMNOS_API"        { $MEMNOS_API        = $value }
            "MEMNOS_KEY"        { $MEMNOS_KEY        = $value }
            "MEMNOS_DEFAULT_NS" { $MEMNOS_DEFAULT_NS = $value }
            "MEMNOS_TOP_K"      { $MEMNOS_TOP_K      = [int]$value }
            "MEMNOS_MIN_SCORE"  { $MEMNOS_MIN_SCORE  = [double]$value }
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

# ── Check memnos health ───────────────────────────────────────────────────────
try {
    $HealthUrl = "$MEMNOS_API/api/v1/admin/health"
    $Headers   = @{ "Authorization" = "Bearer $MEMNOS_KEY" }
    $null = Invoke-RestMethod -Uri $HealthUrl -Headers $Headers `
        -Method Get -TimeoutSec 2 -ErrorAction Stop
} catch {
    exit 0
}

# ── Resolve namespace ─────────────────────────────────────────────────────────
$EngNS = $MEMNOS_DEFAULT_NS

# Check .memnos file in repo root (highest priority)
try {
    $RepoRoot = & git -C $Cwd rev-parse --show-toplevel 2>$null
    if ($RepoRoot) {
        $DotMemnos = Join-Path $RepoRoot.Trim() ".memnos"
        if (Test-Path $DotMemnos) {
            foreach ($line in (Get-Content $DotMemnos)) {
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

# ── Secret pattern detection ─────────────────────────────────────────────────
$VaultAlert = ""
$SecretPatterns = @(
    @{ Pattern = 'sk-ant-api[0-9A-Za-z\-]{20,}';                                                           Label = 'Anthropic API key' },
    @{ Pattern = '\bsk-[0-9A-Za-z]{40,}';                                                                  Label = 'API key (sk-)' },
    @{ Pattern = '\bghp_[0-9A-Za-z]{36,}';                                                                 Label = 'GitHub personal token' },
    @{ Pattern = '\bghs_[0-9A-Za-z]{36,}';                                                                 Label = 'GitHub service token' },
    @{ Pattern = '\bAKIA[0-9A-Z]{16}\b';                                                                   Label = 'AWS access key' },
    @{ Pattern = '-----BEGIN [A-Z ]+ PRIVATE KEY-----';                                                     Label = 'private key PEM' },
    @{ Pattern = 'ey[A-Za-z0-9_\-]{20,}\.ey[A-Za-z0-9_\-]{20,}';                                         Label = 'JWT token' },
    @{ Pattern = '(?i)(?:password|api[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret)\s*[=:]\s*["'']+(?!\s)[^\s"'']{16,}'; Label = 'credential assignment' }
)
$FoundTypes = @()
foreach ($p in $SecretPatterns) {
    if ($Prompt -match $p.Pattern) { $FoundTypes += $p.Label }
}
if ($FoundTypes.Count -gt 0) {
    $TypeList   = $FoundTypes -join ", "
    $VaultAlert = "[vault-alert] Potential secret in prompt ($TypeList) — save to memnos vault before use: vault_secret_set(key_name=`"<name>`", value=`"<value>`", namespace=`"...`")"
}

# ── Build search query ────────────────────────────────────────────────────────
$QueryRaw  = $Prompt.Substring(0, [Math]::Min(200, $Prompt.Length))
$QueryEnc  = [System.Uri]::EscapeDataString($QueryRaw)

if ([string]::IsNullOrWhiteSpace($QueryEnc)) { exit 0 }

# ── Query memnos ──────────────────────────────────────────────────────────────
try {
    # Use ns=all — server searches every namespace the key can access
    $SearchUrl = "$MEMNOS_API/api/v1/memory/search?q=$QueryEnc&ns=all&top_k=$MEMNOS_TOP_K"
    $Headers   = @{ "Authorization" = "Bearer $MEMNOS_KEY" }
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

    # Filter below minimum score
    $Results = $Results | Where-Object {
        $s = $_.score
        ($null -ne $s) -and (($s -is [double]) -or ($s -is [decimal])) -and ([double]$s -ge $MEMNOS_MIN_SCORE)
    }
    if ($Results.Count -eq 0) { exit 0 }

    $Lines = [System.Collections.Generic.List[string]]::new()
    $Lines.Add("[memnos: relevant past context]")

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
    $Context = ""
}

# ── Merge memnos context + vault alert ───────────────────────────────────────
if ($Context -ne "" -and $VaultAlert -ne "") {
    $FullContext = "$Context`n$VaultAlert"
} elseif ($Context -ne "") {
    $FullContext = $Context
} elseif ($VaultAlert -ne "") {
    $FullContext = $VaultAlert
} else {
    exit 0
}

# ── Emit output JSON ──────────────────────────────────────────────────────────
try {
    $Output = [ordered]@{
        hookSpecificOutput = [ordered]@{
            hookEventName     = "UserPromptSubmit"
            additionalContext = $FullContext
        }
    }
    $Output | ConvertTo-Json -Depth 5 -Compress | Write-Output
} catch {
    exit 0
}

exit 0
