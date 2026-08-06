# memnos × any MCP client (Cursor, Windsurf, Zed, …)

memnos speaks the **Model Context Protocol** over **stdio** (`memnos_mcp.py`, spawned as
a subprocess) and over **streamable-HTTP** (mounted at `{MEMNOS_URL}/mcp` on the same
server that already serves the REST API), so any MCP-compatible agent can use whichever
transport it supports — both are wired to the SAME tool definitions:
`recall(query)`, `remember(text)`, `consolidate()`, and the rest of the tool surface.

Prereq: a running memnos server + token (see [`../../QUICKSTART.md`](../../QUICKSTART.md)).

## Generic MCP config (stdio)
Point the client at the stdio command. With the package installed (`uv tool install memnos`):
```jsonc
{
  "command": "memnos",
  "args": ["mcp"],
  "env": {
    "MEMNOS_URL": "http://127.0.0.1:8900",
    "MEMNOS_TOKEN": "mnk_...",
    "MEMNOS_NS": "user:alice"        // namespace = per-user / per-team / per-agent scope
  }
}
```
(From-source checkout: `"command": "/abs/path/.venv/bin/python", "args":
["/abs/path/memnos_mcp.py"]`.)

## Streamable-HTTP config (no subprocess, survives a memnos restart)
Point the client at the already-running server instead of spawning one. This is the
better fit when the client can't spawn subprocesses, or when you want the MCP connection
to keep working across a `memnos` upgrade/restart without the agent needing to reconnect:
```jsonc
{
  "type": "http",
  "url": "http://127.0.0.1:8900/mcp",
  "headers": {
    "Authorization": "Bearer mnk_...",
    "X-Memnos-Namespace": "user:alice"   // required if the token is granted on more than one namespace
  }
}
```
`memnos agent-setup claude-code --transport http` / `memnos agent-setup omnigent
--transport http` generate this automatically (stdio remains the default for both).

**Scope note:** this transport ships **opt-in only** (issue #37 Layer 1). `--transport`
defaults to `stdio` for every target, so #37's own acceptance bar — *no `memnos mcp` child
processes exist* — is **not** met by the default, documented command; existing installs
(and any new install that doesn't pass `--transport http` explicitly) still spawn a
per-session stdio subprocess, and the CRITICAL subprocess-sprawl bug #37 opened with
remains open for them. Flipping the default is tracked separately in issue #39, once the
HTTP transport has run in the field for a while. Pass `--transport http` explicitly today
to actually close #37 for your own setup.

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
