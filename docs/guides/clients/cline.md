# Connecting memnos to Cline (VS Code)

Cline supports MCP servers over stdio. memnos installs as a single package, so the server
command is just `memnos mcp`.

## Prerequisites

- memnos running (`memnos serve`, default `http://127.0.0.1:8900`) — see [quickstart](../quickstart.md).
- A scoped token + namespace:
  ```bash
  memnos namespace add user:alice
  memnos token alice --label cline      # prints mnk_… once
  memnos grant alice user:alice
  ```

## Add the server

Open the Cline MCP settings (**Cline panel → MCP Servers → Configure → Edit JSON**, which
opens `cline_mcp_settings.json`) and add:

```json
{
  "mcpServers": {
    "memnos": {
      "command": "memnos",
      "args": ["mcp"],
      "env": {
        "MEMNOS_URL": "http://127.0.0.1:8900",
        "MEMNOS_TOKEN": "mnk_...",
        "MEMNOS_NS": "user:alice"
      }
    }
  }
}
```

> If `memnos` isn't found, use the absolute path from `which memnos` (uv and pipx install to
> `~/.local/bin/memnos`).

Toggle the server on, then reload the Cline extension. memnos appears with three tools:

| tool | purpose |
|------|---------|
| `recall(query)` | ranked context for a query — no LLM at query time |
| `remember(text)` | store a message → raw turn + bi-temporal facts |
| `consolidate()` | distill facts into entity dossiers |

## Verify

Ask Cline to *"recall what we decided about <topic>"*; it should call `recall`. Confirm
writes in the console at `http://127.0.0.1:8900/admin`.
