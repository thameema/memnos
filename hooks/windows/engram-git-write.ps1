# engram-git-write.ps1
#
# Claude Code PostToolUse hook — two jobs:
# 1. Git commits: written to engram immediately when Claude runs git commit
# 2. Periodic auto-save: every 10 minutes of tool activity, background session save
#
# Installation: add to Claude Code settings.json hooks → PostToolUse (async: true)
#   "command": "powershell.exe -NonInteractive -NoProfile -File \"C:\\path\\to\\engram-git-write.ps1\""

# ── Load config ────────────────────────────────────────────────────────────────
$EnvFile = Join-Path $env:USERPROFILE ".claude\hooks\engram.env"

$ENGRAM_API             = "http://localhost:8766"
$ENGRAM_KEY             = ""
$ENGRAM_DEFAULT_NS      = "personal:me"
$SAVE_INTERVAL_MINUTES  = 10

if (Test-Path $EnvFile) {
    foreach ($line in (Get-Content $EnvFile)) {
        $line = $line.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { continue }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) { continue }
        $key   = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()
        switch ($key) {
            "ENGRAM_API"              { $ENGRAM_API            = $value }
            "ENGRAM_KEY"              { $ENGRAM_KEY            = $value }
            "ENGRAM_DEFAULT_NS"       { $ENGRAM_DEFAULT_NS     = $value }
            "ENGRAM_AUTOSAVE_MINUTES" { $SAVE_INTERVAL_MINUTES = [int]$value }
        }
    }
}

# ── Read stdin ────────────────────────────────────────────────────────────────
try {
    $RawInput  = [Console]::In.ReadToEnd()
    $HookData  = $RawInput | ConvertFrom-Json
    $ToolName  = if ($HookData.tool_name)               { $HookData.tool_name }               else { "" }
    $ToolInput = if ($HookData.tool_input)              { $HookData.tool_input }               else { $null }
    $ToolResp  = if ($HookData.tool_response)           { $HookData.tool_response }            else { "" }
    $Cwd       = if ($HookData.cwd)                     { $HookData.cwd }                      else { "" }
    $SessionId = if ($HookData.session_id)              { $HookData.session_id }               else { "" }
    $Transcript= if ($HookData.transcript_path)         { $HookData.transcript_path }          else { "" }
    $Cmd       = if ($ToolInput -and $ToolInput.command){ $ToolInput.command }                  else { "" }
} catch {
    exit 0
}

if ([string]::IsNullOrWhiteSpace($SessionId)) { exit 0 }

# ── Namespace resolution ──────────────────────────────────────────────────────
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

# ── Job 1: git commit capture ─────────────────────────────────────────────────
if ($ToolName -eq "Bash" -and $Cmd -match "git commit") {
    $CommitLine = ""
    foreach ($line in ($ToolResp -split "`n")) {
        if ($line -match '^\[') { $CommitLine = $line.Trim(); break }
    }
    if ([string]::IsNullOrWhiteSpace($CommitLine)) {
        if ($Cmd -match '"([^"]{10,})"') { $CommitLine = $Matches[1] }
    }

    $CommitContent = "$Project" + $(if ($Branch) { " | $Branch" } else { "" }) + " — committed: $CommitLine"

    $payload = @{
        content     = $CommitContent
        namespace   = $EngNS
        memory_type = "fact"
        tags        = @("git-commit", "real-time", "auto", $Project)
        metadata    = @{ session_id = $SessionId; project = $Project; source = "post-tool-hook" }
        provenance  = @{ tool = "claude-code-post-tool-hook"; agent_id = $SessionId }
    } | ConvertTo-Json -Depth 5

    try {
        $null = Invoke-RestMethod `
            -Uri "$ENGRAM_API/api/v1/memory/" `
            -Method Post `
            -Headers @{ "Content-Type" = "application/json"; "Authorization" = "Bearer $ENGRAM_KEY"; "X-Engram-Tool" = "post-tool-hook" } `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
            -TimeoutSec 4
    } catch { }
}

# ── Job 2: time-based auto-save every N minutes ───────────────────────────────
$CounterFile  = Join-Path $env:TEMP "engram_counter_$SessionId"
$LastSaveFile = Join-Path $env:TEMP "engram_lastsave_$SessionId"

$Count = 0
try { $Count = [int](Get-Content $CounterFile -ErrorAction Stop) } catch { }
$Count++
Set-Content $CounterFile $Count

$LastSave = 0
try { $LastSave = [long](Get-Content $LastSaveFile -ErrorAction Stop) } catch { }
$Now     = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$Elapsed = $Now - $LastSave

if ($Elapsed -ge ($SAVE_INTERVAL_MINUTES * 60)) {
    Set-Content $LastSaveFile $Now

    # Find transcript
    if ([string]::IsNullOrWhiteSpace($Transcript) -or !(Test-Path $Transcript)) {
        $Slug       = $Cwd -replace '\\', '-' -replace '/', '-' -replace ':', ''
        $Transcript = Join-Path $env:USERPROFILE ".claude\projects\$Slug\$SessionId.jsonl"
    }

    if ((Test-Path $Transcript) -and (Get-Command claude -ErrorAction SilentlyContinue)) {
        # Run summarization + write in a background job so it never blocks the tool response
        $bgScript = {
            param($transcript, $project, $branch, $count, $engramApi, $engramKey, $ns, $sessionId)

            $turns = @()
            foreach ($line in (Get-Content $transcript -Encoding UTF8 -ErrorAction SilentlyContinue)) {
                try {
                    $d = $line.Trim() | ConvertFrom-Json -ErrorAction Stop
                    $role = $d.type
                    if ($role -ne "user" -and $role -ne "assistant") { continue }
                    $content = $d.message.content
                    $text = ""
                    if ($content -is [string]) { $text = $content }
                    elseif ($content -is [array]) {
                        foreach ($c in $content) { if ($c.type -eq "text") { $text += $c.text } }
                    }
                    $text = $text.Trim()
                    if ($text.Length -gt 20) {
                        $turns += "$($role.ToUpper()): $($text.Substring(0,[Math]::Min(500,$text.Length)))"
                    }
                } catch { continue }
            }
            $turns = $turns | Select-Object -Last 12
            if ($turns.Count -lt 2) { return }

            $promptText = "Project: $project" + $(if ($branch) { "  branch: $branch" } else { "" }) +
                "  [auto-save at tool-call #$count]`n`n" + ($turns -join "`n`n") +
                "`n`nCapture this in-progress dev session for another agent to resume. Write a dense, specific summary: what has been done, what is currently being worked on, decisions made, errors seen, current status. Name specific tickets, files, functions. Be concise (max 200 words). End with ""STATUS: <in-progress|blocked|complete>""." +
                "`nIMPORTANT: respond with PLAIN TEXT ONLY. Do not generate any tool calls, <function_calls> XML, or <invoke> tags."

            try {
                $rawOutput = ($promptText | & claude --print --no-session-persistence --strict-mcp-config --tools "" 2>$null) -join "`n"
                # Strip tool call XML that claude --print may emit even with --tools ""
                $summary = [regex]::Replace($rawOutput, '(?s)<function_calls>.*?</function_calls>', '')
                $summary = [regex]::Replace($summary,   '(?s)<tool_call>.*?</tool_call>', '')
                $summary = [regex]::Replace($summary,   '(\r?\n){3,}', "`n`n")
                $summary = $summary.Trim()
            } catch { return }

            if ([string]::IsNullOrWhiteSpace($summary)) { return }

            $content = "[auto-save #$count] $project" + $(if ($branch) { " | $branch" } else { "" }) + " — $summary"
            $payload = @{
                content     = $content
                namespace   = $ns
                memory_type = "session"
                tags        = @("session-summary", "auto-periodic", "real-time", $project)
                metadata    = @{ session_id = $sessionId; project = $project; elapsed_minutes = $count; source = "periodic-autosave" }
                provenance  = @{ tool = "periodic-autosave"; agent_id = $sessionId }
            } | ConvertTo-Json -Depth 5

            try {
                $null = Invoke-RestMethod `
                    -Uri "$engramApi/api/v1/memory/" `
                    -Method Post `
                    -Headers @{ "Content-Type" = "application/json"; "Authorization" = "Bearer $engramKey"; "X-Engram-Tool" = "periodic-autosave" } `
                    -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) `
                    -TimeoutSec 5
            } catch { }
        }

        Start-Job -ScriptBlock $bgScript -ArgumentList `
            $Transcript, $Project, $Branch, $Count,
            $ENGRAM_API, $ENGRAM_KEY, $EngNS, $SessionId | Out-Null
    }
}

exit 0
