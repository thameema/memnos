# post-commit.ps1
#
# Global git post-commit hook — auto-writes every git commit to engram memory.
# No stdin: git calls this hook directly after a successful commit.
# Never blocks the commit — all errors are silent (exit 0).
#
# Memory type mapping:
#   feat: / feature: / refactor: / arch:  → decision
#   fix:  / hotfix:  / bug:               → incident
#   everything else                        → fact
#
# Chaining: if .git\hooks\post-commit.local.ps1 exists in the current repo,
# it is called after engram write so repo-specific logic can still run.
#
# Installation options:
#
#   A) Per-repo: copy to <repo>\.git\hooks\post-commit.ps1 and add a
#      wrapper post-commit (no extension) that calls:
#        powershell.exe -NonInteractive -File .git\hooks\post-commit.ps1
#
#   B) Global git hook: set core.hooksPath in ~/.gitconfig to a directory
#      containing this file renamed to post-commit (PowerShell 7+ on PATH),
#      or add a POSIX-compatible wrapper that delegates to powershell.exe.

# ── Load config ────────────────────────────────────────────────────────────────
$EnvFile = Join-Path $HOME ".claude\hooks\engram.env"

$ENGRAM_API        = "http://localhost:8766"
$ENGRAM_KEY        = "engram-local-dev-key"
$ENGRAM_DEFAULT_NS = "personal:default"

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

# ── Check engram health ───────────────────────────────────────────────────────
try {
    $HealthUrl = "$ENGRAM_API/api/v1/admin/health"
    $Headers   = @{ "Authorization" = "Bearer $ENGRAM_KEY" }
    $null = Invoke-RestMethod -Uri $HealthUrl -Headers $Headers `
        -Method Get -TimeoutSec 2 -ErrorAction Stop
} catch {
    exit 0   # engram not running — never block a commit
}

# ── Gather repo info ──────────────────────────────────────────────────────────
try {
    $RepoRoot = (& git rev-parse --show-toplevel 2>$null).Trim()
    $RepoName = Split-Path -Leaf $RepoRoot
} catch {
    exit 0
}

# ── Resolve namespace ─────────────────────────────────────────────────────────
$EngNS = $ENGRAM_DEFAULT_NS

try {
    $DotEngram = Join-Path $RepoRoot ".engram"
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
} catch {
    # keep default namespace
}

# ── Gather commit info ────────────────────────────────────────────────────────
try {
    $CommitHash   = (& git rev-parse --short HEAD 2>$null).Trim()
    $CommitFull   = (& git rev-parse HEAD 2>$null).Trim()
    $CommitMsg    = (& git log -1 --pretty=%B 2>$null | Select-Object -First 5) -join "`n"
    $CommitAuthor = (& git log -1 --pretty=%an 2>$null).Trim()
    $ChangedFiles = (& git diff-tree --no-commit-id -r --name-only HEAD 2>$null |
                     Select-Object -First 20) -join " "
    $Branch       = (& git rev-parse --abbrev-ref HEAD 2>$null).Trim()
} catch {
    exit 0
}

# ── Map memory type from conventional commit prefix ──────────────────────────
$MemoryType = "fact"
if ($CommitMsg -match "(?i)^(feat|feature|refactor|arch):") {
    $MemoryType = "decision"
} elseif ($CommitMsg -match "(?i)^(fix|hotfix|bug):") {
    $MemoryType = "incident"
}

# ── Build content string ──────────────────────────────────────────────────────
$Content = "[engram-commit] $CommitMsg`nrepo: $RepoName | commit: $CommitHash | branch: $Branch | author: $CommitAuthor`nfiles: $ChangedFiles"

# ── Build POST payload ────────────────────────────────────────────────────────
try {
    $Payload = [ordered]@{
        content     = $Content
        namespace   = $EngNS
        memory_type = $MemoryType
        author      = $CommitAuthor
        tags        = @("git-commit", "auto", $RepoName, $MemoryType)
        metadata    = [ordered]@{
            commit_hash   = $CommitFull
            branch        = $Branch
            author        = $CommitAuthor
            changed_files = $ChangedFiles
            source        = "post-commit-hook"
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
        "Authorization" = "Bearer $ENGRAM_KEY"
    }
    $null = Invoke-RestMethod -Uri $PostUrl -Headers $Headers `
        -Method Post -Body $PayloadJson -TimeoutSec 5 -ErrorAction Stop
} catch {
    exit 0   # never block the commit
}

# ── Chain to repo-local hook if present ──────────────────────────────────────
try {
    $LocalHook = Join-Path $RepoRoot ".git\hooks\post-commit.local.ps1"
    if (Test-Path $LocalHook) {
        & powershell.exe -NonInteractive -File $LocalHook
    }
} catch {
    # local hook failure must never block the commit
}

exit 0
