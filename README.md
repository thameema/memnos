# engram

**Persistent memory and multi-agent orchestration for LLM workflows.**

engram gives Claude Code (and any MCP-compatible client) a long-term memory that persists across sessions, plus the ability to fork parallel background agents and learn from past interactions.

```
Claude Code  ──── MCP/SSE ────►  engram server
                                    ├── Knowledge graph (Neo4j + Graphiti)
                                    ├── Vector store (Qdrant)
                                    ├── Multi-agent orchestrator
                                    ├── Self-learning (reflection + heuristics)
                                    └── Mobile gateway (Telegram / WhatsApp)
```

## Why engram?

Claude Code forgets everything when a session ends. engram fixes that.

- **Memory across sessions** — facts, decisions, and context persist in a temporal knowledge graph
- **Team memory** — share knowledge across your org via namespaces (`org:acme`, `project:backend`)
- **Multi-agent tasks** — one command forks N parallel workers, collects results, tears them down
- **Self-improving** — nightly reflection rewrites heuristics from failures; successful patterns become reusable skill templates
- **Mobile access** — send tasks from Telegram or WhatsApp; engram runs them and replies
- **Bring your own key** — engram never holds your API keys. You configure your own Anthropic/OpenRouter key.

## Quickstart

```bash
git clone https://github.com/yourorg/engram.git && cd engram
cp .env.example .env && cp engram.yaml.example engram.yaml
# Edit .env with your API keys
docker compose up -d
```

Connect Claude Code — add to `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "engram": {
      "url": "http://localhost:8765/sse",
      "apiKey": "your-engram-api-key"
    }
  }
}
```

See [docs/quickstart.md](docs/quickstart.md) for the full guide.

## Architecture

| Component | Purpose | Technology |
|-----------|---------|------------|
| `packages/core` | Memory client — graph + vector | Graphiti, Neo4j, Qdrant |
| `packages/mcp-server` | MCP tools for Claude Code | MCP Python SDK, FastAPI SSE |
| `packages/orchestrator` | Multi-agent task forking | asyncio, Anthropic SDK |
| `packages/api` | REST API | FastAPI |
| `packages/gateway` | Mobile messaging | python-telegram-bot, Evolution API |
| `packages/learning` | Self-improvement | Reflection, skill extraction, APScheduler |

## MCP Tools

| Tool | What it does |
|------|-------------|
| `memory_search` | Semantic + graph search across persistent memory |
| `memory_write` | Persist information to the knowledge graph |
| `memory_delete` | Remove a memory entry |
| `graph_query` | Run Cypher queries on the knowledge graph |
| `get_entity` | Look up an entity and its relationships |
| `spawn_task` | Fork a background worker agent |
| `get_task_result` | Retrieve a spawned task's output |
| `list_tasks` | List tasks for a namespace |
| `get_heuristics` | View learned rules from past failures |
| `list_agents` | List available agent definitions |

## Agents

engram ships 10 built-in agent definitions in `agents/builtin/`. You can add your own in `agents/`:

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

## Runtime modes

| Mode | How it runs | Use when |
|------|-------------|----------|
| `api` | Anthropic API tool-calling loop | Server, headless, default |
| `claude-code` | Claude Code CLI subprocess | Desktop, file editing tasks |
| `openrouter` | OpenRouter API | Multi-model, cost optimization |

## Self-learning

engram improves over time through five mechanisms:

1. **Episodic memory** — every task stored; planner learns from past approaches
2. **Feedback loop** — thumbs up/down and correction detection
3. **Reflection** — nightly LLM job distils failures into heuristics
4. **Skill extraction** — successful approaches become reusable templates
5. **Critic-worker loop** — optional critique + revision pass for high-stakes tasks

## Terms compliance (Anthropic)

- Claude Code CLI/SDK headless use requires `ANTHROPIC_API_KEY` (not OAuth) — engram uses API keys only
- engram does not resell API access — each user provides their own key
- engram augments Claude Code; it is not a competing product

## License

Apache 2.0 — see [LICENSE](LICENSE).
