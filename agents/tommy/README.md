# Tommy — Personal Coding Orchestrator

Tommy is a thin coordinator built on [memnos](../../). It decomposes coding
tasks, routes work to the right LLM harnesses (claude, codex, hermes, aider,
goose, …), manages memory via memnos, and keeps sessions persistent across
context windows.

Tommy is **not** a coding agent. It never reads code to analyse it — it
dispatches agents that do.

---

## Install (via uv, as part of memnos)

```bash
# Install memnos (which includes tommy)
uv pip install "memnos[tommy]"

# Or install tommy standalone from source
cd agents/tommy
uv pip install -e .

# First-time setup
tommy --install
```

`--install` writes:
- `~/.memnos/agents/tommy/tommy.conf` — your config (edit TOMMY_USER, ORG, PROJECTS)
- `~/.memnos/agents/tommy/prompts/` — bundled prompts (customisable)
- `~/.claude/commands/memnos*.md` — Claude Code `/memnos*` slash commands

---

## Configuration

`~/.memnos/agents/tommy/tommy.conf` (KEY=VALUE):

```ini
TOMMY_USER=Thameem
ORG=hc
PROMPTS_DIR=~/.memnos/agents/tommy/prompts

TOMMY_NS=user:thameema:tommy
DEFAULT_NS=org:hc:engineering

DEFAULT_MODEL=claude-sonnet-4-5
HARNESS=claude
SMART_ROUTING=on
MCP_INTROSPECT=off

# key:NAME:JIRA:~/git/repo  (comma-separated)
PROJECTS=hdig:HDIG:HPTE:~/git/hdig,bid:BID:BID:~/git/bid
```

---

## Usage

```bash
tommy                         # launch with default harness
tommy --project hdig          # activate project context (injects project prompt layer)
tommy --list-projects         # show configured projects
tommy --list-harnesses        # show detected harnesses (✓=on PATH)
tommy --no-memnos-check       # skip memnos health check
tommy --conf /path/to/conf    # explicit config file
```

Tommy exec-replaces itself with the harness — you interact with claude/codex/etc
directly. Tommy writes the assembled system prompt to a temp file and passes it
via `--append-system-prompt-file` (or equivalent per harness).

---

## Adding harnesses

Drop a TOML file into `~/.memnos/harnesses/`:

```toml
[myharness]
binary = "myharness"
launch_template = ["myharness", "--system", "{prompt_file}"]
supports_tools = true
supports_mcp = false
description = "My custom LLM harness"
```

---

## memnos Integration

Tommy is memnos-native. At launch:
1. Health-checks memnos server (`http://127.0.0.1:8900` by default)
2. Writes a session-start memory to `TOMMY_NS`
3. Injects namespace-aware context into the system prompt

Tommy's core prompt (`prompts/core.md`) instructs the harness to:
- Recall project context before every task dispatch
- Use leases to prevent duplicate work across sessions
- Subscribe to namespace changes during long sessions
- Run `segment_episodes` + `consolidate` at session end

---

## Slash Commands (Claude Code)

After `tommy --install`, these commands are available in Claude Code:

| Command | Purpose |
|---------|---------|
| `/memnos` | memnos quick-reference cheat sheet |
| `/memnos-save <text>` | Save a fact to memnos |
| `/memnos-constraint <rule>` | Save a pinned constraint rule |
| `/memnos-recall <topic>` | Search memnos memory |
| `/memnos-pin` | Summarise + save current exchange |

---

## Architecture

```
tommy/
├── cli.py              # Click CLI, exec's harness
├── config.py           # KEY=VALUE config loader
├── prompt.py           # Layer stacker (core → org → project → local → runtime → MCP)
├── install.py          # First-time setup
├── discovery/
│   ├── harnesses.py    # HarnessSpec registry + PATH detection + TOML drop-ins
│   └── mcp.py          # MCP config reader + optional stdio tool introspection
├── prompts/
│   └── core.md         # Tommy's brain (system prompt)
├── slash_commands/
│   └── memnos*.md      # Claude Code custom commands
└── tommy.conf.default  # Bundled default config
```

Tommy has no cloud control plane dependency, no vendor lock-in.
It works with any harness that accepts a system prompt file.
