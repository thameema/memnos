# Tommy — memnos-native coding orchestrator

Tommy is a lightweight CLI orchestrator that sits between your editor and your
coding harness (Claude Code, Codex, etc.).  It enriches every task with
long-term memory from [memnos](../../README.md), routes work to the right
harness, and lets you steer a running sub-agent mid-run without waiting for it
to finish.

> This file is the mechanical reference (CLI flags, config keys, MCP tool
> signatures, the control-channel wire protocol). For what Tommy is, why it
> exists, how the pieces fit together, and what's genuinely still prompted
> behavior vs. code-enforced, see
> [`docs/guides/tommy.md`](../../docs/guides/tommy.md).

```
┌─────────────┐   tommy --mcp   ┌──────────────────────────────────────────┐
│  Editor /   │ ─────────────→  │             Tommy (stdio process)         │
│  IDE        │ ←────────────── │  9 MCP tools  ·  memnos  ·  harness mgr  │
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
| Python    | ≥ 3.10  |
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

# Secret Shield — ENV_VAR:secret://NAME pairs (comma-separated). Resolved via
# memnos at launch time, injected into the launched harness's subprocess env
# ONLY (never the prompt, never a log). See "Secret Shield" below.
SECRET_ENV=OPENAI_API_KEY:secret://openai_api_key
```

### Project fields

| Field | Meaning |
|-------|---------|
| `key` | Short identifier used in `tommy --project <key>` |
| `Name` | Human-readable label |
| `JIRA_PROJECT` | Jira project key (used in commit messages, ticket links) |
| `path` | Absolute path — the workspace Tommy gives to the harness |

---

## tommy.yaml — committed project config

`tommy.conf` above is a per-user INI file installed at
`~/.memnos/agents/tommy/tommy.conf` — it's never checked into a repo, and it's
where personal identity (`TOMMY_USER`, `ORG`) and your own multi-repo
`PROJECTS` list live. `tommy.yaml` is the other half: a single project's
config, committed alongside the code it governs, so a team shares one source
of truth instead of everyone hand-syncing their own `tommy.conf`.

**Safe to commit — policy only, never secrets.** Nothing in this schema
accepts a token, key, password, or URL with embedded credentials. If a field
ever needs one, it belongs in `tommy.conf` or an environment variable
instead, never in `tommy.yaml`.

```yaml
tommy:
  version: 1
project:
  name: MyApp          # free-form/informational only
  key: myapp             # free-form/informational only
  git_root: .             # optional, defaults to the repo root tommy.yaml lives in
memnos:
  namespace: "org:myorg:myapp"
design_docs:
  - "docs/adr/*.md"       # hand-authored ADRs/design docs, NOT vendor guides
corpus:
  corpus_gate: true        # gate dispatch on corpus_check() before proceeding
  auto_ingest: false        # auto-ingest design_docs matches into the corpus
agents:
  default_model: claude-sonnet-4-5
  harness: claude
  smart_routing: true
  mcp_introspect: false
  skip_permissions: true
env:
  OPENAI_API_KEY: secret://openai_api_key   # secret:// references ONLY — see "Secret Shield" below
merge_gate: true           # formalizes core.md's wave-based dispatch concept
wave_limit: 4
```

**Deliberately absent from this schema** (see issue #113 — exclusions, not
omissions): a `platform:` field or any GitLab/GitHub/Azure-specific
integration logic (Tommy stays platform-agnostic); scheduler ownership (set up
your own cron/launchd if you want scheduled runs); a `peer_approver:` field
(considered and cut — undesigned semantics, needs its own issue); and a
top-level `harness:` field (which harness a person runs locally is
machine-specific, not a team-wide committed decision — use `agents.harness`,
a default *suggestion* with the same precedence rules as every other field,
instead).

### Config precedence

Three layers, lowest to highest:

1. **`tommy.conf`** — installed/global INI defaults (bundled default → your
   `~/.memnos/agents/tommy/tommy.conf` → an explicit `--conf`).
2. **`tommy.yaml`** — project config, committed. Only fields the file
   actually sets participate; anything left out falls through to the
   `tommy.conf` value untouched.
3. **Environment variables** — highest precedence, for one-off local
   overrides without editing either file: `TOMMY_CFG_DEFAULT_MODEL`,
   `TOMMY_CFG_HARNESS`, `TOMMY_CFG_SMART_ROUTING`, `TOMMY_CFG_MCP_INTROSPECT`,
   `TOMMY_CFG_SKIP_PERMISSIONS`, `TOMMY_CFG_NAMESPACE`,
   `TOMMY_CFG_PROJECT_NAME`, `TOMMY_CFG_PROJECT_KEY`,
   `TOMMY_CFG_PROJECT_GIT_ROOT`, `TOMMY_CFG_DESIGN_DOCS` (comma-separated),
   `TOMMY_CFG_CORPUS_GATE`, `TOMMY_CFG_AUTO_INGEST`, `TOMMY_CFG_MERGE_GATE`,
   `TOMMY_CFG_WAVE_LIMIT`.

Run `tommy config show` to print the fully-resolved effective config —
every field's final value plus which of the three layers it came from
(`tommy.conf` / `tommy.yaml` / `env` / `default`). Add `--format json` for
scripting.

### `tommy generate` — project harness adapters

Reads `tommy.yaml` and writes/updates whichever coding-harness config
file(s) this project already shows evidence of using:

| Harness | Target file | "Present" means |
|---------|------------|------------------|
| Claude Code | `CLAUDE.md` | the file exists |
| Cursor | `.cursor/rules/tommy.mdc` | `.cursor/` exists |
| Windsurf | `.windsurfrules` | the file exists |
| Copilot | `.github/copilot-instructions.md` | `.github/` exists |

If none of the above are present, the generated block is printed to stdout
instead of silently creating four files for harnesses nobody on the project
actually uses. Pass `--create-missing` to force every target to be written
regardless.

Every write goes through explicit idempotent markers
(`<!-- TOMMY:BEGIN -->` / `<!-- TOMMY:END -->`) — re-running `tommy generate`
only ever replaces the marked region, never anything else in the file:

```bash
tommy generate                 # update whatever adapter files are present
tommy generate --dry-run       # preview without writing
tommy generate --create-missing  # also create files with no prior evidence
```

---

## Secret Shield — `secret://NAME` references

`SECRET_ENV` (`tommy.conf`) and `env:` (`tommy.yaml`) let you reference a
secret stored in memnos's Vault by name, instead of ever writing its real
value into a config file:

```ini
# tommy.conf
SECRET_ENV=OPENAI_API_KEY:secret://openai_api_key,DB_PASSWORD:secret://prod_db_password
```

```yaml
# tommy.yaml
env:
  OPENAI_API_KEY: secret://openai_api_key
```

At launch time — for both the interactive `tommy` CLI and `tommy_dispatch` —
Tommy resolves every configured reference via memnos (`memnos secret set
<name> <value>` + `memnos grant add <principal> secret:<name>` on the server
side) and injects the real values into the launched harness subprocess's
environment, under the env-var names you configured. tommy.yaml wins over
tommy.conf on a shared env-var name — same precedence direction as every
other field.

**Fails closed.** If any reference can't be resolved — memnos unreachable,
no such secret, the token isn't granted access — Tommy refuses to launch the
harness at all, before the prompt file or the control-channel socket are
ever created. A project with no `SECRET_ENV`/`env:` entries configured pays
no cost and sees no behavior change.

**Precondition.** `secret://` references are only ever read from static
`tommy.conf`/`tommy.yaml` — never derived from a dispatched task or built
prompt.

**Scope, precisely.** This keeps a resolved secret out of the prompt Tommy
builds and out of Tommy's own logs. It does **not** stop a harness process
from reflecting its own environment back into its own output — and that
carve-out applies through more than one channel:

- If the harness runs something like `printenv` and that ends up in its
  Claude Code transcript, `_post_run_capture` ingests that transcript into
  memnos after every interactive run; `core/redact.py` (not Tommy) is what
  stands between that and durable storage, and it has known gaps on short,
  unusually-shaped secrets under a prefixed variable name (e.g.
  `DB_PASSWORD=hunter2xyz`).
- On the `tommy_dispatch` (MCP) path, the harness's raw stdout is also what
  `tommy_dispatch(async_run=False)` returns as `output` and what
  `tommy_status` returns as its tail — both go straight back to the calling
  LLM as MCP tool output. If the harness reflects a secret into its own
  stdout, that's the same leak surface as the transcript-ingest path above,
  through a different channel, and Secret Shield does not filter it either.

Keep secrets short-lived / scoped and don't rely on Secret Shield as the
only layer of defense against either path.

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

# Print the fully-resolved effective config (tommy.conf -> tommy.yaml -> env)
tommy config show

# Write/update harness adapter files (CLAUDE.md, .cursor/rules/tommy.mdc, ...) from tommy.yaml
tommy generate
```

### MCP stdio mode (for editors)

```bash
tommy --mcp
```

The editor spawns this process, sends JSON-RPC over stdin/stdout, and kills
the process when done.  In MCP mode Tommy itself has no persistent listening
port — but each `tommy_dispatch` call opens a transient `127.0.0.1:0` TCP
control channel that is closed when the sub-agent exits.

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
| `tommy_sketch` | Mermaid sequence diagram -> Canonical Flow Corpus (CFC) constraints -> `/corpus/ingest` |
| `tommy_drift_sweep` | Check recent commits against the architecture corpus |

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

### `tommy_sketch` — mermaid sequence diagram -> CFC constraints

```
tommy_sketch(
    flow_name="checkout-flow",
    mermaid_text="""
sequenceDiagram
    participant C as Client
    participant S as Server
    C->>S: Submit order
    alt payment succeeds
        S-->>C: 200 OK
    else payment fails
        S-->>C: 402 Payment Required
    end
    Note over S: Server must not log raw card data
""",
)
→ {"ok": True, "constraints": 4, "ids": [...], "warnings": [], "cfc_text": "..."}
```

Mermaid TEXT in — never an image (see [Known limitations](../../docs/guides/tommy.md#known-limitations)
in the guide for why: no harness in the registry carries an image-input path). Accepts
`mermaid_file` instead of `mermaid_text` to read from a file. The naive line-based
`_mermaid_to_cfc()` parser (`tommy/sketch.py`) supports flat, single-level `alt`/`else`;
nested `alt`/`opt`/`loop`, multi-line/wrapped labels, and unrecognized syntax are skipped
and reported in the returned `warnings` list rather than silently mis-parsed. Ingests via
`POST /corpus/ingest` with `kind="cfc"` — a `WRITE_OPS` endpoint (a read-only memnos token
403s, surfaced as `{"ok": False, "error": "...(403)..."}`), and re-using the same
`flow_name` DELETE-then-replaces that source's prior constraints.

The harness receives the message over a **TCP loopback control channel**
(`TOMMY_CTRL_PORT` env var) — no polling required.

### `tommy_drift_sweep` — check recent commits against the architecture corpus

Catches drift `tommy_dispatch`'s per-dispatch corpus gate (issue #109)
can't see — commits made directly outside Tommy, or dispatched with the
corpus gate off. Diffs the last `commits` commits and checks the result
against the architecture corpus, also reachable as the `/drift` slash
command.

```
tommy_drift_sweep(commits=20)
→ {
    "ok": true,
    "mode": "recall_fallback",
    "commits_requested": 20, "commits_used": 20, "commits_available": 143,
    "clamped": false,
    "possibly_relevant_constraints": [...],
    "check_failures": [],
  }
```

`commits` is clamped to the repo's actual history (shallow clones and young
repos included) — `commits_used`/`clamped` always report the effective
value used, never silently. `mode` is `"recall_fallback"` today: results
are keyword-matched via corpus FTS recall over the diff, not a
violated/satisfied/uncovered verdict — treat `possibly_relevant_constraints`
as leads, not confirmed violations.

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
    elif msg["type"] == "answer":
        # Reply to a question you sent via client.question()
        user_answer = msg["text"]

client = ControlClient(on_control=handle_tommy_message)

# Report progress
client.progress(25, "parsed 250 / 1000 files")
client.checkpoint("analysis", "found 3 duplicate patterns")

# Send a question to Tommy/user; answer arrives via the on_control callback as {"type": "answer", "text": ...}
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
    ├── config.py           ← TommyConfig, ProjectEntry (tommy.conf)
    ├── project_config.py   ← tommy.yaml schema + parsing + discovery
    ├── effective_config.py ← tommy.conf -> tommy.yaml -> env precedence resolution
    ├── adapters.py         ← tommy generate: idempotent harness adapter writers
    ├── generate_cmd.py     ← `tommy generate` / `tommy config show` CLI commands
    ├── control.py          ← ControlServer + ControlClient (TCP IPC)
    ├── install.py          ← tommy --install
    ├── mcp_server.py       ← FastMCP stdio server, 10 tools
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
