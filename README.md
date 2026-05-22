# engram

**Persistent memory and multi-agent orchestration for Claude Code and any MCP-compatible LLM client.**

engram gives Claude Code a long-term memory that persists across sessions, the ability to fork parallel background agents, and a self-improving knowledge base that learns from your past interactions — all without replacing Claude Code or changing how you work.

```
Claude Code  ──── MCP/SSE ────►  engram server
                                    ├── Knowledge graph (Neo4j + Graphiti)
                                    ├── Vector store (Qdrant)
                                    ├── Multi-agent orchestrator
                                    ├── Self-learning (reflection + heuristics)
                                    └── Mobile gateway (Telegram / WhatsApp)
```

---

## Why engram?

Claude Code forgets everything when a session ends. engram fixes that.

Every project decision, code pattern, error you've debugged, and architectural choice lives in a temporal knowledge graph. Next session, engram surfaces it automatically.

- **Memory across sessions** — facts, decisions, and context persist in a knowledge graph that spans months and years
- **Team memory** — share knowledge across your org via namespaces (`org:acme`, `project:backend`, `personal:default`)
- **Multi-agent tasks** — one command forks N parallel background workers, collects results, tears them down
- **Self-improving** — nightly reflection rewrites heuristics from failures; successful patterns become reusable skill templates
- **Mobile access** — send tasks from Telegram or WhatsApp; engram runs them and replies
- **Bring your own key** — engram never holds your API keys; you configure your own Anthropic or OpenRouter key

---

## How is engram different from Obsidian, Letta, mem0, or Hermes agents?

There are several tools in this space. Here is an honest comparison:

| | engram | Obsidian vault | Letta (formerly MemGPT) | mem0 | Hermes agents | OpenClaw |
|---|---|---|---|---|---|---|
| **What it is** | Memory + orchestration layer | Manual note vault | Stateful agent framework | Cloud memory API | Fine-tuned LLMs for tool use | Node.js agent platform |
| **Primary audience** | Claude Code / AI engineering teams | Individual engineers, writers | General LLM app developers | App developers (SaaS API) | Developers running local LLMs | Developers, power users |
| **Works with Claude Code** | Native MCP integration | Manual CLAUDE.md file | No direct MCP | API wrapper needed | Different runtime (local LLM) | No direct MCP |
| **Memory capture** | Instructed via CLAUDE.md — Claude writes on key decisions | Manual — you write notes | Sophisticated in-agent memory | API call required | No built-in memory layer | Shallow MEMORY files |
| **Knowledge graph** | Neo4j + Graphiti (temporal) | No — flat Markdown files | Structured in-context store | Flat key-value store | No | No |
| **Team sharing** | Real-time shared graph | Git/iCloud sync (async) | No | API-based (cloud) | No | No |
| **Self-improving** | Nightly reflection, heuristic decay | No | No | No | Model weights are static | No |
| **Multi-agent orchestration** | Built-in (fork/join, critic loop) | No | Agent-in-memory architecture | No | You build it yourself | No |
| **Mobile gateway** | Telegram + WhatsApp | No | No | No | No | 50+ channels built-in |
| **Runs locally** | Yes — Docker + Python | Yes — plain files | Partial (some cloud deps) | Cloud-only | Yes — local inference | Yes — Node.js |
| **Access control** | Namespace ACL per API key | Folder structure only | No | API key only | No | No |
| **Open source** | MIT, self-hostable | Partial (core OSS) | Apache 2.0 | Partial (SDK only) | Apache 2.0 (NousResearch) | MIT |

### Where each tool genuinely wins

**Obsidian** wins when you want human-readable, manually curated notes with zero infrastructure. Notes are plain Markdown files — you can edit them in any editor, commit them to git, and read them without any running service. If you want direct control over exactly what gets stored and why, Obsidian is hard to beat. See [Migrating from Obsidian](#migrating-from-obsidian) if you want to bring your existing vault into engram.

**Letta** (formerly MemGPT) has the most architecturally sophisticated approach to in-agent memory. It treats the LLM itself as a process with working memory, a context manager, and persistent storage — the agent decides what to remember and forget within its own reasoning loop. This is powerful for stateful agents that need to manage their own memory policies.

**mem0** wins on operational simplicity. It is a clean REST API — one call to write, one to search — with a reported 91% reduction in p95 latency compared to full RAG pipelines. If you need a drop-in memory layer with minimal setup and no infrastructure to run, mem0 is a solid choice (the open-source version self-hosts; the cloud version is a paid service).

**Hermes agents** (NousResearch) — Hermes 2 Pro, Hermes 3, etc. — are LLMs fine-tuned for structured JSON output, tool use, and agentic reasoning. They are excellent at the *reasoning and planning* part of an agent. Hermes does not provide a memory or orchestration layer — you supply those. You can point a Hermes-based agent at engram's REST API for persistent memory.

**OpenClaw** wins on messaging channel breadth. It ships with 50+ pre-built channel adapters (Discord, Telegram, WhatsApp, Slack, X/Twitter, and more) and a large template library, making it fast to deploy an interactive bot across multiple platforms. Its memory system is shallow (flat key-value files); engram can serve as a deeper memory backend for OpenClaw agents.

**engram** wins when you need all three things together in one self-hosted system: cross-session memory that Claude Code writes based on CLAUDE.md instructions (key decisions, session summaries, explicit "remember this" requests), a temporal knowledge graph that connects facts across months of work, and built-in multi-agent task orchestration. The main trade-off: it requires Docker (Neo4j + Qdrant), has a higher operational footprint than Obsidian or mem0, and graph entity extraction requires an LLM API key.

See [docs/enterprise-ai-engineering.md](docs/enterprise-ai-engineering.md) for the full enterprise team model.

---

## Why we built this

Claude Code is the most capable coding assistant we have used. But it has a fundamental limitation: every session starts from zero.

You spend the first 10 minutes of every session re-explaining your project structure, your conventions, what you tried last week, and why you made certain decisions. Context is expensive. Repetition is expensive. And when a session hits the context limit, that knowledge evaporates.

We built engram to close three gaps:

1. **No cross-session memory.** Claude Code does not persist anything between sessions. We needed a memory layer that works with MCP — the protocol Claude Code already speaks.

2. **No cross-agent knowledge sharing.** When you run parallel agents, they each start blank. There is no shared state, no way to say "that agent already figured this out." engram provides a shared namespace each agent can read and write.

3. **No learning from experience.** Every failure is forgotten. Every successful pattern disappears. engram runs nightly reflection jobs that distil your interaction history into heuristics: "when debugging async code in this codebase, always check the event loop first." Those heuristics are injected into future sessions automatically.

engram is not a replacement for Claude Code. It is the long-term memory layer that Claude Code does not ship with.

---

## Migrating from Obsidian

Already using Obsidian as your external brain for Claude Code? Migrate your entire vault into engram in one command:

```bash
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/my-vault \
  --namespace obsidian:my-vault \
  --api-key your-engram-api-key
```

Imports every note as a memory, maps `[[wikilinks]]` to graph edges, and maps folder structure to sub-namespaces. Run `--dry-run` first to preview. See [docs/obsidian-migration.md](docs/obsidian-migration.md) for full guide.

---

## Quick Install

**One command (macOS or Linux):**

```bash
curl -fsSL https://raw.githubusercontent.com/thameema/engram/main/install.sh | bash
```

The installer will:
- Check Docker, Python 3.11+, and curl are installed
- Prompt for your LLM API key (Anthropic recommended) and generate secure credentials
- Pull Neo4j and Qdrant Docker images and start them
- Install the Python packages and the `engram` CLI
- Create `~/.engram/` with your config and data directories
- Optionally add engram to Claude Code's MCP servers automatically

**Or with pip** (if you already have Neo4j and Qdrant running):

```bash
pip install engram-ai
engram start
```

---

## Starting engram

```bash
engram start      # Start all services (Neo4j, Qdrant, Python server)
engram stop       # Stop all services
engram restart    # Restart all services
engram status     # Show health and running status
engram logs       # Tail server logs (engram|neo4j|qdrant|all)
engram config     # Print engram.yaml
```

### macOS — run engram automatically at login

Create a launchd plist so engram starts when you log in and restarts if it crashes:

```bash
# Generate and install the plist
cat > ~/Library/LaunchAgents/io.engram.server.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.engram.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>~/.local/bin/engram start</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>~/.engram/logs/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>~/.engram/logs/launchd.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ENGRAM_CONFIG</key>
    <string>~/.engram/engram.yaml</string>
  </dict>
</dict>
</plist>
EOF

# Load it
launchctl load ~/Library/LaunchAgents/io.engram.server.plist

# Check status
launchctl list | grep engram
```

To stop and unload:
```bash
launchctl unload ~/Library/LaunchAgents/io.engram.server.plist
```

### Linux — run engram as a systemd service

```bash
# Create the service file
sudo tee /etc/systemd/system/engram.service > /dev/null << EOF
[Unit]
Description=engram — persistent memory server
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=$USER
ExecStart=$HOME/.local/bin/engram start
ExecStop=$HOME/.local/bin/engram stop
Restart=on-failure
RestartSec=10
Environment=ENGRAM_CONFIG=$HOME/.engram/engram.yaml
StandardOutput=append:$HOME/.engram/logs/engram.log
StandardError=append:$HOME/.engram/logs/engram.err

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable engram
sudo systemctl start engram

# Check status
sudo systemctl status engram
sudo journalctl -u engram -f
```

To run as a user service (no sudo), place it in `~/.config/systemd/user/engram.service` and use `systemctl --user` commands instead.

---

## Connecting to Claude Code

engram is **not a replacement for Claude Code**. It is an MCP server that augments Claude Code with persistent memory and background agent capabilities. You keep using Claude Code exactly as you do today.

### Step 1 — Add to `~/.claude.json`

> **Important:** The config file is `~/.claude.json` (not `~/.claude/settings.json`).

Add the `engram` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "engram": {
      "type": "sse",
      "url": "http://localhost:8765/sse",
      "headers": {
        "Authorization": "Bearer your-engram-api-key"
      }
    }
  }
}
```

Fully restart Claude Code (quit and reopen), then run `/mcp` to confirm engram is connected.

### Step 2 — Add CLAUDE.md instructions

Registering the MCP server makes the tools available — but Claude won't use them automatically without instructions. Add memory routing rules to `~/.claude/CLAUDE.md`:

```markdown
## Memory System — engram MCP
ALWAYS use memory_search before answering questions about past decisions or context.
ALWAYS use memory_write when the user asks you to remember something, or when a key decision is made.
Never use Bash grep or file search to recall knowledge — use memory_search instead.
MCP results are plain text — read them directly, never spawn agents to parse them.
```

See the full guide in [`docs/claude-code-setup.md`](docs/claude-code-setup.md) for namespace configuration and troubleshooting.

Restart Claude Code after saving. Run `/mcp` to confirm engram is connected.

### Two modes of use

**Interactive mode** — you are in the session, engram stores and retrieves:
```
You:     "What was the auth approach we decided on for the user service?"
Claude:  [calls memory_search] "In session from 2026-03-12, you decided to use JWT
          with 24h expiry and refresh tokens stored in Redis. The key constraint
          was that mobile clients needed offline support."
```

**Autonomous mode** — engram runs background agents while you do other things:
```
You:     "Spawn an agent to audit all API endpoints for missing rate limits"
Claude:  [calls spawn_task] "Task spawned. I'll notify you when complete."
# ... minutes later ...
Claude:  [calls get_task_result] "Audit complete. Found 7 endpoints missing rate
          limits. Results saved to memory under 'audit:rate-limits-2026-05'."
```

Both modes use the same MCP tool interface. No special configuration needed to switch between them.

---

## MCP Tools

| Tool | What it does |
|------|-------------|
| `memory_search` | Semantic + graph search across persistent memory |
| `memory_write` | Persist information to the knowledge graph |
| `memory_delete` | Remove a memory entry |
| `memory_get` | Retrieve a specific memory by ID |
| `graph_query` | Run Cypher queries on the knowledge graph |
| `get_entity` | Look up an entity and its relationships |
| `get_related` | Get entities related to a given node |
| `add_fact` | Add a factual relationship to the graph |
| `spawn_task` | Fork a background worker agent |
| `get_task_result` | Retrieve a spawned task's output |
| `list_tasks` | List tasks for a namespace |
| `get_heuristics` | View learned rules from past failures |
| `list_agents` | List available agent definitions |

---

## Architecture

| Component | Purpose | Technology |
|-----------|---------|------------|
| `packages/core` | Memory client — graph + vector | Graphiti, Neo4j, Qdrant |
| `packages/mcp-server` | MCP tools for Claude Code | MCP Python SDK, FastAPI SSE |
| `packages/orchestrator` | Multi-agent task forking | asyncio, Anthropic SDK |
| `packages/api` | REST API | FastAPI |
| `packages/gateway` | Mobile messaging | python-telegram-bot, Evolution API |
| `packages/learning` | Self-improvement | Reflection, skill extraction, APScheduler |

---

## Agents

engram ships built-in agent definitions in `agents/builtin/`. Add your own in `agents/`:

```yaml
# agents/my-agent.yaml
name: my-agent
description: Does something specific for my workflow
model: claude-sonnet-4-6
system_prompt: |
  You are a specialist in...
tools:
  - memory_search
  - memory_write
use_critic: true
```

---

## Runtime modes

| Mode | How it runs | Use when |
|------|-------------|----------|
| `api` | Anthropic API tool-calling loop | Server, headless, default |
| `claude-code` | Claude Code CLI subprocess | Desktop, file editing tasks |
| `openrouter` | OpenRouter API | Multi-model, cost optimization |

---

## Self-learning

engram improves over time through five mechanisms:

1. **Episodic memory** — every task stored; planner learns from past approaches
2. **Feedback loop** — thumbs up/down and correction detection
3. **Reflection** — nightly LLM job distils failures into heuristics
4. **Skill extraction** — successful approaches become reusable templates
5. **Critic-worker loop** — optional critique + revision pass for high-stakes tasks

---

## For enterprise AI engineering teams

If your organisation runs AI-assisted engineering at scale — architects, developers, QA, DevOps all using Claude Code — engram is the shared memory layer that connects them.

The short version: Obsidian vaults make one engineer more productive. engram makes the whole team smarter together, with every decision, failure, and discovery automatically available to every team member's Claude Code session without manual curation.

Read the full guide: [docs/enterprise-ai-engineering.md](docs/enterprise-ai-engineering.md)

---

## Mobile gateway (Telegram & WhatsApp)

The gateway lets you query your memory and run agent tasks from your phone. It is fully two-way: send a message, engram processes it with an LLM, and replies — even for tasks that take minutes.

```
Your phone ──► engram server ──► LLM (API mode)
              └──► knowledge graph (shared with Claude Code)
```

The gateway shares the same namespaces and knowledge graph as your Claude Code sessions. Memories written from Claude Code are searchable from your phone, and tasks spawned from your phone appear in your task list.

See [docs/gateway.md](docs/gateway.md) for full setup, mode compatibility, and troubleshooting.

---

## Developer Setup

```bash
git clone https://github.com/thameema/engram.git && cd engram
make setup          # copies .env.example + engram.yaml.example, installs all packages in dev mode
# Edit .env with your API keys
make dev            # starts Neo4j + Qdrant via Docker Compose
python -m engram_api.main   # starts the Python server
```

See [docs/quickstart.md](docs/quickstart.md) for the full guide.

---

## Contributing

engram is MIT-licensed and actively welcomes contributions. The project is early — there is a lot of ground to cover.

**Where to start:**

- Browse [open issues](https://github.com/thameema/engram/issues) — anything tagged `good first issue` is a solid entry point
- Check the [design doc](DESIGN.md) to understand what is planned vs what is built
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR

**What we need most:**

- **Integrations** — new MCP tools, new gateway adapters (Discord, Slack, SMS)
- **Vector backends** — Pinecone, Weaviate, pgvector alternatives to Qdrant
- **Graph backends** — alternative to Neo4j for smaller deployments (SQLite-based graph?)
- **Learning algorithms** — better reflection prompts, smarter heuristic decay
- **Packaging** — Homebrew formula, Docker Hub image, proper PyPI release
- **Docs** — tutorials, "recipes" for common patterns, video walkthroughs
- **Tests** — unit and integration test coverage is thin; Robot Framework suites welcome

**Before contributing:**

1. Open an issue or comment on an existing one so we can coordinate
2. Fork the repo and create a feature branch from `main`
3. Run `make dev` to start local services, then `make test` before submitting
4. PRs should include a brief description of what changed and why
5. We aim to review within 48 hours

**Code style:** black + ruff for Python, standard async/await patterns throughout. No new dependencies without discussion.

---

## Anthropic terms compliance

- Claude Code CLI/SDK headless use requires `ANTHROPIC_API_KEY` (not OAuth) — engram uses API keys only
- engram does not resell API access — each user provides and pays for their own key
- engram augments Claude Code; it is not a competing product or a replacement for Claude Code

---

## License

MIT — see [LICENSE](LICENSE).
