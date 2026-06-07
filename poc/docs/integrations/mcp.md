# memnos × any MCP client (Cursor, Windsurf, Zed, …)

memnos speaks the **Model Context Protocol** over stdio via `poc/memnos_mcp.py`, so any
MCP-compatible agent can use it. The tool surface is identical everywhere:
`recall(query)`, `remember(text)`, `consolidate()`.

Prereq: a running memnos server + token (see [`../../QUICKSTART.md`](../../QUICKSTART.md)).

## Generic MCP config
Point the client at the stdio command:
```jsonc
{
  "command": "/abs/path/memnos/poc/.venv/bin/python",
  "args": ["/abs/path/memnos/poc/memnos_mcp.py"],
  "env": {
    "MEMNOS_URL": "http://127.0.0.1:8900",
    "MEMNOS_TOKEN": "mnk_...",
    "MEMNOS_NS": "user:alice"        // namespace = per-user / per-team / per-agent scope
  }
}
```

## Per-client locations
| Client | Where to add it |
|--------|-----------------|
| **Cursor** | Settings → MCP → Add server (or `.cursor/mcp.json`) |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` → `mcpServers` |
| **Zed** | `settings.json` → `context_servers` |
| **Claude Code** | see [`claude-code.md`](claude-code.md) (also supports hooks) |
| **Claude Desktop** | `claude_desktop_config.json` → `mcpServers` |

## REST / SDK alternative
No MCP? Use the HTTP API directly (`/remember`, `/recall`) — see the endpoint table in
[`../../QUICKSTART.md`](../../QUICKSTART.md). Every language with an HTTP client works.

## Multi-agent / multi-tenant
Give each agent or user its own `MEMNOS_NS` and a token granted only to that namespace.
memnos enforces the ACL server-side and audits every access — agents can share a namespace
for coordination or stay isolated, your choice.
