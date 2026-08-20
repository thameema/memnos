# Tommy — memnos-native coding orchestrator

Tommy is a personal coding orchestrator, published independently to PyPI as
`tommy-orchestrator` and developed in this repo at [`agents/tommy/`](../../agents/tommy/).
It sits between you and a coding-agent CLI (Claude Code, Codex, aider, goose, cursor-agent,
kiro, or a fully local harness), gives that session persistent memnos memory it wouldn't
otherwise have, and — on its interactive launch path — hands it a system prompt that keeps
it acting as a thin coordinator instead of doing the work itself.

This guide covers what Tommy is, why it exists, and how it fits together. For exact CLI
flags, the full MCP tool signatures, the control-channel wire protocol, and per-editor MCP
JSON, see the package's own [`agents/tommy/README.md`](../../agents/tommy/README.md) —
this guide won't duplicate that reference; it explains the parts the reference doesn't.

`agents/tommy` is versioned and released independently of `memnos`/`memnos-sdk` (see
[`../../RELEASING.md`](../../RELEASING.md)) — its version number has no relationship to the
version of memnos or memnos-sdk you have installed, beyond the minimum `memnos-sdk` pin in
its `pyproject.toml`.

---

## What Tommy is

Tommy is **not** a domain-capability agent. It doesn't read code, write diffs, or review a
merge request itself. It launches a coding-agent CLI as a subprocess, gives that CLI's
session your relevant memnos memory up front, and — when launched interactively — injects a
system prompt whose central rule is that a coordinator which does the work itself has
become the thing it's supposed to dispatch to. The actual implementation, review, or
investigation happens inside the harness session Tommy launches (e.g. a Claude Code session
using its own subagent/Task mechanism), not inside Tommy.

Concretely, Tommy ships two entry points from one `tommy` binary:

- **`tommy`** — an interactive CLI. It resolves your config, does a memnos health check,
  builds a layered system prompt, and `Popen`s your configured harness in the foreground
  with that prompt attached, staying alive to capture output after the harness exits.
- **`tommy --mcp`** — an MCP stdio server. An editor (Claude Desktop, Cursor, Continue,
  Zed) spawns this process and drives it over JSON-RPC/stdio instead of a human running the
  CLI directly. It exposes 8 tools (`tommy_recall`, `tommy_remember`, `tommy_dispatch`,
  `tommy_status`, `tommy_control`, `tommy_switch_project`, `tommy_route`,
  `tommy_list_harnesses`) — see the README for their signatures.

These two paths share the harness registry and memnos config, and now also share the same
core.md coordinator prompt-injection — both call `tommy/prompt.py`'s `build_prompt()`. See
[How it works](#how-it-works) below for the one remaining difference: a non-interactive
framing note on the `tommy_dispatch` path, since there's no human turn to greet or ask a
clarifying question.

---

## Why it exists

Every fresh coding-agent session — a new Claude Code window, a new Codex run, a new aider
invocation — starts with no memory of yesterday's work. The harness itself is stateless
between invocations; whatever it learned about your codebase, your conventions, or a
decision you made last week is gone unless you re-explain it. Tommy's reason for existing
is to sit in front of that gap:

- **It recalls before it dispatches.** Tommy queries memnos for context relevant to the task
  and front-loads it into the harness's prompt, because the harness session itself won't
  call memnos on its own initiative unless it's separately wired to.
- **It journals after.** On the interactive CLI path, Tommy ingests the harness's own
  conversation transcript into memnos and consolidates it, so decisions made *during* the
  session become recallable in the *next* one.
- **It's meant to prevent duplicate work across parallel sessions.** If you (or a
  teammate) run more than one Tommy session against the same ticket, MR, or issue at once,
  nothing today stops two sessions from independently picking up the same item — the
  intended fix is a memnos lease (acquire/heartbeat/release) keyed on the work item before
  dispatch. This is real memnos functionality (see below for exactly how far it's wired in).
- **It's meant to route to the right model/harness tier for the task**, rather than always
  reaching for the same model regardless of whether the task is a quick lookup or a
  high-stakes architectural change.
- **It bounds fan-out and keeps it auditable.** Rather than dispatching everything at once,
  the injected prompt asks for wave-based dispatch (a small number of subagent tasks per
  turn), typed `PURPOSE` headers, and `{project}-{task-slug}-{agent}` task names, so a human
  reviewing a session can tell what was dispatched, to what, and why.

Some of the above is enforced by Tommy's own Python code; some of it is an instruction
Tommy hands to the LLM session it launches, which that session follows (or doesn't) using
tools that may or may not actually be present. The next section is explicit about which is
which — that distinction is the most important thing to understand about how Tommy actually
behaves today.

---

## How it works

### Two different things both called "the orchestrator"

Tommy's Python process (`tommy/cli.py`, `tommy/mcp_server.py`) is a **launcher and memory
bridge**: it detects harnesses on `PATH`, builds a prompt, opens a control-channel socket,
`Popen`s the harness, and — only on the interactive `tommy` CLI path — ingests the resulting
transcript back into memnos afterward.

The actual "thin coordinator" behavior described above — the lease/corpus/wave-dispatch
rules — is **not implemented in Tommy's Python code**. It lives in
[`tommy/prompts/core.md`](../../agents/tommy/tommy/prompts/core.md), a system prompt that
`tommy/prompt.py`'s `build_prompt()` layers together (core.md → optional org override →
optional project override → optional workspace-local `.tommy.md` → a runtime-config block →
an MCP-server manifest) and attaches to the harness via
`--append-system-prompt-file` (for harnesses whose registry entry supports it). Once the
harness is running with that prompt, the actual orchestration — dispatching subagents,
acquiring leases, checking corpus rules, respecting wave limits — is **the harness's own LLM
session choosing to follow those instructions**, using tools that must independently be
available to it.

`build_prompt()` is invoked on both paths. The interactive `tommy` CLI calls it as
`build_prompt(cfg, project_key=...)`; the `tommy_dispatch` MCP tool calls the same function as
`build_prompt(cfg, project_key=_active_project, task=task_with_memory)` —
`task_with_memory` being the dispatched task, optionally prefixed with a single memnos
`recall()` result. A `task` parameter on `build_prompt()` appends the dispatched task as a
final layer, so a task dispatched through `tommy_dispatch` gets the same
lease/corpus/wave-dispatch coordinator identity core.md gives the interactive path, not just
memory-priming. The one difference: since there's no human at the other end of a headless
dispatch, that final layer opens with a short framing note overriding core.md's two
interactive-only instructions (the session-start greeting and asking the user a clarifying
question) — every other coordinator rule is unchanged.

### memnos-native memory (what Tommy's own code actually does)

Tommy's Python process talks to memnos through `memnos_sdk.MemnosClient`, whose surface is
narrower than memnos's full MCP tool set:  `remember`, `recall`, `ingest_file`, `context`,
`consolidate`, `feedback`, `healthy`. Concretely, on an interactive `tommy` launch, Tommy's
own code:

1. Health-checks memnos (`GET {MEMNOS_URL}/healthz`) and, if it's not reachable, makes one
   best-effort attempt to start it (`memnos start --http --port <port>`) before giving up
   and continuing with memory features disabled.
2. Writes a "Tommy session started" fact to `TOMMY_NS` via `remember()`.
3. After the harness process exits: finds the most recently modified Claude Code transcript
   under `~/.claude/projects/*/*.jsonl`, ingests it via `ingest_file(..., extract=True)`,
   calls `consolidate()`, and writes a "Tommy session ended" fact to `TOMMY_NS`. This
   transcript-ingest step is Claude-Code-specific — it doesn't generalize to other
   harnesses' own transcript formats.

Everything else attributed to "memnos-native memory" in the design — per-dispatch `recall`
into subagent prompts, `remember` of orchestration decisions with `memory_type="decision"`,
lease acquire/heartbeat/release, `corpus_check`/`corpus_ingest`, `namespace_subscribe`/
`namespace_feed`, `segment_episodes` — are **core.md instructions telling the harness's own
LLM session to call memnos's MCP tools directly**. memnos exposes those as MCP tools (see
[`../integrations/mcp.md`](../integrations/mcp.md)), but the memnos-sdk Python client Tommy
itself uses does not have `lease_acquire`, `corpus_check`, or pub/sub methods at all — so
Tommy's own process could not call them even if it wanted to. Whether the harness session
actually can depends entirely on whether *that harness* has memnos wired in as its own MCP
server — see [Integrating with existing CLIs](#integrating-with-existing-clis).

### Leases

`core.md` instructs the harness session to call `lease_acquire(key, holder_id, ttl_seconds)`
before dispatching long-running work on a named item, heartbeat it periodically, and release
it on completion or failure — so that two Tommy sessions working the same ticket/MR don't
duplicate work. This is real memnos functionality. It is **not enforced anywhere in Tommy's
own code** — it only happens if (a) you're on the interactive `tommy` path where core.md is
loaded, and (b) the harness has memnos's MCP tools available to actually call
`lease_acquire`. Skip either condition and nothing stops two concurrent sessions from
picking up the same item.

### Model / harness routing

`SMART_ROUTING` is a config flag Tommy's own code reads and injects into the runtime-config
block of the prompt (`SMART_ROUTING: on|off`), but Tommy's Python code does not act on it —
routing is, again, an instruction to the harness session:

- **`SMART_ROUTING=off`** — core.md gives the harness a static task-type → model/harness
  table to follow.
- **`SMART_ROUTING=on`** — core.md tells the harness to call memnos's `corpus_check` MCP
  tool before each dispatch against a corpus of ingested routing rules (context load, task
  type, quality tier, latency, privacy), with hard overrides such as "sensitive privacy
  forces a fully local harness" taking precedence over whatever the corpus returns.

The MCP tool `tommy_route` (dry-run: "which harness would Tommy pick?") does not implement
this either — its own docstring says task-type routing "will be added in a future release";
today it just echoes back the statically configured `HARNESS` value.

### Multi-harness subprocess supervision

`tommy/discovery/harnesses.py` holds a built-in registry (`claude`, `codex`, `cursor-agent`,
`hermes`, `aider`, `goose`, `kiro`), each with a `binary` to look up on `PATH`
(`shutil.which`) and a `launch_template` command line. `all_harnesses()` merges that
built-in registry with any TOML drop-ins found under `~/.memnos/harnesses/*.toml`, so you
can add a harness the built-in registry doesn't know, or override one whose CLI flags have
since changed. Detection happens fresh at every launch — there's no caching.

Only the exact command lines in the registry are what Tommy actually invokes; this guide
doesn't independently vouch for every flag against every vendor CLI's current version (CLI
flags change across releases). If a harness in the built-in registry doesn't launch cleanly
against the version you have installed, add a `~/.memnos/harnesses/*.toml` override rather
than assuming the built-in template is current — see the README for the TOML schema.

### MCP server mode

`tommy --mcp` runs `FastMCP` over stdio — no listening port, no daemon; the editor owns the
process's lifecycle. It's a separate direction from harness supervision: here Tommy is the
thing being driven over MCP, not the thing doing the driving. See
[Integrating with existing CLIs](#integrating-with-existing-clis) for how the two directions
combine (or don't) with each other.

### The control channel

`tommy/control.py` implements a real, tested, bidirectional TCP-loopback control channel:
Tommy opens `ControlServer` on an ephemeral `127.0.0.1:0` port before `Popen`-ing the
harness, passes the port via the `TOMMY_CTRL_PORT` env var, and can then send `wrap_up` /
`abort` / `pivot` / `answer` messages to whatever connects back, while receiving `progress`
/ `checkpoint` / `done` / `error` / `question` messages from it. `tommy/control.py` also
ships a `ControlClient` class for a harness to import and connect back with.

**None of the harnesses in the built-in registry actually do this.** `claude`, `codex`,
`aider`, `goose`, and the rest don't read `TOMMY_CTRL_PORT` or dial back to it — they have
no knowledge the control channel exists. Calling `tommy_control(task_id, "wrap_up")` against
a task dispatched to a stock harness will time out waiting for a connection and come back
with `harness_connected: false`. The control channel is real, working plumbing — but today
it's infrastructure for a harness *you* instrument (via `ControlClient`, as documented in
the README), not a capability that comes for free with any of the built-in ones.

### Wave-based dispatch, `PURPOSE` headers, named tasks

These are all core.md instructions to the harness session, governing how *it* uses *its
own* subagent-dispatch mechanism (e.g. Claude Code's own Task tool) — not something Tommy's
Python process schedules or enforces. Tommy has no dispatch queue of its own for this;
"wave-based" here means the harness session is told to keep fan-out to a small number of
tasks per turn and wait for them before continuing.

The "small number" was, until issue #113, only ever a number in core.md's prose ("max
4/turn"). `tommy.yaml`'s `merge_gate` / `wave_limit` fields (see the project's own
[README](../../agents/tommy/README.md#tommyyaml--committed-project-config)) formalize that
into actual config — `wave_limit` defaults to 4 precisely so formalizing it doesn't silently
change today's behavior for a project that hasn't adopted `tommy.yaml` yet. `tommy generate`
projects the resolved values into whichever harness adapter file(s) (CLAUDE.md, Cursor
rules, etc.) a project already uses, so the harness session sees the actual configured
number rather than the static prompt text. Nothing consumes `merge_gate`/`wave_limit` to
*enforce* a cap in code yet — like everything else in this section, it's still an
instruction the harness session chooses to follow, now sourced from committed config instead
of a hardcoded prompt literal.

### Reviewer dispatch — mandatory review passes

Also a core.md instruction, not code: for any Task dispatched with `PURPOSE: review`
(code, LLD, architecture, or any future review agent type), core.md tells the harness
session to append a fixed "MANDATORY REVIEW PASSES" block to the subagent's Task prompt
before dispatching it. The block asks the reviewer for three things standard review misses:

- **Pass 4 — System Invariant Check**: a CLEAR/BLOCKER verdict against four fixed
  invariants (no live-tenant mutation from reaper/scheduler paths, credential rotation
  paired with pool invalidation, fail-closed on missing tenant context, error handling for
  the gap in paired writes to two stores).
- **Pass 5 — Call Graph Mandate**: enumerate *all* callers of any new/renamed function —
  schedulers, reapers, lifecycle hooks, Helm hooks, internal REST — not just direct callers,
  and state reachability explicitly. Callers Tommy can't trace (e.g. in another repo) are
  assumed live-tenant reachable.
- **Pass 6 — Safety Claim Verification**: trace safety claims ("idempotent", "fails closed",
  "no side effects", "safe to retry", "does not rotate") to source instead of accepting them
  from a comment or MR description. An unverifiable claim is a MAJOR finding that blocks
  approval until the author adds a test.

Because both entry points build the harness's system prompt from the same core.md via
`build_prompt()` (see `tests/test_dispatch_core_prompt_parity.py`), this block reaches a
review dispatched from the interactive `tommy` CLI and one dispatched via `tommy_dispatch`
identically — there is no separate reviewer-prompt code path to keep in sync.

### Interrupt / control model

Because a subagent dispatch from the harness's own Task-tool perspective is a blocking call,
core.md is explicit that control commands (`/stop`, `/cancel`, `/pivot`, `/status`,
`/results`) typed by the user can only take effect at the start of the harness's *next*
turn — not mid-dispatch. Wave-based dispatch exists partly to create natural points for a
human to redirect between waves rather than only at the very end. This is a real,
architectural constraint of how Task-tool-style dispatch works, not a bug to be fixed
later.

---

## Installation

```bash
# uv (recommended — matches the rest of this repo's tooling)
uv tool install tommy-orchestrator

# Fallback if you don't have uv:
pip install tommy-orchestrator

# From a checkout of this repo, editable (uv):
uv tool install -e agents/tommy
```

Requires Python ≥ 3.10 and pulls in `click`, `httpx`, `fastmcp`, and `memnos-sdk>=0.1.21`
(the pin in `agents/tommy/pyproject.toml` — bump your `memnos-sdk` install if you're below
that). It does not require the full `memnos` server package to be installed in the same
environment; it talks to memnos over HTTP.

First-time setup:

```bash
tommy --install
```

This writes `~/.memnos/agents/tommy/tommy.conf` (from the bundled default, if one doesn't
already exist), copies the bundled `/memnos*` slash-command files to
`~/.claude/commands/`, and copies the bundled prompt layers (`core.md`, etc.) to
`~/.memnos/agents/tommy/prompts/` so you can edit your own copy without touching the
installed package. `--force` overwrites existing files.

---

## Configuration

Tommy reads `~/.memnos/agents/tommy/tommy.conf` (`KEY=VALUE`, one per line), merged in this
order — later sources win: bundled default → `~/.memnos/agents/tommy/tommy.conf` →
`--conf <path>` → two environment variables.

| Key | Meaning | Default |
|---|---|---|
| `TOMMY_USER` | Your name, used in the launch banner and journaled facts | `developer` |
| `ORG` | Selects an optional `{ORG}.md` prompt override layer | `myorg` |
| `PROMPTS_DIR` | Where `core.md`/org/project prompt layers are read from | `~/.memnos/agents/tommy/prompts` |
| `TOMMY_NS` | Namespace Tommy journals its own session/decision facts to | `user:tommy` |
| `DEFAULT_NS` | Default namespace for shared project/org memory | `org:engineering` |
| `DEFAULT_MODEL` | Advisory model name injected into the prompt (not enforced by Tommy's code) | `claude-sonnet-4-5` |
| `HARNESS` | Which registry entry to launch (`claude`, `codex`, …) | `claude` |
| `SMART_ROUTING` | `on`/`off` — see [Model / harness routing](#model--harness-routing) | `on` |
| `MCP_INTROSPECT` | `on` spawns each configured MCP server briefly to list its tools for the prompt | `off` |
| `PROJECTS` | `key:Name:JIRA_PROJECT:path` entries, comma-separated | *(empty)* |
| `SKIP_PERMISSIONS` | `on` passes `--dangerously-skip-permissions` to harnesses that support it (`claude` only today); override per-run with `tommy --ask-permissions` | `on` |
| `MEMNOS_URL` | Where to reach memnos | `http://127.0.0.1:8900` |
| `MEMNOS_TOKEN` | Bearer token for memnos, if your server requires one | *(unset)* |

Two environment variables are read at config-load time and override the file (note the
second one's name):

| Env var | Overrides |
|---|---|
| `MEMNOS_URL` | `MEMNOS_URL` config key |
| `MEMNOS_SECRET_KEY` | `MEMNOS_TOKEN` config key — **not** a plain `MEMNOS_TOKEN` env var, which Tommy's config loader does not read at all, even though it's the variable name every other memnos integration in this repo uses |

Separately, on the interactive `tommy` CLI path, Tommy sets `MEMNOS_URL`, `TOMMY_NS`, and
`TOMMY_DEFAULT_NS` in the **harness subprocess's** environment (so the harness's own MCP
config, if it has memnos wired in, can pick them up); the `tommy_dispatch` MCP path sets
only `MEMNOS_URL` and `TOMMY_NS` on the child, not `TOMMY_DEFAULT_NS`. Neither path passes
`MEMNOS_TOKEN` through to the harness subprocess — if the harness's own memnos MCP wiring
needs a token, set it there independently (see the next section).

`PROJECTS` entries: `key:Name:JIRA_PROJECT:path` — `key` is what you pass to
`tommy --project <key>` / `tommy_switch_project`, `path` becomes the harness's working
directory and (on the CLI path) the "Active project" block in the prompt. Note:
`tommy_switch_project` sets which project is *active* for the MCP session, but it does not
actually change which memnos namespace `tommy_recall`/`tommy_remember`/`tommy_dispatch` use
— `ProjectEntry` has no `namespace` field, so the effective namespace always falls back to
`DEFAULT_NS` regardless of the active project.

---

## Integrating with existing CLIs

Tommy connects to other coding-agent CLIs in three separate ways. They're independent of
each other — wiring one doesn't wire the others.

### 1. Tommy supervises a harness as a subprocess

This is the default `tommy` CLI behavior described throughout this guide: Tommy `Popen`s
your configured harness (`claude`, `codex`, `aider`, `goose`, `cursor-agent`, `kiro`, or a
TOML drop-in) with a prompt file, waits for it, and captures its transcript afterward. No
MCP involved on this path — it's a plain subprocess launch with a file-based system prompt.
Pick the harness in `tommy.conf`'s `HARNESS=` key, or override per-invocation with
`tommy --project <key>` plus whatever the harness itself accepts via
`tommy [harness-flags...]` (unrecognized flags pass through to the harness).

### 2. Tommy exposed as an MCP server to an editor

`tommy --mcp` lets an editor (Claude Desktop, Cursor, VS Code + Continue, Zed) drive Tommy
itself over MCP — dispatching tasks, checking status, sending control messages, switching
project context — instead of a human running `tommy` at a terminal. See
[`agents/tommy/README.md`](../../agents/tommy/README.md#editor-integration) for the exact
config JSON for each editor. Tasks dispatched this way get the same core.md coordinator prompt
as the interactive `tommy` CLI path (see [How it works](#how-it-works)), memory-primed with a
single `recall()`, plus a short non-interactive framing note since there's no human at the
terminal to greet or ask a clarifying question.

### 3. The harness needs its own memnos MCP wiring — Tommy doesn't do this for it

This is the part most likely to surprise you, and it's the one that determines whether
leases, `corpus_check` routing, and pub/sub actually do anything. Tommy's `core.md` prompt
tells the harness session to call memnos MCP tools like `recall`, `remember`,
`lease_acquire`, `corpus_check`, `namespace_subscribe`. Those tools exist only if *that
specific harness* — separately from Tommy — has memnos registered as its own MCP server.
Tommy does not do this wiring for you. For Claude Code, that means running
[`memnos claude-setup`](claude-code-setup.md) (or `claude mcp add memnos ...`) once, so the
Claude Code session Tommy launches actually has `recall`/`remember`/`lease_acquire`/
`corpus_check`/etc. available as tools, not just instructions telling it to use tools that
don't exist. The equivalent for Codex is `memnos agent-setup codex` (see
[`clients/codex.md`](clients/codex.md)); for a generic MCP client see
[`../integrations/mcp.md`](../integrations/mcp.md).

If the harness *doesn't* have memnos's MCP tools wired in, core.md's instructions to call
them simply have nothing to call — the harness either skips them or the tool calls fail,
depending on how it handles an unknown tool name. Tommy's own memnos plumbing (the
`memnos_sdk`-based session journaling and transcript ingestion described earlier) still
works regardless, since that's Tommy's own process talking to memnos directly — it's only
the *harness session's* half of the memory loop that depends on this separate wiring.

`tommy/prompt.py` also injects an "Available MCP Servers" manifest into the interactive
prompt, built from `tommy/discovery/mcp.py`. That reader looks at Claude-Desktop-style
config files (`~/.claude/claude_desktop_config.json`,
`~/Library/Application Support/Claude/claude_desktop_config.json`,
`/etc/claude/mcp_servers.json`) — not Claude Code's own `~/.claude.json`, which is where
`claude mcp add` actually writes MCP server entries (see
[`claude-code-setup.md`](claude-code-setup.md)'s gotcha about exactly this file
distinction). If you're launching the `claude` harness and its MCP servers are registered
in `~/.claude.json` rather than a Claude-Desktop-style file, this manifest layer likely
won't reflect them.

---

## Known limitations

- **`tommy_dispatch` overrides two of core.md's interactive-only instructions, not the rest.**
  Both entry points load the same core.md coordinator prompt via `build_prompt()`, but
  `tommy_dispatch` has no human at the terminal, so its final prompt layer explicitly tells
  the harness not to print core.md's session-start greeting and not to ask the user a
  clarifying question (make the most reasonable assumption and proceed instead). Every other
  coordinator rule — leases, `corpus_check`, wave-based fan-out, spawn bounds — is unchanged
  and applies identically to both paths.
- **`MCP_INTROSPECT=on` now costs per `tommy_dispatch` call, not just once per CLI launch.**
  Since `tommy_dispatch` now calls the same `build_prompt()` the CLI path does,
  `format_mcp_manifest(introspect=cfg.mcp_introspect)` runs on every dispatch when
  `MCP_INTROSPECT` is on — each configured MCP server gets spawned with a 5s timeout while the
  editor blocks on the tool call. Default is `off`.
- **The control channel is inert against every harness in the built-in registry.** It's
  real, tested infrastructure, but `claude`, `codex`, `aider`, `goose`, `cursor-agent`, and
  `kiro` don't read `TOMMY_CTRL_PORT` or connect to it. Useful only for a harness you
  instrument yourself with `ControlClient`.
- **Leases, `corpus_check` routing, and pub/sub are prompted behavior, not enforced
  behavior.** They depend on both the interactive CLI path *and* the launched harness
  having memnos wired in as its own MCP server. Tommy's own code has no lease/corpus/pub-sub
  calls anywhere — `memnos_sdk.MemnosClient` doesn't expose those methods.
- **Blocking dispatch means control commands land between turns, not mid-flight.** `/stop`,
  `/pivot`, etc. take effect at the start of the harness's next turn — an architectural
  property of Task-tool-style dispatch, not a polling gap that will be closed later.
- **`tommy_switch_project` doesn't change the effective memnos namespace** — see
  [Configuration](#configuration). It does still change the active workspace/project
  metadata injected into the prompt.
- **No `tommy --version` flag.** It isn't registered as a Click option, so it falls through
  to `extra_args` and gets passed to the harness instead of printing Tommy's own version.
  Check the installed version with `pip show tommy-orchestrator` / `uv tool list`, or look
  at the (separately hardcoded, not read from `tommy.__version__`) banner Tommy prints on
  launch.
- **The harness `launch_template` command lines aren't independently verified against
  current vendor CLI versions.** If one doesn't work against what you have installed, add
  an override in `~/.memnos/harnesses/*.toml` rather than assuming the built-in template is
  current.
- **Transcript ingestion (Layer 3 memory capture) is Claude-Code-specific.** It looks for
  the most recently modified `~/.claude/projects/*/*.jsonl` file; running a different
  harness through Tommy doesn't get this step (it silently finds nothing to ingest).
- **Upgrading the PyPI package does not update an existing install's prompts.**
  `tommy.conf.default` sets `PROMPTS_DIR=~/.memnos/agents/tommy/prompts`, which
  `TommyConfig.load()` picks up from the bundled defaults even if the user never wrote
  their own `tommy.conf`. `tommy --install` (`install_prompts()` in `tommy/install.py`)
  copies the package's bundled `core.md` etc. into that directory only the first time —
  `dst_file.exists()` short-circuits every run after that unless `--force` is passed. So a
  `uv tool upgrade tommy-orchestrator` (or: `pip install --upgrade tommy-orchestrator`) that
  changes bundled `core.md` content — as this reviewer-dispatch change does — is invisible
  to anyone who already has a copy on disk until they run `tommy --install --force`. Not
  something this change fixes; flagging it because it's easy to assume a version bump alone
  ships new prompt behavior to existing users, and it doesn't.
- **`docs-gen` doesn't cover Tommy.** `docs/cli.md` and `ui/cli-reference.json` are
  generated from `memnos_cli.py`'s own Click tree; `agents/tommy` has a separate entry point
  and isn't part of that generator. This guide and the package README are the only
  documentation for Tommy's CLI/MCP surface — they aren't auto-checked against the code the
  way `docs/cli.md` is, so treat drift as possible and the source under `agents/tommy/` as
  the final authority.

---

## See also

- [`agents/tommy/README.md`](../../agents/tommy/README.md) — CLI flags, full MCP tool
  signatures, control-channel wire protocol, per-editor MCP config JSON.
- [`claude-code-setup.md`](claude-code-setup.md) — wiring memnos into Claude Code, which a
  Tommy-launched `claude` session also needs if you want it to actually call memnos's MCP
  tools rather than just being told to.
- [`clients/codex.md`](clients/codex.md) — the same, for Codex.
- [`../integrations/mcp.md`](../integrations/mcp.md) — wiring memnos into any other
  MCP-capable harness.
- [`../../RELEASING.md`](../../RELEASING.md) — how `tommy-orchestrator` is versioned and
  published independently of `memnos`/`memnos-sdk`.
