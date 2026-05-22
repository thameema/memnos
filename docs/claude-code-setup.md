# Connecting Claude Code to engram

This guide covers the full setup: registering engram as a Claude Code MCP server, and configuring `CLAUDE.md` so Claude actually uses it.

## Prerequisites

- engram installed and ArcadeDB running (see [quickstart.md](quickstart.md))
- Claude Code v2.0+ (CLI or desktop app)

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

## Team setup — shared server with per-engineer API keys

If engram runs on a shared server:

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
