# engram Windows Hooks

Windows PowerShell equivalents of the bash hooks in `~/.claude/hooks/`.
These target PowerShell 5.1 (ships with Windows 10/11) and PowerShell 7+.

## Files

| File | Hook event | Purpose |
|------|-----------|---------|
| `engram-inject.ps1` | Claude Code `UserPromptSubmit` | Injects relevant memories as context before each prompt |
| `engram-session-write.ps1` | Claude Code `Stop` | Writes session state to engram when a session ends |
| `post-commit.ps1` | git `post-commit` | Records every git commit to engram memory |

## Configuration

Create `%USERPROFILE%\.claude\hooks\engram.env` with key=value lines (no sections):

```
ENGRAM_API=http://localhost:8766
ENGRAM_KEY=your-api-key-here
ENGRAM_DEFAULT_NS=org:myteam:engineering
ENGRAM_TOP_K=5
```

The `post-commit.ps1` hook reads the same file from `$HOME\.claude\hooks\engram.env`.

### Per-repo namespace override

Add a `.engram` file to the repo root:

```
namespace=org:myteam:myproject
```

This takes highest priority over the default namespace in `engram.env`.

## Installation

### Claude Code hooks (engram-inject + engram-session-write)

Add entries to your Claude Code `settings.json` (usually `%APPDATA%\Claude\settings.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "powershell.exe -NonInteractive -File C:\\Users\\YOU\\.claude\\hooks\\windows\\engram-inject.ps1"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "powershell.exe -NonInteractive -File C:\\Users\\YOU\\.claude\\hooks\\windows\\engram-session-write.ps1"
          }
        ]
      }
    ]
  }
}
```

Copy (or symlink) the `.ps1` files to a location of your choice and update the paths above.

### git post-commit hook

**Option A — per-repo wrapper (works with any git version):**

1. Copy `post-commit.ps1` into `<repo>\.git\hooks\post-commit.ps1`
2. Create `<repo>\.git\hooks\post-commit` (no extension, must be executable) with:
   ```sh
   #!/bin/sh
   powershell.exe -NonInteractive -File "$(git rev-parse --show-toplevel)/.git/hooks/post-commit.ps1"
   ```

**Option B — global hooks directory (git 2.9+, PowerShell 7 on PATH):**

1. Create a directory, e.g. `%USERPROFILE%\.git-hooks`
2. Copy `post-commit.ps1` there as `post-commit` (no extension) — only works if
   PowerShell 7 (`pwsh`) is on PATH and the shebang is updated to `#!/usr/bin/env pwsh`
3. Configure git globally:
   ```
   git config --global core.hooksPath %USERPROFILE%\.git-hooks
   ```

### Chaining repo-local logic (post-commit only)

If `.git\hooks\post-commit.local.ps1` exists in a repo, `post-commit.ps1` will
call it automatically after writing to engram. Use this for repo-specific actions.

## Behaviour

- All hooks fail **silently** — a network error or unreachable engram server
  never blocks a prompt, session stop, or git commit.
- Health check timeout: 2 seconds.
- API call timeout: 5 seconds.
- The inject hook sends up to the first 200 characters of the prompt as the
  search query and surfaces up to `ENGRAM_TOP_K` (default 5) results.
- The post-commit hook maps conventional commit prefixes to memory types:
  - `feat:` / `feature:` / `refactor:` / `arch:` → `decision`
  - `fix:` / `hotfix:` / `bug:` → `incident`
  - everything else → `fact`
