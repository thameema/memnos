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

## How is engram different from Letta, mem0, or OpenHermes?

There are several excellent memory tools for LLMs. Here is how engram is different:

| | engram | Letta (formerly MemGPT) | mem0 | OpenHermes |
|---|---|---|---|---|
| **Primary audience** | Claude Code users | General LLM apps | App developers (SaaS API) | Chat / assistant |
| **Works with Claude Code** | Native MCP integration | No direct MCP | API wrapper needed | No |
| **Knowledge graph** | Neo4j + Graphiti (temporal) | Custom in-context | Flat key-value | No |
| **Multi-agent orchestration** | Built-in (fork/join, critic loop) | Agent-in-memory architecture | No | No |
| **Self-improving** | Nightly reflection, heuristic decay | No | No | No |
| **Mobile gateway** | Telegram + WhatsApp | No | No | No |
| **Runs locally** | Yes — Docker + Python, no cloud required | Partial | Cloud-only | No |
| **Open source** | MIT, self-hostable | Apache 2.0 | Partial | MIT |

**The short version:** Letta is a research-oriented framework for building stateful agents. mem0 is a cloud API. OpenHermes is a fine-tuned chat model. engram is purpose-built to give Claude Code a persistent, team-shareable brain that runs on your own machine — with no cloud dependency.

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

### Add to `~/.claude/settings.json`

The installer can do this automatically, or add it manually:

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
