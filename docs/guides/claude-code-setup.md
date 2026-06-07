# Connecting Claude Code to memnos

Give Claude Code persistent, governed memory across sessions. Two paths — use either or both:

- **MCP tools** — Claude explicitly calls `recall` / `remember` / `consolidate`.
- **Hooks** — memnos *automatically* injects relevant memory before each prompt and
  captures the exchange after, with zero tool calls.

## Prerequisites

- memnos installed and running — see [quickstart.md](quickstart.md)
  (`./install.sh` → `memnos setup` → `memnos serve`, server on `http://127.0.0.1:8900`).
- A scoped token + namespace:
  ```bash
  memnos namespace add user:alice
  memnos token alice --label "claude code"     # prints mnk_… once — copy it
  memnos grant alice user:alice
  ```
- Claude Code v2.0+ (CLI or desktop).

---

## Option A — MCP server (explicit tools)

Because memnos installs as a single package, the MCP server is just `memnos mcp` (a stdio
adapter — no repo paths, no second process to babysit). Add to **`~/.claude/settings.json`**:

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

Fully restart Claude Code, then run `/mcp` — you should see `memnos ✓ connected`. Tools:

| tool | what it does |
|------|--------------|
| `recall(query)` | ranked context block for a query — **no LLM at query time** |
| `remember(text)` | stores a message → raw turn + extracted bi-temporal facts |
| `consolidate()` | distills stored facts into entity dossiers (multi-hop pre-join) |

> If `memnos` isn't on the PATH Claude Code uses (common with `pipx`), use the absolute
> path from `which memnos` as `command`.

---

## Option B — Hooks (automatic, no tool calls)

memnos ships two hook scripts that wire into Claude Code's lifecycle:

- `memnos-recall.py` — **UserPromptSubmit**: injects relevant memories *before* Claude answers.
- `memnos-remember.py` — **Stop**: stores the exchange *after* Claude responds.

Both **fail open** (never block your prompt) and use no LLM at query time. Find them under
the installed package's `integrations/claude-code/` directory (or in the repo at
`poc/integrations/claude-code/`). Wire them in **`~/.claude/settings.json`**:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "MEMNOS_NS=user:alice MEMNOS_URL=http://127.0.0.1:8900 python3 /path/to/integrations/claude-code/memnos-recall.py",
        "timeout": 15 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command",
        "command": "MEMNOS_NS=user:alice MEMNOS_TOKEN=mnk_... python3 /path/to/integrations/claude-code/memnos-remember.py",
        "async": true } ] }
    ]
  }
}
```

The recall hook needs no token if the server allows read on that namespace; the remember
hook writes, so it needs `MEMNOS_TOKEN`.

---

## Which to use?

- **Hooks** = effortless, always-on memory (recommended for daily coding).
- **MCP** = explicit control when you want Claude to decide *when* to remember/recall.
- **Both** = automatic recall + Claude can also deliberately store key decisions.

---

## Optionally tell Claude to lean on it

Add to **`~/.claude/CLAUDE.md`** so Claude prefers memnos over ad-hoc file search:

```markdown
## Memory — memnos
Call `recall` first when I reference past decisions, preferences, people, or prior context.
Call `remember` when a key decision is made or I say "remember this".
MCP results are plain text — read them directly; don't spawn agents to parse them.
```

---

## Verify

```bash
curl -s localhost:8900/healthz            # {"ok": true}
```

In a fresh session ask: *"What do you remember about the auth service?"* — Claude should
call `recall`. Then *"Remember we use JWT with 24h expiry"* — it should call `remember`.
Inspect everything in the console at **http://127.0.0.1:8900/admin**.

---

## Troubleshooting

- **`/mcp` shows disconnected** — fully restart Claude Code (quit, not a new tab); confirm
  `memnos mcp` runs in a terminal with the same env; use the absolute path to `memnos`.
- **Recall returns nothing** — the token only sees granted namespaces. `memnos whoami <token>`
  shows the grants; check `MEMNOS_NS` matches one of them.
- **Auth errors** — the token must have a *write* grant for `remember`; read is enough for `recall`.
