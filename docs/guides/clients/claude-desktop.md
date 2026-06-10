# Connecting memnos to Claude Desktop

Claude Desktop (macOS / Windows) speaks MCP over stdio — wiring memnos in gives your
desktop Claude **persistent, governed long-term memory**: it can recall facts you told it
in past chats (or that your other memnos-connected agents stored) and save new ones.

## One command

```bash
memnos agent-setup claude-desktop
```

This mints a scoped token and writes the memnos MCP server into Claude Desktop's config
(merging — your other MCP servers are preserved; the file is backed up first):

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

Then **fully quit Claude Desktop** (Cmd-Q on macOS / quit from the system tray on Windows —
closing the window is not enough) and reopen it. The memnos tools appear under the
**search-and-tools** icon in the chat input.

What it writes:

```json
{
  "mcpServers": {
    "memnos": {
      "command": "/Users/you/.local/bin/memnos",
      "args": ["mcp"],
      "env": {
        "MEMNOS_URL": "http://127.0.0.1:8900",
        "MEMNOS_TOKEN": "mnk_...",
        "MEMNOS_NS": "user:you"
      }
    }
  }
}
```

> Note the **absolute** `command` path — Claude Desktop launches MCP servers with a minimal
> `PATH` that doesn't include `~/.local/bin`, so a bare `memnos` fails to resolve there.
> `agent-setup` handles this for you.

## Usage notes

- Claude Desktop gets the memnos MCP **tools** (`recall`, `recall_wide`, `remember`,
  `reconcile_claim`, `get_entity`, `get_provenance`). There are no lifecycle hooks (Claude
  Code only), so memory isn't auto-injected — Claude calls the tools when relevant. Adding
  a line to your Claude **profile preferences** ("use the memnos tools to recall context
  and store durable facts") makes it consistent.
- The memnos server must be running (`memnos start`, or better `memnos autostart` so it
  survives reboots). If it's down, the tools return a clear "server is not running" message
  instead of hanging.
- **Same memory as Claude Code:** if you also ran `memnos claude-setup`, both clients share
  the same namespaces — a fact saved from the terminal is recallable in the desktop app
  (subject to the token's grants).

## Troubleshooting

- **Tools don't appear** → you didn't fully quit (Cmd-Q / tray-quit), or check the MCP logs:
  `~/Library/Logs/Claude/mcp*.log` (macOS) / `%APPDATA%\Claude\logs\` (Windows).
- **`spawn memnos ENOENT` in the log** → the command path isn't absolute; re-run
  `memnos agent-setup claude-desktop` (it writes the absolute path).
- **Tools error with "unauthorized"** → token was revoked; re-run agent-setup to mint a
  fresh one.
