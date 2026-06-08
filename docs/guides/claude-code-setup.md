# Connecting Claude Code to memnos

Give Claude Code persistent, governed memory across sessions. Three pieces, all optional but
best together:

1. **Hooks** — memnos *automatically* injects relevant memory before each prompt and saves
   the exchange after (zero tool calls). This is the "it just remembers" layer.
2. **MCP tools** — Claude can *explicitly* call `recall` / `remember` / `consolidate`.
3. **`/memnos` slash command** — manual recall + status on demand.

> **Critical gotcha (read this):** Claude Code reads MCP server definitions from
> **`~/.claude.json`**, *not* `~/.claude/settings.json`. Hooks, however, live in
> `~/.claude/settings.json`. Putting an MCP server in settings.json makes it silently
> invisible. Use `claude mcp add` (below) and you won't hit this.

## Prerequisites

- memnos installed + running — `memnos serve` (or the launchd service), `http://127.0.0.1:8900`.
- A token + namespace:
  ```bash
  memnos namespace add user:thameem
  memnos token thameem --label "claude code"     # prints mnk_… once
  memnos grant thameem user:thameem
  ```

---

## 1. Hooks — automatic recall + autosave (recommended)

memnos ships two hook scripts: `memnos-recall.py` (UserPromptSubmit → inject memory before
Claude answers) and `memnos-remember.py` (Stop → save the turn after). Both **fail open**
(never block your prompt) and use no LLM at query time.

Add to **`~/.claude/settings.json`** (`<REPO>` = your memnos checkout, e.g.
`/Users/you/git/memnos/poc`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "MEMNOS_URL=http://127.0.0.1:8900 MEMNOS_NS=user:thameem MEMNOS_TOKEN=mnk_... <REPO>/.venv/bin/python <REPO>/integrations/claude-code/memnos-recall.py",
        "timeout": 15 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command",
        "command": "MEMNOS_URL=http://127.0.0.1:8900 MEMNOS_NS=user:thameem MEMNOS_TOKEN=mnk_... <REPO>/.venv/bin/python <REPO>/integrations/claude-code/memnos-remember.py",
        "timeout": 15 } ] }
    ]
  }
}
```

When it's working you'll see a **"## Relevant memories (memnos)"** block prepended to your
prompts. That's the recall hook. (Hooks reload on the next prompt — no restart needed.)

---

## 2. MCP server — explicit tools

Use the CLI so it lands in the right file (`~/.claude.json`):

```bash
claude mcp add memnos \
  --env MEMNOS_URL=http://127.0.0.1:8900 \
  --env MEMNOS_TOKEN=mnk_... \
  --env MEMNOS_NS=user:thameem \
  -- <REPO>/.venv/bin/python <REPO>/integrations/claude-code/memnos_mcp.py
```

(With a packaged install, `-- memnos mcp` works instead of the python+script form.)

Equivalent manual entry in **`~/.claude.json`** under top-level `"mcpServers"`:

```json
"memnos": {
  "command": "<REPO>/.venv/bin/python",
  "args": ["<REPO>/memnos_mcp.py"],
  "env": {
    "MEMNOS_URL": "http://127.0.0.1:8900",
    "MEMNOS_TOKEN": "mnk_...",
    "MEMNOS_NS": "user:thameem"
  }
}
```

**Restart Claude Code**, then `/mcp` should show `memnos ✓ connected` with three tools:

| tool | what it does |
|------|--------------|
| `recall(query)` | ranked context for a query — no LLM at query time |
| `remember(text)` | store a message → raw turn + bi-temporal facts |
| `consolidate()` | distill stored facts into entity dossiers |

---

## 3. `/memnos` slash command

Create **`~/.claude/commands/memnos.md`** (a prompt template). `/memnos <query>` recalls from
your namespace and answers from it; `/memnos` alone shows server status. See the shipped
example in this repo. Slash commands are picked up immediately (no restart).

---

## Namespaces — how a session attaches to one

A Claude Code "session" attaches to a memnos **namespace** via the `MEMNOS_NS` env on the
hook/MCP commands, resolved by `memnos_ns.resolve()` in this order:

1. **`MEMNOS_NS`** env (if set to anything other than `auto`) — explicit pin. *(This is what
   the configs above use: every session → `user:thameem`.)*
2. **`proj:<git-repo-name>`** — if `MEMNOS_NS=auto`, each repo automatically gets its own
   namespace.
3. **`proj:<cwd-basename>`** — fallback when not in a git repo.

So you have three ways to attach a session/project to a namespace:

- **Pin globally** — set `MEMNOS_NS=<ns>` in the global hook/MCP env (current setup → `user:thameem`).
- **Auto per-repo** — set `MEMNOS_NS=auto`; then grant the token a wildcard once:
  `memnos grant thameem 'proj:*'`. Each repo silently routes to `proj:<repo>`.
- **Pin one project** — drop a project-level **`.claude/settings.json`** in that repo whose
  hook commands set `MEMNOS_NS=<that-project's-ns>`. It overrides the global env for that repo
  only. (Grant the token to that namespace first.)

Whichever namespace you target, the token in the env **must have a grant** for it, or
recall/remember get an ACL denial. Check with `memnos whoami <token>`.

---

## What is and isn't automatic

| Behaviour | Automatic? | How |
|---|---|---|
| Recall relevant memory before each prompt | ✅ (hooks) | UserPromptSubmit → `memnos-recall.py` |
| Save the turn after each response | ✅ (hooks) | Stop → `memnos-remember.py` |
| Raw turns + bi-temporal fact extraction | ✅ | server `remember` (LLM at write only) |
| Entities + relations | ✅ | derived on each write |
| Entity dossiers (consolidation) | ▶ on demand | `consolidate` / the sleep-pass job |
| **Episodes** (`episodic` table) | ❌ **not yet** | schema exists but the engine doesn't populate it |
| Secret redaction before storage | ✅ | server-side on every write |

---

## Troubleshooting

- **`/mcp` shows an old/other server but not memnos** → the memnos entry is in the wrong file.
  MCP servers must be in **`~/.claude.json`** (use `claude mcp add`), not
  `~/.claude/settings.json`. Restart Claude Code after adding.
- **No "## Relevant memories" block** → the recall hook isn't firing. Confirm the server is up
  (`curl localhost:8900/healthz`) and the hook command's `<REPO>` paths + token are correct.
- **ACL denied / empty recall** → the token lacks a grant for `MEMNOS_NS`. `memnos whoami <token>`
  shows grants; `memnos grant <principal> <namespace>` to fix.
- **Recall/remember silently do nothing** → both hooks fail open by design; check the server
  logs and that `MEMNOS_TOKEN` is valid (`memnos whoami <token>`).
