# Agent Authoring Guide

engram agents are YAML files that tell the orchestrator how to handle specific types of tasks. Each file defines one agent: what model it uses, which tools it can call, how it should behave, and whether a critic pass is needed.

---

## Quick start

Create a file in the `agents/` directory (or any subdirectory):

```yaml
name: my-agent
version: "1.0"
description: Summarises meeting notes and writes key decisions to memory
model: claude-sonnet-4-6
temperature: 0.4
max_tokens: 4096
system_prompt: |
  You are a meeting summariser. When given a block of meeting notes:
  1. Extract the key decisions made
  2. Extract action items and owners
  3. Write each decision to memory with memory_write
  4. Return a short bullet-point summary

tools:
  - memory_write
  - memory_search
use_critic: false
timeout_s: 120
```

The agent is available immediately — no restart required.

---

## Field reference

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | **yes** | — | Unique identifier. Must be unique across all agent files. |
| `version` | string | no | `"1.0"` | Semver version string. |
| `description` | string | no | `""` | One-sentence description. Used by the agent router to match tasks. |
| `model` | string | no | `""` | Anthropic model ID (e.g. `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`). Falls back to orchestrator default when empty. |
| `temperature` | float | no | `null` | Sampling temperature (0.0–1.0). Lower = more focused. |
| `max_tokens` | int | no | `null` | Maximum tokens in the response. |
| `system_prompt` | string | no | `""` | Instructions prepended to every call. |
| `tools` | list[string] | no | `[]` | Tool names the agent can call. See [Available tools](#available-tools). |
| `use_critic` | bool | no | `false` | Run a critic pass after the agent responds. |
| `critic_model` | string | no | orchestrator default | Model for the critic. Defaults to `claude-haiku-4-5-20251001`. |
| `critic_prompt` | string | no | built-in | Custom critic instructions. |
| `timeout_s` | int | no | `300` | Per-agent timeout in seconds. |

### Name uniqueness

Names are deduplicated by the router — last file wins on name collision. Use `builtin/` for shared agents and the root `agents/` directory for project-specific overrides.

---

## Available tools

Tools are referenced by the string names below. The orchestrator injects the corresponding function at runtime.

| Tool name | What it does |
|-----------|--------------|
| `memory_search` | Semantic search over persistent memory |
| `memory_write` | Write a new memory entry |
| `memory_delete` | Delete a memory entry by ID |
| `memory_get` | Retrieve a memory entry by ID |
| `graph_query` | Execute a read-only ArcadeDB SQL query |
| `get_entity` | Look up a named entity in the knowledge graph |
| `get_related` | Traverse the graph from an entity |
| `add_fact` | Record a subject-predicate-object fact |
| `web_search` | Search the web (requires `BRAVE_API_KEY` or `SERPER_API_KEY`) |
| `fetch_url` | Fetch text content from a URL |
| `spawn_task` | Spawn a sub-task and return its task ID |
| `get_task_result` | Retrieve the result of a previously spawned sub-task |

---

## Critic loop

When `use_critic: true`, the orchestrator runs a second LLM call on the agent's output before returning it. The critic checks the quality of the response and either approves it (`LGTM`) or returns corrections. If corrections are returned, the agent reruns with those corrections prepended.

Use the critic for agents where output quality matters:

```yaml
use_critic: true
critic_model: claude-haiku-4-5-20251001
critic_prompt: |
  Review this code review report:
  1. Are the line numbers and file paths accurate?
  2. Are any obvious security issues missed?
  Reply with specific corrections, or LGTM if the report is complete and accurate.
```

When `critic_prompt` is omitted, a generic quality check is used.

---

## Agent routing

The orchestrator uses semantic similarity to route incoming tasks to the best agent. When a task arrives, the router:

1. Embeds the task description
2. Embeds each agent's `description` field
3. Returns the agent with the highest cosine similarity (threshold ≥ 0.82)
4. Falls back to the default `api` worker if no agent matches

**Write descriptions that match the language users use for tasks**, not technical jargon. Compare:

```yaml
# Poor: too technical, users won't phrase it this way
description: Applies NLP entity extraction to unstructured text corpora

# Good: matches natural task phrasing
description: Extracts named entities (people, organisations, dates) from documents
```

---

## File layout

```
agents/
├── builtin/              # Shared agents bundled with engram
│   ├── code-reviewer.yaml
│   ├── critic.yaml
│   ├── data-analyst.yaml
│   ├── doc-writer.yaml
│   ├── planner.yaml
│   ├── refactor.yaml
│   ├── researcher.yaml
│   ├── summarizer.yaml
│   ├── synthesizer.yaml
│   └── test-writer.yaml
└── my-custom-agent.yaml  # Project-specific agents
```

Set `ENGRAM_AGENTS_DIR` to point at a different directory:

```bash
ENGRAM_AGENTS_DIR=/path/to/my/agents engram-server
```

---

## REST API

List all agents:

```bash
curl -H "Authorization: Bearer your-key" \
  http://localhost:8766/api/v1/agents/
```

Get a specific agent:

```bash
curl -H "Authorization: Bearer your-key" \
  http://localhost:8766/api/v1/agents/researcher
```

Response includes: `name`, `version`, `description`, `model`, `tools`, `use_critic`, `timeout_s`, and a 200-character `system_prompt_preview`.

---

## MCP tools

Agents can be invoked from Claude Code via the orchestrator MCP tools:

```
spawn_task(prompt="summarise the Q3 meeting notes", agent="my-agent")
get_task_result(task_id="...")
```

---

## Example agents

### Researcher

```yaml
name: researcher
description: Researches topics by searching memory and the web, then synthesizes findings into a structured report
model: claude-sonnet-4-6
temperature: 0.5
max_tokens: 8192
system_prompt: |
  You are a research agent. When given a topic:
  1. Search memory first for existing knowledge
  2. Search the web if memory is insufficient
  3. Write key findings to memory with appropriate tags
  4. Return a structured report: Summary, Key Findings, Sources, Open Questions
tools:
  - memory_search
  - memory_write
  - web_search
  - fetch_url
use_critic: true
critic_model: claude-haiku-4-5-20251001
timeout_s: 240
```

### Quick summariser (no critic needed)

```yaml
name: summarizer
description: Summarises long documents, threads, or log output into bullet points
model: claude-haiku-4-5-20251001
temperature: 0.3
max_tokens: 2048
system_prompt: |
  Summarise the provided content into concise bullet points.
  Focus on facts, decisions, and action items. Omit filler.
tools: []
use_critic: false
timeout_s: 60
```

---

## Tips

- **Keep `timeout_s` realistic.** Agents that do web searches typically need 60–120 s. Complex research agents may need 240 s or more. The default 300 s is generous; set it explicitly to avoid surprise timeouts.
- **Prefer haiku for critics.** The critic's job is quality checking, not generation. `claude-haiku-4-5-20251001` is faster and cheaper without sacrificing review quality.
- **Use memory tools.** Agents that write findings to memory make future tasks cheaper — subsequent searches hit the cache instead of rerunning the web search.
- **Limit tools to what the agent actually needs.** An agent with only `memory_search` and `memory_write` is faster to route and easier to audit than one with all tools enabled.
- **Test routing.** Use the MCP `spawn_task` tool with a few realistic task descriptions to confirm the router sends them to the correct agent.
