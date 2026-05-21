# Connecting Claude Code to engram

## Local mode (stdio — no network)

Claude Code spawns engram as a subprocess. No server needed.

Install engram locally:
```bash
cd ~/path/to/engram
pip install -e packages/core -e packages/mcp-server -e packages/orchestrator -e packages/api -e packages/learning -e packages/gateway
```

Add to `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "engram": {
      "command": "python",
      "args": ["-m", "engram_mcp.transports.stdio"],
      "env": {
        "ENGRAM_API_KEY": "your-key",
        "ENGRAM_CONFIG": "/absolute/path/to/engram.yaml",
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```

## Remote mode (SSE — server running elsewhere)

engram is deployed on a server (local Docker or remote VPS).

Add to `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "engram": {
      "url": "https://engram.yourdomain.com/sse",
      "apiKey": "your-engram-api-key"
    }
  }
}
```

For local Docker, use `http://localhost:8765/sse`.

## Verify the connection

In Claude Code:
```
/mcp
```

You should see `engram` listed with these tools:
- `memory_search` — search persistent memory
- `memory_write` — write to memory
- `memory_delete` — delete a memory
- `graph_query` — Cypher query on knowledge graph
- `get_entity` — look up an entity
- `spawn_task` — fork a background worker
- `get_task_result` — get task output
- `list_tasks` — list tasks
- `get_heuristics` — view learned rules
- `list_agents` — view available agents

## Available namespaces

Every memory operation requires a `namespace` parameter:

| Format | Who has access | Example |
|--------|---------------|---------|
| `personal:{id}` | You only | `personal:alice` |
| `org:{name}` | Your whole team | `org:acme` |
| `project:{name}` | Project members | `project:backend` |

Use `personal:me` as your default personal namespace.

## Tips

- Start every session with: `Use memory_search to find context about [current task]`
- End every session with: `Use memory_write to save key decisions from this session`
- Use `spawn_task` for long-running work so Claude Code can continue with other things
