# Connecting Claude Code to engram

This guide covers the full setup: registering engram as a Claude Code MCP server, and configuring `CLAUDE.md` so Claude actually uses it.

## Prerequisites

- engram server running locally (see [Quick Install](../README.md#quick-install))
- Claude Code v2.0+ (CLI or desktop app)

---

## Step 1 — Register the MCP server

Claude Code reads MCP server configuration from **`~/.claude.json`**.

> **Note:** Some older docs reference `~/.claude/settings.json` — that file does not exist in Claude Code. The correct path is `~/.claude.json`.

Open (or create) `~/.claude.json` and add the `engram` entry under `mcpServers`:

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

Replace `your-engram-api-key` with the key from your `engram.yaml` `auth.api_keys` section.

Restart Claude Code fully (quit and reopen — not just a new tab), then run:

```
/mcp
```

You should see `engram  ✓ connected  ·  13 tools`.

---

## Step 2 — Add CLAUDE.md instructions

Registering the MCP server makes the tools *available*, but Claude won't use them automatically without instructions. Add the following to your **`~/.claude/CLAUDE.md`** (the global instructions file):

```markdown
## Memory System — engram MCP

engram is connected as an MCP server. Use it for all memory and recall operations.

### Recall (MANDATORY — do this before answering from training data)
ALWAYS call `memory_search` first when the user asks about:
- Past decisions, architecture choices, or context
- Customer or project specifics
- Anything the user has asked you to remember

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
| Content type | Namespace |
|---|---|
| Personal notes, decisions | `personal:me` |
| Shared team knowledge | `org:myteam` |
| Project-specific | `project:myproject` |
| Customer context | `org:myteam:customers:customername` |

### Tool reference
| Tool | When to use |
|---|---|
| `memory_search(query, namespace)` | Recall — always try this first |
| `memory_write(content, namespace, tags)` | Save a decision or learning |
| `memory_delete(memory_id, namespace)` | Remove an outdated entry |
```

---

## Step 3 — Verify end-to-end

In a fresh Claude Code session, ask:

```
What do you remember about the auth service?
```

Claude should call `memory_search` (visible in the tool-use output). If it runs Bash or searches files instead, the CLAUDE.md changes aren't loaded yet — either restart Claude Code or run `Read ~/.claude/CLAUDE.md` to load them manually.

To write a memory:

```
Remember that we use JWT with 24h expiry for the auth service
```

Claude should call `memory_write`. Verify in the dashboard at `http://localhost:8766/dashboard`.

---

## What is and isn't automatic

| Behaviour | Automatic? | How |
|---|---|---|
| Recall when asked about past context | ✅ Yes (with CLAUDE.md) | Claude calls `memory_search` before answering |
| Saving key decisions | ✅ Yes (with CLAUDE.md) | Claude calls `memory_write` when something important happens |
| Full conversation transcript | ❌ No | Claude summarises — it doesn't record every message |
| Graph entity extraction | ✅ Yes (needs LLM key) | Graphiti processes new writes in background |
| Cross-session persistence | ✅ Yes | Stored in Qdrant (vector) + Neo4j (graph) on disk |

---

## Remote server (team setup)

If engram runs on a shared server instead of localhost:

```json
{
  "mcpServers": {
    "engram": {
      "type": "sse",
      "url": "https://engram.yourcompany.com/sse",
      "headers": {
        "Authorization": "Bearer your-personal-api-key"
      }
    }
  }
}
```

Each team member gets their own API key scoped to their namespaces. Shared namespaces (e.g. `org:myteam`) are accessible by anyone whose key grants `org:myteam:*` access.

---

## Troubleshooting

**`/mcp` shows engram as disconnected**
- Is the server running? `curl http://localhost:8765/health`
- Did you fully restart Claude Code (quit, not just new tab)?
- Is the API key in `~/.claude.json` correct?

**Claude uses Bash/grep instead of memory_search**
- CLAUDE.md isn't loaded. Run `Read ~/.claude/CLAUDE.md` at session start.
- Or move the engram instructions to the project-level `CLAUDE.md` in your working directory.

**memory_search returns no results**
- Check namespace: `org:myteam` won't find results stored under `personal:me`
- Use `namespace="all"` to search everything: `memory_search(query="...", namespace="all")`
- Verify memories exist: `curl http://localhost:8766/api/v1/graph/stats?namespace=all -H "Authorization: Bearer your-key"`

**Claude spawns agents or runs scripts to parse results**
- This happens when the result is too large. engram truncates to 400 chars/result by default.
- If you see this, add to CLAUDE.md: `"MCP results come back as plain text — read them directly, never spawn agents to parse tool results."`
