# Connecting memnos to Cursor

Cursor supports MCP servers over stdio. memnos installs as a single package, so the server
command is just `memnos mcp`.

## Prerequisites

- memnos running (`memnos serve`, default `http://127.0.0.1:8900`) — see [quickstart](../quickstart.md).
- A scoped token + namespace:
  ```bash
  memnos namespace add user:alice
  memnos token alice --label cursor    # prints mnk_… once
  memnos grant alice user:alice
  ```

## Add the server

**Settings → MCP → Add new global MCP server**, or create `.cursor/mcp.json` in your project:

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

> If `memnos` isn't found, use the absolute path from `which memnos` (pipx installs to
> `~/.local/bin/memnos`).

Reload Cursor. In the MCP settings you should see **memnos** connected with three tools:

| tool | purpose |
|------|---------|
| `recall(query)` | ranked context for a query — no LLM at query time |
| `remember(text)` | store a message → raw turn + bi-temporal facts |
| `consolidate()` | distill facts into entity dossiers |

## Verify

Ask Cursor's agent to *"recall what I know about <topic>"*; it should call `recall`. Confirm
writes in the console at `http://127.0.0.1:8900/admin`.

## Per-project namespaces

Give each project its own `MEMNOS_NS` (and a token granted only to that namespace) by using
a per-project `.cursor/mcp.json`. memnos enforces the ACL server-side and audits every call.
