# Connecting Claude Code to engram

This guide covers the full setup: registering engram as a Claude Code MCP server, configuring `CLAUDE.md` so Claude uses it, and wiring the zero-touch automation hooks so context is injected and memories are written without any manual action.

## Prerequisites

- engram server installed and running — see [Installer](#installer) below or [quickstart.md](quickstart.md)
- Claude Code v2.0+ (CLI or desktop app)
- macOS, Linux, WSL, or Windows (PowerShell 5.1+)

---

## Installer

engram ships three installer scripts. Run the one that fits your situation:

| Script | Platform | What it does |
|--------|----------|--------------|
| `install.sh` | macOS / Linux / WSL | **Orchestrator** — interactive menu, calls the scripts below |
| `install-server.sh` | macOS / Linux / WSL | Installs engram server via Docker (ArcadeDB + API) |
| `install-client.sh` | macOS / Linux / WSL / Git Bash | Installs Claude Code hooks on a developer machine |
| `install-client.ps1` | Windows (PowerShell 5.1+) | Same as above for Windows |

### Quick start

```bash
# Interactive — choose server / client / both
./install.sh

# Or non-interactive
./install.sh --both        # server + client on this machine (laptop / dev box)
./install.sh --server      # server only (dedicated VM or shared host)
./install.sh --client      # client hooks only (team member pointing at remote server)
```

The orchestrator presents a menu when run without flags:

```
  What would you like to install?

  1) Full install (server + client)
     → Run engram server here AND install Claude Code hooks on this machine.
  2) Server only
     → Install engram server (Docker). Share the API URL + key with team members.
  3) Client only
     → Install Claude Code hooks only. Connects to an existing engram server.
```

### Windows

```powershell
# With server URL and key (e.g. pointing at a remote/shared engram server)
.\install-client.ps1 -Server http://engram.yourcompany.com:8766 -Key engram-abc123

# Local server
.\install-client.ps1 -Server http://localhost:8766 -Key engram-abc123
```

Requires git for Windows and Claude Code for Windows. The script copies PowerShell hook scripts from `hooks/windows/` and creates a `.bat` wrapper so git can invoke the post-commit hook.

### What gets installed

**Server** (`install-server.sh`):
- `~/.engram/docker-compose.yml` — ArcadeDB + engram API containers
- `~/.engram/.env` — API key, ArcadeDB password (mode 600)
- Pulls images and starts services

**Client** (`install-client.sh` / `install-client.ps1`):
- `~/.claude/hooks/engram.env` — central config (server URL, API key, default namespace)
- `~/.claude/hooks/engram-inject.sh` — UserPromptSubmit hook
- `~/.claude/hooks/engram-session-write.sh` — Stop hook
- `~/.git-hooks/post-commit` — global git hook
- `~/.claude/commands/engram.md` — `/engram` slash command
- Patches `~/.claude/settings.json` to register the hooks

---

## Step 1 — Register the MCP server

Claude Code reads MCP server configuration from **`~/.claude.json`** (not `~/.claude/settings.json`).

Two transport modes are available. Choose one:

### Transport A — stdio (recommended for local use)

The stdio transport is simpler: Claude Code spawns `engram-mcp-stdio` as a subprocess. No HTTP server needs to be running separately; ArcadeDB must still be accessible.

```json
{
  "mcpServers": {
    "engram": {
      "type": "stdio",
      "command": "/Users/yourname/.venv/bin/engram-mcp-stdio",
      "env": {
        "ENGRAM_CONFIG": "/Users/yourname/engram/engram.yaml",
        "ARCADEDB_PASSWORD": "your-arcadedb-password",
        "ENGRAM_API_KEY": "your-engram-api-key",
        "ENGRAM_VAULT_KEY": "your-vault-key",
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

Find the binary path: `which engram-mcp-stdio`

> **Important:** Use **absolute paths** for `command` and `ENGRAM_CONFIG`. Claude Code spawns the process from a different working directory; relative paths will fail silently.

> **Startup time:** On first invocation, `engram-mcp-stdio` loads the sentence-transformers embedding model (~25–40s). Claude Code's stdio timeout is 60s by default — if your machine is slow, consider setting `ENGRAM_LOG_LEVEL=WARNING` to reduce I/O during startup.

### Transport B — SSE (HTTP, for shared/remote servers)

Requires `engram-server` to be running (see [quickstart.md](quickstart.md)). Best for team deployments where all engineers share one server.

```json
{
  "mcpServers": {
    "engram": {
      "type": "sse",
      "url": "http://localhost:8765/sse",
      "headers": {
        "Authorization": "Bearer your-engram-api-key"
      }
    }
  }
}
```

For a team server, replace `localhost:8765` with your shared server URL.

---

After adding either entry: **fully restart Claude Code** (quit and reopen), then run:

```
/mcp
```

You should see `engram  ✓ connected  · 18 tools`.

---

## Step 2 — Add CLAUDE.md instructions

Registering the MCP server makes the tools *available*, but Claude won't use them automatically without instructions. Add the following to **`~/.claude/CLAUDE.md`**:

```markdown
## Memory System — engram MCP

engram is connected as an MCP server. Use it for all memory and recall operations.

### Recall (MANDATORY — do this before answering from training data)
ALWAYS call `memory_search` first when the user asks about:
- Past decisions, architecture choices, or context
- Customer or project specifics
- Anything the user has previously asked you to remember

Never use Bash grep, file search, or Obsidian MCP to recall knowledge.
The MCP result comes back in plain text — read it directly, do not spawn agents or run scripts.

### Save (do this when something worth keeping happens)
Call `memory_write` when:
- A significant technical decision is made
- The user says "remember this" or "note that"
- A meeting or call produces actionable context
- You discover something non-obvious about a codebase or system

### End of session
When the user wraps up or says goodbye, write a session summary:
`memory_write(content="Session [date]: ...", namespace="personal:me", tags=["session"])`

### Namespace guide
| Content type          | Namespace              |
|-----------------------|------------------------|
| Personal notes        | personal:me            |
| Shared team knowledge | org:myteam             |
| Project-specific      | project:myproject      |
| Customer context      | org:myteam:customers:customername |

### Tool reference
| Tool | When to use |
|---|---|
| `memory_search(query, namespace)` | Recall — always try this first |
| `memory_write(content, namespace, tags)` | Save a decision or learning |
| `memory_delete(memory_id, namespace)` | Remove an outdated entry |
| `secret_set(key_name, value, namespace)` | Store a credential in the encrypted vault |
| `secret_get(key_name, namespace)` | Retrieve a vault secret |
```

---

## Step 3 — Verify end-to-end

In a fresh Claude Code session:

```
What do you remember about the auth service?
```

Claude should call `memory_search` (visible in the tool-use output). If it runs Bash or searches files instead, the CLAUDE.md changes aren't loaded — restart Claude Code or run `Read ~/.claude/CLAUDE.md`.

To write a memory:

```
Remember that we use JWT with 24h expiry for the auth service
```

Claude should call `memory_write`. Verify at `http://localhost:8766/dashboard`.

The dashboard has two main tabs:
- **Memory Graph** — interactive force-directed graph of all memories and their relationships
- **API Keys** — create, list, and revoke runtime API keys without restarting the server

---

## What is and isn't automatic

| Behaviour | Automatic? | How |
|---|---|---|
| Recall when asked about past context | ✅ Yes (with CLAUDE.md) | Claude calls `memory_search` |
| Saving key decisions | ✅ Yes (with CLAUDE.md) | Claude calls `memory_write` |
| Full conversation transcript | ❌ No | Claude summarises — it doesn't record every message |
| Knowledge graph entity extraction | ✅ Yes (spaCy, no LLM needed) | Runs on every `memory_write` |
| Cross-session persistence | ✅ Yes | Stored in ArcadeDB on disk |
| Credential auto-redaction | ✅ Yes | Detected and redacted before any write reaches ArcadeDB |

---

## Zero-touch automation (optional but recommended)

The MCP approach above requires Claude to actively call `memory_write`. If Claude skips it, forgets, or a session ends abruptly, memories are lost.

**Zero-touch automation** wires engram directly into Claude Code's lifecycle via shell hooks, so memories are written and injected automatically — no agent action required.

Three hooks work together:

| Hook | When it fires | What it does |
|------|--------------|--------------|
| **UserPromptSubmit** | Every Claude Code prompt | Queries engram and injects relevant past context |
| **Stop** | Every turn end | Writes a session-state memory (branch, recent commits, CWD) |
| **git post-commit** | Every `git commit` | Writes the commit to engram (conventional prefix → memory type) |

### Quick install

The fastest path is the client installer — it writes all hook scripts, patches `~/.claude/settings.json`, and sets the global git hooks path in one shot:

```bash
# macOS / Linux / WSL
./install-client.sh                                              # local server (localhost:8766)
./install-client.sh --server http://eng.example.com:8766 \
                    --key engram-abc123                          # remote server
./install-client.sh --server http://localhost:8766 \
                    --key engram-abc123 \
                    --namespace personal:me                      # with explicit namespace
```

```powershell
# Windows (PowerShell 5.1+)
.\install-client.ps1
.\install-client.ps1 -Server http://eng.example.com:8766 -Key engram-abc123
```

The installer asks for anything not supplied as a flag, tests the server connection, and tells you at the end what it installed.

After running: **restart Claude Code** (quit and reopen). The hooks are active for every project immediately — no per-repo setup needed.

---

### Central config file — `~/.claude/hooks/engram.env`

All hooks read their connection details from a single config file so you only have one place to update when the server URL or key changes:

```bash
# ~/.claude/hooks/engram.env
ENGRAM_API=http://localhost:8766       # change to your remote server if needed
ENGRAM_KEY=engram-abc123               # your API key
ENGRAM_DEFAULT_NS=personal:me          # fallback namespace for all projects
ENGRAM_TOP_K=5                         # memories injected per prompt
```

Edit this file any time to point hooks at a different server — no need to reinstall.

---

### Per-project namespace — `.engram` file

Drop a `.engram` file in any repo root to override the default namespace for that project:

```bash
# in ~/work/my-project/.engram
namespace=project:my-project
```

**Namespace resolution order** (highest to lowest priority):

1. `.engram` file in the git repo root
2. `ENGRAM_NS_OVERRIDE` environment variable
3. `ENGRAM_DEFAULT_NS` in `~/.claude/hooks/engram.env`

This means every repo can silently route to its own namespace without touching the global config.

---

### `/engram` slash command

The installer creates a `~/.claude/commands/engram.md` slash command. Type `/engram` in any Claude Code session to see:

```
engram status

Namespaces
  • personal:me
  • project:my-project
  • org:myteam:engineering

Current namespace — project:my-project  (resolved from .engram file)

Recent memories
  [fact] 0.91 — session ended | project: my-project | branch: feature/auth-refactor | uncommitted: 3
  [decision] 0.88 — feat: replace JWT with session tokens...
  ...
```

If you pass a namespace argument (`/engram ns:project:other`), the command also shows how to set it permanently:

```bash
echo "namespace=project:other" > .engram
```

---

### Manual install (reference)

If you prefer to install hooks by hand or need to understand what the installer does:

**Step 1 — Create directories**

```bash
mkdir -p ~/.claude/hooks ~/.git-hooks ~/.claude/commands
```

**Step 2 — Write `engram.env`**

```bash
cat > ~/.claude/hooks/engram.env <<'EOF'
ENGRAM_API=http://localhost:8766
ENGRAM_KEY=your-engram-api-key
ENGRAM_DEFAULT_NS=personal:me
ENGRAM_TOP_K=5
EOF
```

**Step 3 — Write the hook scripts**

See `install-client.sh` in the repo for the full script content — the installer writes the same scripts verbatim.

**Step 4 — Register in `~/.claude/settings.json`**

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/yourname/.claude/hooks/engram-inject.sh",
            "timeout": 8
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/yourname/.claude/hooks/engram-session-write.sh",
            "timeout": 8,
            "async": true
          }
        ]
      }
    ]
  }
}
```

> Use **absolute paths** for `command`. Relative paths fail silently.  
> `async: true` on the Stop hook means it runs after Claude responds — the user never waits for it.

**Step 5 — Global git post-commit hook**

```bash
chmod +x ~/.git-hooks/post-commit
git config --global core.hooksPath ~/.git-hooks
```

> **Per-repo overrides:** create `.git/hooks/post-commit.local` in a specific repo — the global hook calls it automatically after writing to engram.

---

### What each hook writes

**inject hook** (reads): surfaces the 5 most semantically similar past memories as context before every Claude response. Claude sees them as `[engram: relevant past context]` at the top of its input — no extra tool calls needed.

**session hook** (writes): after every turn, records the current branch, recent commits, and CWD so the next session can orient itself without reading git history.

**commit hook** (writes): every `git commit` produces an engram memory tagged `git-commit`. `feat:`/`refactor:` commits become `decision` type; `fix:` commits become `incident` type. These surface automatically when you later ask "why did we change X?" or "what was the fix for Y?".

### Updated automation table

| Behaviour | Automatic? | How |
|---|---|---|
| Recall when asked about past context | ✅ Yes (with CLAUDE.md) | Claude calls `memory_search` |
| Context injected before every prompt | ✅ Yes (with inject hook) | UserPromptSubmit hook |
| Session state written after every turn | ✅ Yes (with stop hook) | Stop hook (async) |
| Every git commit recorded | ✅ Yes (with git hook) | `core.hooksPath` global hook |
| Saving key decisions | ✅ Yes (with CLAUDE.md) | Claude calls `memory_write` |
| Full conversation transcript | ❌ No | Claude summarises — it doesn't record every message |
| Knowledge graph entity extraction | ✅ Yes (spaCy, no LLM needed) | Runs on every `memory_write` |
| Cross-session persistence | ✅ Yes | Stored in ArcadeDB on disk |
| Credential auto-redaction | ✅ Yes | Detected and redacted before any write reaches ArcadeDB |

---

## Team setup — shared server with per-engineer hooks

**Server admin** — run once on the shared host:

```bash
./install-server.sh
# → generates ~/.engram/.env with ENGRAM_API_KEY and ARCADEDB_PASSWORD
# → starts ArcadeDB + engram API via Docker
```

Share the API URL and key from `~/.engram/.env` with each developer.

**Each developer** — run on their own machine:

```bash
./install-client.sh --server http://engram.yourcompany.com:8766 --key engram-abc123
# → installs hooks, sets their default namespace, restarts Claude Code
```

On Windows:
```powershell
.\install-client.ps1 -Server http://engram.yourcompany.com:8766 -Key engram-abc123
```

MCP config uses SSE transport for a remote server:

```json
{
  "mcpServers": {
    "engram": {
      "type": "sse",
      "url": "https://engram.yourcompany.com/sse",
      "headers": {
        "Authorization": "Bearer alice-personal-key"
      }
    }
  }
}
```

Each engineer uses their own API key. Keys are scoped to namespaces in `engram.yaml`:

```yaml
auth:
  api_keys:
    - key: "alice-personal-key"
      user_id: "alice"
      namespaces: ["personal:alice", "team:architecture", "project:*"]
    - key: "bob-personal-key"
      user_id: "bob"
      namespaces: ["personal:bob", "project:payments:*"]
```

See [enterprise-team-setup.md](enterprise-team-setup.md) for the full team deployment guide.

---

## Troubleshooting

**`/mcp` shows engram as disconnected**
- SSE: Is the server running? `curl http://localhost:8765/health`
- stdio: Does the binary exist and run? `ENGRAM_CONFIG=/path/to/engram.yaml ARCADEDB_PASSWORD=... engram-mcp-stdio`
- Did you fully restart Claude Code (quit, not just new tab)?
- Are all env vars set correctly? Check `ENGRAM_CONFIG` is an absolute path.

**Claude uses Bash/grep instead of memory_search**
- CLAUDE.md isn't loaded. Run `Read ~/.claude/CLAUDE.md` at session start.
- Or move the engram instructions to the project-level `CLAUDE.md` in your working directory.

**memory_search returns no results**
- Check namespace: `org:myteam` won't find results stored under `personal:me`
- Use `namespace="all"` to search everything: `memory_search(query="...", namespace="all")`
- Verify memories exist: `curl http://localhost:8766/api/v1/graph/stats?namespace=all -H "Authorization: Bearer your-key"`

**stdio takes too long to start**
- First start downloads/loads the `all-MiniLM-L6-v2` model (~90MB). Allow 40–60s.
- Subsequent starts are faster once the model is cached (`~/.cache/huggingface/`).
- Set `ENGRAM_LOG_LEVEL=WARNING` in the env block to reduce logging overhead.

**Claude spawns agents or runs scripts to parse results**
- Add to CLAUDE.md: `"MCP results come back as plain text — read them directly, never spawn agents to parse tool results."`
