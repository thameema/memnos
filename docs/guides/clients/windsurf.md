# Connecting memnos to Windsurf

## One command

```bash
memnos agent-setup windsurf
```

Writes the memnos MCP server to `~/.codeium/windsurf/mcp_config.json` (mints a scoped token),
idempotent + backed up. **Refresh the MCP panel** in Windsurf. The manual config below is
only if you'd rather do it by hand.

---

Windsurf (Cascade) supports MCP servers over stdio. memnos installs as a single package, so
the server command is just `memnos mcp`.

## Prerequisites

- memnos running (`memnos serve`, default `http://127.0.0.1:8900`) — see [quickstart](../quickstart.md).
- A scoped token + namespace:
  ```bash
  memnos namespace add user:alice
  memnos token alice --label windsurf   # prints mnk_… once
  memnos grant alice user:alice
  ```

## Add the server

Edit **`~/.codeium/windsurf/mcp_config.json`** (Windsurf → Settings → Cascade → MCP servers
→ *Edit raw config*):

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

> If `memnos` isn't found on Windsurf's PATH, use the absolute path from `which memnos`
> (pipx installs to `~/.local/bin/memnos`).

Click **Refresh** in the MCP panel. memnos appears with three tools:

| tool | purpose |
|------|---------|
| `recall(query)` | ranked context for a query — no LLM at query time |
| `remember(text)` | store a message → raw turn + bi-temporal facts |
| `consolidate()` | distill facts into entity dossiers |

## Verify

Ask Cascade to *"recall what I decided about <topic>"*; it should call `recall`. Confirm
writes in the console at `http://127.0.0.1:8900/admin`.
