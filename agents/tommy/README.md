# Tommy — memnos-native coding orchestrator

Tommy is a lightweight CLI orchestrator that sits between your editor and your
coding harness (Claude Code, Codex, etc.).  It enriches every task with
long-term memory from [memnos](../../README.md), routes work to the right
harness, and lets you steer a running sub-agent mid-run without waiting for it
to finish.

```
┌─────────────┐   tommy --mcp   ┌──────────────────────────────────────────┐
│  Editor /   │ ─────────────→  │             Tommy (stdio process)         │
│  IDE        │ ←────────────── │  7 MCP tools  ·  memnos  ·  harness mgr  │
└─────────────┘  JSON-RPC/stdio └──────────────────────┬───────────────────┘
                                                        │ Popen
                                              TOMMY_CTRL_PORT
                                                        │
                                         ┌──────────────▼──────────────┐
                                         │  Harness (Claude Code, etc.) │
                                         │  progress / wrap_up / abort  │
                                         └─────────────────────────────┘
```

**Key design decisions:**
- Tommy is a **stdio subprocess**, not a daemon or HTTP server.  The editor
  spawns it (`tommy --mcp`) and owns its lifecycle.
- **memnos is the only persistent server.**  Tommy talks to memnos for memory;
  it exposes no long-lived listening ports.
- A **TCP loopback control channel** (`TOMMY_CTRL_PORT`) is opened
  *transiently* — one ephemeral `127.0.0.1:0` socket per dispatch, closed
  when the sub-agent exits.  This lets Tommy send `wrap_up` / `abort` /
  `pivot` to a running sub-agent and receive live progress without polling.

---

## Requirements

| Dependency | Version |
|-----------|---------|
| Python    | ≥ 3.11  |
| [uv](https://docs.astral.sh/uv/) | any recent |
| memnos    | installed and running (HTTP or stdio) |
| A supported harness | Claude Code (`claude`), Codex, etc. |

---

## Install

```bash
# From the memnos repo root (editable, uv-managed):
uv tool install -e ~/git/memnos/agents/tommy

# Verify:
tommy --version
```

> **Note:** always use `--force` when pyproject.toml dependencies change:
> ```bash
> uv tool install -e ~/git/memnos/agents/tommy --force
> ```

### First-time setup

```bash
tommy --install
```

This creates `~/.memnos/agents/tommy/tommy.conf` with sensible defaults.

---

## Configuration

Edit `~/.memnos/agents/tommy/tommy.conf`:

```ini
# Who you are
TOMMY_USER=YourName
ORG=your-org

# memnos namespaces
TOMMY_NS=user:yourname:tommy          # where Tommy journals its own sessions
DEFAULT_NS=org:your-org:engineering   # default namespace for new memories

# Model & harness
DEFAULT_MODEL=claude-sonnet-4-5
HARNESS=claude                        # claude | codex | auto
SMART_ROUTING=on

# Projects — format: key:Name:JIRA_PROJECT:absolute/path/to/repo
# (one per line, comma-separated)
PROJECTS=\
  myapp:MyApp:MYAPP:~/git/myapp,\
  platform:Platform:PLAT:~/git/platform,\
  infra:Infra:PLAT:~/git/infra
```

### Project fields

| Field | Meaning |
|-------|---------|
| `key` | Short identifier used in `tommy --project <key>` |
| `Name` | Human-readable label |
| `JIRA_PROJECT` | Jira project key (used in commit messages, ticket links) |
| `path` | Absolute path — the workspace Tommy gives to the harness |

---

## Usage

### CLI

```bash
# Launch harness with memory context
tommy

# Activate a project (workspace + namespace auto-set)
tommy --project myapp

# List configured projects
tommy --list-projects

# List detected harnesses
tommy --list-harnesses

# Upgrade (respects uv/pipx/pip — never mixes installers)
tommy --upgrade
```

### MCP stdio mode (for editors)

```bash
tommy --mcp
```

The editor spawns this process, sends JSON-RPC over stdin/stdout, and kills
the process when done.  Tommy never opens a port in this mode.

---

## Editor integration

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tommy": {
      "command": "tommy",
      "args": ["--mcp"]
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` in your project root (or the global
`~/.cursor/mcp.json`):

```json
{
  "servers": {
    "tommy": {
      "command": "tommy",
      "args": ["--mcp"]
    }
  }
}
```

### VS Code + Continue

In `.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "tommy",
      "command": "tommy",
      "args": ["--mcp"]
    }
  ]
}
```

### Zed

In `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "tommy": {
      "command": {
        "path": "tommy",
        "args": ["--mcp"]
      }
    }
  }
}
```

---

## MCP tools

| Tool | Description |
|------|-------------|
| `tommy_recall` | Query memnos memory for context |
| `tommy_remember` | Persist a fact / decision to memnos |
| `tommy_dispatch` | Launch a harness task (async by default) |
| `tommy_status` | Check a running task's output / exit code |
| `tommy_control` | Send `wrap_up` / `abort` / `pivot` / `answer` to a running task |
| `tommy_switch_project` | Set active project (workspace + namespace) |
| `tommy_route` | Dry-run: which harness would Tommy pick? |
| `tommy_list_harnesses` | Available harnesses + active routing config |

### `tommy_dispatch`

```
tommy_dispatch(
    task="refactor the auth module to use PKCE",
    harness="auto",          # or "claude", "codex", …
    workspace="/path/to/repo",
    async_run=True,          # return task_id immediately
    inject_memory=True,      # prepend memnos recall to prompt
)
→ {"task_id": "a3f1b2c4", "status": "running", "harness": "claude"}
```

### `tommy_control` — steer a running task

```
# Ask the harness to wrap up (gives it 60 s to finish gracefully)
tommy_control(task_id="a3f1b2c4", action="wrap_up", budget_seconds=60)

# Stop immediately
tommy_control(task_id="a3f1b2c4", action="abort")

# Redirect to a different goal mid-run
tommy_control(task_id="a3f1b2c4", action="pivot",
              message="focus only on the login flow, skip registration")

# Answer a question the harness asked
tommy_control(task_id="a3f1b2c4", action="answer", message="yes, overwrite")
```

The harness receives the message over a **TCP loopback control channel**
(`TOMMY_CTRL_PORT` env var) — no polling required.

---

## Control channel (for harness authors)

If you write a custom harness in Python, connect back to Tommy using the
bundled `ControlClient`:

```python
from tommy.control import ControlClient

def handle_tommy_message(msg: dict) -> None:
    if msg["type"] == "wrap_up":
        # save state and exit within msg["budget_seconds"]
        ...
    elif msg["type"] == "abort":
        raise SystemExit(1)
    elif msg["type"] == "pivot":
        current_goal = msg["new_goal"]

client = ControlClient(on_control=handle_tommy_message)

# Report progress
client.progress(25, "parsed 250 / 1000 files")
client.checkpoint("analysis", "found 3 duplicate patterns")

# Ask Tommy / user a question (blocks until answered via tommy_control)
client.question("Should I overwrite existing tests?", options=["yes", "no"])

client.done("refactoring complete — 12 files changed")
client.close()
```

The client auto-reads `TOMMY_CTRL_PORT` from the environment.

**Protocol (newline-delimited JSON):**

| Direction | `type` | Extra fields |
|-----------|--------|-------------|
| Harness → Tommy | `progress` | `pct`, `detail` |
| Harness → Tommy | `checkpoint` | `phase`, `summary` |
| Harness → Tommy | `done` | `summary` |
| Harness → Tommy | `error` | `message` |
| Harness → Tommy | `question` | `text`, `options` |
| Tommy → Harness | `wrap_up` | `reason`, `budget_seconds` |
| Tommy → Harness | `abort` | — |
| Tommy → Harness | `pivot` | `new_goal` |
| Tommy → Harness | `answer` | `text` |

The control channel uses TCP loopback (`127.0.0.1`), which works on macOS,
Linux, and Windows without any extra setup.

---

## Upgrade

```bash
tommy --upgrade
```

Tommy detects whether it was installed with `uv`, `pipx`, or `pip` and uses
the same tool to upgrade — so the venv is never mixed.

To upgrade manually with uv:

```bash
uv tool install -e ~/git/memnos/agents/tommy --force
```

---

## Project structure

```
agents/tommy/
├── README.md               ← you are here
├── pyproject.toml
└── tommy/
    ├── __init__.py
    ├── cli.py              ← click entrypoint, _launch_harness
    ├── config.py           ← TommyConfig, ProjectEntry
    ├── control.py          ← ControlServer + ControlClient (TCP IPC)
    ├── install.py          ← tommy --install
    ├── mcp_server.py       ← FastMCP stdio server, 8 tools
    ├── prompt.py           ← memnos-enriched system prompt builder
    └── discovery/
        └── harnesses.py    ← auto-detect installed harnesses
```

---

## Roadmap

- [ ] Supervision loop: idle + wall-clock timeout with automatic `wrap_up`
- [ ] Smart harness routing by task type (coding vs. research vs. review)
- [ ] memnos lease heartbeat while harness is running
- [ ] Multi-harness fan-out (run two harnesses in parallel, merge outputs)
