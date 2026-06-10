# memnos × Claude Code

Give Claude Code persistent, governed memory across sessions. Two integration paths —
use either or both:

- **MCP tools** — Claude can *explicitly* call `recall` / `remember` / `consolidate`.
- **Hooks** — memnos *automatically* injects relevant memory before each prompt and
  captures the exchange after, with zero tool calls.

Prereq: a running memnos server + a token (see [`../../QUICKSTART.md`](../../QUICKSTART.md)).

---

## Fastest — one command

```bash
memnos claude-setup
```

This wires **everything** for you and is idempotent (it backs up any file it edits):
the MCP server in `~/.claude.json`, the auto recall/save **hooks** in
`~/.claude/settings.json`, a `/memnos` slash command, and a memnos section in your
`CLAUDE.md`. It also mints a scoped token. **Restart Claude Code afterwards** and verify
with `/mcp`. (`memnos setup` runs this automatically when it detects Claude Code.)

The manual paths below are the same wiring, by hand, if you'd rather not run the helper.

---

## Option A — MCP server (explicit tools)

If you installed the package (`uv tool install memnos`), the MCP server is just `memnos mcp`:
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
(Running from a source checkout without installing the package? Point `command` at your
venv Python and `args` at `memnos_mcp.py`: `"command": "/abs/path/.venv/bin/python",
"args": ["/abs/path/memnos_mcp.py"]`.)
Restart Claude Code. It now has three tools:
| tool | what it does |
|------|--------------|
| `recall(query)` | returns the ranked context block for a query (no LLM at query time) |
| `remember(text)` | stores a message → raw turn + extracted bi-temporal facts |
| `consolidate()` | distills stored facts into entity dossiers (multi-hop pre-join) |

## Option B — Hooks (automatic, no tool calls)

The packaged hooks ship inside the `memnos` CLI — `memnos hook recall` (**UserPromptSubmit**:
injects relevant memories *before* Claude answers) and `memnos hook remember` (**Stop**:
stores the exchange *after* Claude responds). Both **fail open** (never block your prompt)
and use no LLM at query time. `memnos claude-setup` writes these for you; to do it by hand,
add to `~/.claude/settings.json`:
```jsonc
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "MEMNOS_URL=http://127.0.0.1:8900 MEMNOS_NS=user:alice MEMNOS_TOKEN=mnk_... memnos hook recall",
        "timeout": 15 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command",
        "command": "MEMNOS_URL=http://127.0.0.1:8900 MEMNOS_NS=user:alice MEMNOS_TOKEN=mnk_... memnos hook remember",
        "timeout": 15 } ] }
    ]
  }
}
```
(The remember hook writes, so its token needs write on the namespace; recall needs read.)

## Which to use?
- **Hooks** = effortless, always-on memory (recommended for daily coding).
- **MCP** = explicit control when you want Claude to decide *when* to remember/recall.
- Both together = automatic recall + Claude can also deliberately store key decisions.

## Notes
- **Answerer = Claude itself.** memnos returns *context*; Claude does the reasoning. This is
  why a strong agent gets the best results for free (the +6pp answerer effect from
  [`../../benchmarks/`](../../benchmarks/README.md) applies automatically).
- **Governance:** every call is token-authed, namespace-ACL'd, and audited
  (`memnos audit`).
- **Help memnos learn what helped:** call `/feedback {helpful:true}` after a useful recall.
