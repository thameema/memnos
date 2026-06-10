# memnos Client Setup Guides

All guides use the single-package install: `uv tool install memnos` → `memnos setup` →
`memnos start` (server on `http://127.0.0.1:8900`, console at `/admin`).

## One-command setup per agent

memnos wires itself into your agent for you — no manual config editing:

```bash
memnos claude-setup            # Claude Code: MCP + hooks (auto inject/save) + /memnos + CLAUDE.md
memnos agent-setup codex       # Codex CLI:  MCP server in ~/.codex/config.toml + AGENTS.md
memnos agent-setup cursor      # Cursor:     MCP server in ~/.cursor/mcp.json
memnos agent-setup windsurf    # Windsurf:   MCP server in ~/.codeium/windsurf/mcp_config.json
memnos agent-setup claude-desktop
memnos agent-setup openclaw    # OpenClaw:   MCP server under mcp.servers in ~/.openclaw/openclaw.json
memnos agent-setup hermes      # Hermes Agent (Nous): MCP server in ~/.hermes/config.yaml
```

Each mints a scoped token, is idempotent, and backs up files it edits. `memnos setup` also
offers `claude-setup` automatically when it detects Claude Code. **Restart the agent after.**

> **Claude Code is the only one with lifecycle hooks** (auto-recall before each prompt,
> auto-save after). Every other agent gets the memnos MCP **tools** (`recall`, `recall_wide`,
> `remember`, `reconcile_claim`, …) which it calls explicitly.

MCP clients use the bundled stdio adapter, `memnos mcp`; non-MCP clients use the REST API or
the `memnos-sdk`.

| Client | Connection | Guide |
|--------|-----------|-------|
| Claude Code | MCP (`memnos mcp`) + automatic hooks | [Setup](../claude-code-setup.md) |
| Claude Desktop | MCP (`memnos mcp`, stdio) | [Setup](claude-desktop.md) |
| Cursor | MCP (`memnos mcp`, stdio) | [Setup](cursor.md) |
| Windsurf | MCP (`memnos mcp`, stdio) | [Setup](windsurf.md) |
| Cline (VS Code) | MCP (`memnos mcp`, stdio) | [Setup](cline.md) |
| OpenClaw | MCP (`memnos mcp`, stdio) | [Setup](openclaw.md) |
| Hermes Agent (Nous) | MCP (`memnos mcp`, stdio) | [Setup](hermes.md) |
| Codex CLI | REST / `memnos-sdk` | [Setup](codex.md) |
| ChatGPT | REST via custom GPT Action (tunnel) | [Setup](chatgpt.md) |
| Open WebUI | REST via OpenAPI tool server | [Setup](openwebui.md) |

**MCP tools** (stdio clients): `recall(query)`, `remember(text)`, `consolidate()`.
**REST** (everyone): `POST /recall` and `POST /remember`, Bearer token, namespace-scoped.

> The open-source server speaks REST + **stdio** MCP. There is no hosted MCP-over-HTTP/SSE
> endpoint, so HTTP-only clients (ChatGPT, Open WebUI) use the REST API.
