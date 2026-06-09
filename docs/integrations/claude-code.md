# memnos × Claude Code

Give Claude Code persistent, governed memory across sessions. Two integration paths —
use either or both:

- **MCP tools** — Claude can *explicitly* call `recall` / `remember` / `consolidate`.
- **Hooks** — memnos *automatically* injects relevant memory before each prompt and
  captures the exchange after, with zero tool calls.

Prereq: a running memnos server + a token (see [`../../QUICKSTART.md`](../../QUICKSTART.md)).

---

## Option A — MCP server (explicit tools)

If you installed the package (`pipx install memnos`), the MCP server is just `memnos mcp`:
```jsonc
{
  "mcpServers": {
    "memnos": {
      "command": "memnos",
      "args": ["mcp"],
      "env": {
        "MEMNOS_URL": "http://127.0.0.1:8900",
        "MEMNOS_TOKEN": "mnk_...",          // memnos token <principal>
        "MEMNOS_NS": "user:alice"           // this agent's namespace scope
      }
    }
  }
}
```
(For a from-source checkout, use the venv Python instead: `"command":
"/abs/path/.venv/bin/python", "args": ["/abs/path/memnos_mcp.py"]`.)
Restart Claude Code. It now has three tools:
| tool | what it does |
|------|--------------|
| `recall(query)` | returns the ranked context block for a query (no LLM at query time) |
| `remember(text)` | stores a message → raw turn + extracted bi-temporal facts |
| `consolidate()` | distills stored facts into entity dossiers (multi-hop pre-join) |

## Option B — Hooks (automatic, no tool calls)

memnos ships two hook scripts in `integrations/claude-code/`:
- `memnos-recall.py` — **UserPromptSubmit**: injects relevant memories *before* Claude answers.
- `memnos-remember.py` — **Stop**: stores the exchange *after* Claude responds.

Both **fail open** (never block your prompt) and use no LLM at query time. Wire them in
`~/.claude/settings.json`:
```jsonc
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "MEMNOS_NS=user:alice /abs/path/memnos/.venv/bin/python /abs/path/memnos/integrations/claude-code/memnos-recall.py",
        "timeout": 15 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command",
        "command": "MEMNOS_NS=user:alice MEMNOS_TOKEN=mnk_... /abs/path/memnos/.venv/bin/python /abs/path/memnos/integrations/claude-code/memnos-remember.py" } ] }
    ]
  }
}
```
(The recall hook needs no token if your server allows read on that namespace; the remember
hook writes, so it needs `MEMNOS_TOKEN`.)

## Which to use?
- **Hooks** = effortless, always-on memory (recommended for daily coding).
- **MCP** = explicit control when you want Claude to decide *when* to remember/recall.
- Both together = automatic recall + Claude can also deliberately store key decisions.

## Notes
- **Answerer = Claude itself.** memnos returns *context*; Claude does the reasoning. This is
  why a strong agent gets the best results for free (the +6pp answerer effect from
  [`../../benchmarks/`](../../benchmarks/README.md) applies automatically).
- **Governance:** every call is token-authed, namespace-ACL'd, and audited
  (`python memnos_admin.py audit`).
- **Help memnos learn what helped:** call `/feedback {helpful:true}` after a useful recall.
