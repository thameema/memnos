# Connecting memnos to Cline (VS Code)

Cline supports MCP servers via two config locations: global VS Code settings or a per-project file.

## Global Config (VS Code settings.json)

Open Command Palette → `Preferences: Open User Settings (JSON)` and add:

```json
{
  "cline.mcpServers": {
    "memnos": {
      "type": "sse",
      "url": "http://localhost:8765/sse",
      "headers": { "Authorization": "Bearer memnos-local-dev-key" }
    }
  }
}
```

## Project Config (.cline/mcp_settings.json)

Create `.cline/mcp_settings.json` in your project root:

```json
{
  "mcpServers": {
    "memnos": {
      "type": "sse",
      "url": "http://localhost:8765/sse",
      "headers": { "Authorization": "Bearer memnos-local-dev-key" }
    }
  }
}
```

Project config takes precedence over global config when present.

## After Setup

1. Reload the Cline extension (or restart VS Code)
2. Open the Cline panel → MCP tab — memnos tools should appear as enabled
3. Verify by asking Cline: `search my memories for "X"` — it will invoke `memory_search` and return results inline

## Available Tools

Once connected, Cline can call all memnos tools:
- `memory_search` — recall knowledge
- `memory_write` — save new knowledge
- `memory_delete` — remove stale entries
- `get_related` — graph traversal
- `graph_query` — AQL queries
