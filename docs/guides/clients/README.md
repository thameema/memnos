# memnos Client Setup Guides

All guides use the single-package install: `./install.sh` → `memnos setup` → `memnos serve`
(server on `http://127.0.0.1:8900`, console at `/admin`). MCP clients use the bundled
stdio adapter, `memnos mcp`; everyone else uses the REST API or the `memnos-sdk`.

| Client | Connection | Guide |
|--------|-----------|-------|
| Claude Code | MCP (`memnos mcp`) + automatic hooks | [Setup](../claude-code-setup.md) |
| Cursor | MCP (`memnos mcp`, stdio) | [Setup](cursor.md) |
| Windsurf | MCP (`memnos mcp`, stdio) | [Setup](windsurf.md) |
| Cline (VS Code) | MCP (`memnos mcp`, stdio) | [Setup](cline.md) |
| Codex CLI | REST / `memnos-sdk` | [Setup](codex.md) |
| ChatGPT | REST via custom GPT Action (tunnel) | [Setup](chatgpt.md) |
| Open WebUI | REST via OpenAPI tool server | [Setup](openwebui.md) |

**MCP tools** (stdio clients): `recall(query)`, `remember(text)`, `consolidate()`.
**REST** (everyone): `POST /recall` and `POST /remember`, Bearer token, namespace-scoped.

> The open-source server speaks REST + **stdio** MCP. There is no hosted MCP-over-HTTP/SSE
> endpoint, so HTTP-only clients (ChatGPT, Open WebUI) use the REST API.
