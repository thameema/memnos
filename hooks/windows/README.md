# memnos Windows Hooks

Windows PowerShell equivalents of the bash hooks in `~/.claude/hooks/`.
These target PowerShell 5.1 (ships with Windows 10/11) and PowerShell 7+.

## Files

| File | Hook event | Purpose |
|------|-----------|---------|
| `memnos-inject.ps1` | Claude Code `UserPromptSubmit` | Injects relevant memories as context before each prompt |
| `memnos-session-write.ps1` | Claude Code `Stop` | Writes session state to memnos when a session ends |
| `post-commit.ps1` | git `post-commit` | Records every git commit to memnos memory |

## Configuration

Create `%USERPROFILE%\.claude\hooks\memnos.env` with key=value lines (no sections):

```
MEMNOS_API=http://localhost:8766
MEMNOS_KEY=your-api-key-here
MEMNOS_DEFAULT_NS=org:myteam:engineering
MEMNOS_TOP_K=5
```

The `post-commit.ps1` hook reads the same file from `$HOME\.claude\hooks\memnos.env`.

### Per-repo namespace override

Add a `.memnos` file to the repo root:

```
namespace=org:myteam:myproject
```

This takes highest priority over the default namespace in `memnos.env`.

## Installation

### Claude Code hooks (memnos-inject + memnos-session-write)

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
            "command": "powershell.exe -NonInteractive -File C:\\Users\\YOU\\.claude\\hooks\\windows\\memnos-inject.ps1"
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
            "command": "powershell.exe -NonInteractive -File C:\\Users\\YOU\\.claude\\hooks\\windows\\memnos-session-write.ps1"
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
call it automatically after writing to memnos. Use this for repo-specific actions.

## Behaviour

- All hooks fail **silently** — a network error or unreachable memnos server
  never blocks a prompt, session stop, or git commit.
- Health check timeout: 2 seconds.
- API call timeout: 5 seconds.
- The inject hook sends up to the first 200 characters of the prompt as the
  search query and surfaces up to `MEMNOS_TOP_K` (default 5) results.
- The post-commit hook maps conventional commit prefixes to memory types:
  - `feat:` / `feature:` / `refactor:` / `arch:` → `decision`
  - `fix:` / `hotfix:` / `bug:` → `incident`
  - everything else → `fact`
