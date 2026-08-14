# Tommy — memnos-native coding orchestrator

Tommy is a personal coding orchestrator that sits above LLM harnesses (Claude Code, Codex, Cursor, Hermes, and others) and uses memnos as its memory and governance backbone.

It works in any terminal, on any machine, with any installed harness. No cloud control plane. No vendor lock-in.

---

## Install

```bash
cd agents/tommy && pip install -e .

# First-time setup: writes ~/.memnos/agents/tommy/tommy.conf
# and installs /memnos-* slash commands into your harness
tommy --install
```

Or, when published to PyPI:
```bash
pip install memnos-tommy && tommy --install
```

---

## Quick start

```bash
# List detected harnesses on this machine
tommy --list-harnesses

# Launch with no project context (general tasks)
tommy

# Launch with a project loaded
tommy --project myapp

# List configured projects
tommy --list-projects
```

---

## Configuration

All config lives in one file: `~/.memnos/agents/tommy/tommy.conf`

```ini
# Who you are
TOMMY_USER=developer

# Org layer (loads prompts/orgs/<ORG>.md if present)
ORG=org

# Where to find your prompt layers (core.md + org/project overrides)
PROMPTS_DIR=~/.memnos/agents/tommy/prompts

# memnos namespaces
TOMMY_NS=user:me:tommy        # Tommy's personal orchestration journal
DEFAULT_NS=org:engineering    # shared team/project constraints pool

# LLM harness to use (must be installed on PATH)
HARNESS=claude
DEFAULT_MODEL=claude-sonnet-4-5

# Smart routing: on = corpus_check before every dispatch
#               off = use static model table in core.md
SMART_ROUTING=on

# MCP tool introspection (reads harness MCP configs at startup)
MCP_INTROSPECT=off

# Projects: key:DISPLAY_NAME:JIRA_KEY:~/git/repo  (comma-separated)
PROJECTS=myapp:MyApp:APP:~/git/myapp,infra:Infra:INFRA:~/git/infra
```

---

## Project context

Tommy loads project-specific context automatically when you pass `--project`:

```
tommy --project myapp
```

This stacks:
1. `prompts/core.md` — Tommy's orchestrator brain (bundled)
2. `prompts/orgs/<ORG>.md` — org-level conventions (you create this)
3. `prompts/projects/<key>.md` — project-specific context (you create this)
4. `$PWD/.tommy.md` — repo-local override (dropped in any git root)
5. Runtime block — harness list, memnos namespaces, project variables

---

## Prompt customisation

Drop a `.tommy.md` file in any repo root:

```bash
echo "This repo is the payments API. Dispatch all work to the python-developer role agent." \
  > ~/git/myapp/.tommy.md
tommy   # launched from ~/git/myapp — picks it up automatically
```

For org-level conventions, create `~/.memnos/agents/tommy/prompts/orgs/myorg.md` and set `ORG=myorg` in your conf.

---

## Harness discovery

Tommy detects harnesses on PATH at startup. Supported out of the box:

| Harness | Binary | Best for |
|---|---|---|
| claude | `claude` | Default — full tool + MCP, large context |
| codex | `codex` | Diff-as-deliverable, single-file focus |
| cursor-agent | `cursor-agent` | IDE-integrated |
| hermes | `hermes` | Local, zero data egress — PHI/sensitive work |
| aider | `aider` | Autonomous coding, longer unattended runs |
| goose | `goose` | Autonomous coding agent |
| kiro | `kiro` | Amazon IDE agent |

To add a custom harness, drop a TOML file in `~/.memnos/harnesses/`:

```toml
[myharness]
binary = "myharness"
description = "My custom harness"
launch_template = ["myharness", "--system-prompt", "{prompt_file}"]
```

---

## Smart LLM routing

When `SMART_ROUTING=on`, Tommy calls `memnos corpus_check` before every dispatch to route tasks by capability. A `model-registry.md` file (ingested as a corpus document) defines the routing rules in plain language:

```
SHALL use hermes for any task where privacy=sensitive
SHALL use claude-opus for quality_tier=high_stakes
SHALL use claude-haiku for quality_tier=draft AND latency=realtime
```

Update the registry by re-ingesting after vendors release new models — no code change needed.

---

## memnos integration

Tommy uses memnos across its full lifecycle:

| Feature | When Tommy uses it |
|---|---|
| `recall` | Load project constraints and prior decisions before dispatch |
| `remember` | Journal session start/end and key outcomes |
| `namespace_subscribe` / `namespace_feed` | Capture everything the sub-agent writes in real-time |
| `lease_acquire` / `lease_heartbeat` / `lease_release` | Prevent two Tommy sessions from working the same ticket simultaneously |
| `corpus_check` | Enforce architecture constraints; drive smart routing |
| `corpus_ingest` | Feed vendor model docs into routing registry |
| `ingest_file` | Store the sub-agent conversation transcript after each session |
| `segment_episodes` | Make each orchestration run a searchable episode |
| `consolidate` | Distil orchestration patterns into durable facts over time |

---

## Slash commands (installed into your harness)

`tommy --install` writes these into your harness's custom command directory:

| Command | What it does |
|---|---|
| `/memnos` | Cheat sheet of all memnos tools |
| `/memnos-constraint <rule>` | Save a governing constraint (pinned to every future session) |
| `/memnos-save <text>` | Save a plain fact |
| `/memnos-recall <topic>` | Explicit recall + recall_wide fallback |
| `/memnos-pin` | Save the current exchange as a memory |

---

## CLI reference

```
tommy [OPTIONS] [HARNESS_ARGS]...

Options:
  --project, -p KEY    Activate a project context
  --conf PATH          Path to tommy.conf override
  --install            First-time setup (conf + slash commands)
  --force              With --install: overwrite existing files
  --list-projects      List configured projects
  --list-harnesses     List detected harnesses on PATH
  --no-memnos-check    Skip memnos health check (offline mode)
```

Any extra arguments after the Tommy options are passed through to the harness unchanged.

---

## How sub-agents share memnos

Tommy uses `subprocess.Popen` (not `exec`) so it stays alive as a supervisor. Before launching the harness, Tommy:

1. Calls `ensure_memnos_running()` — health-checks the HTTP daemon, starts it if needed
2. Injects `MEMNOS_URL` into the sub-agent's process environment
3. Calls `namespace_subscribe()` to snapshot the cursor

After the harness exits, Tommy:

4. Calls `namespace_feed()` — drains everything the sub-agent wrote
5. Ingests the harness's conversation transcript via `ingest_file()`
6. Calls `segment_episodes()` — makes the run a searchable memory episode

Both Tommy and the harness connect to the **same HTTP memnos daemon** — no state split, no two-writer conflict.

---

## Repository layout

```
agents/tommy/
├── pyproject.toml
└── tommy/
    ├── cli.py                  # Popen supervisor + memnos lifecycle
    ├── config.py               # KEY=VALUE conf loader
    ├── prompt.py               # Prompt layer stacker
    ├── install.py              # tommy --install
    ├── discovery/
    │   ├── harnesses.py        # Registry + PATH detection
    │   └── mcp.py              # MCP config reader
    ├── prompts/
    │   └── core.md             # Tommy's orchestrator brain
    ├── slash_commands/         # /memnos-* custom commands
    └── tommy.conf.default      # Bundled defaults
```
