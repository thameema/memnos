# engram

**Persistent memory and multi-agent orchestration for Claude Code and any MCP-compatible LLM client.**

engram gives Claude Code a long-term memory that persists across sessions, the ability to fork parallel background agents, and a self-improving knowledge base — all backed by a single Docker container (ArcadeDB) with no external vector database or graph database required.

```
Claude Code  ──── MCP stdio or SSE ────►  engram server
                                            ├── Knowledge graph  (ArcadeDB — graph + HNSW vector)
                                            ├── Encrypted vault  (AES-256-GCM envelope encryption)
                                            ├── Multi-agent orchestrator
                                            ├── Self-learning    (reflection + heuristics)
                                            └── Mobile gateway   (Telegram / WhatsApp)
```

> **v0.2** — ArcadeDB replaces Neo4j + Qdrant + Graphiti. One container, no OpenAI key required for embeddings. See [DESIGN.md](DESIGN.md) for the full architecture.

---

## Why engram?

Claude Code forgets everything when a session ends. engram fixes that.

Every project decision, code pattern, error you debugged, and architectural choice lives in a temporal knowledge graph. Next session, engram surfaces it automatically.

- **Memory across sessions** — facts, decisions, and context persist across months and years
- **Team knowledge graph** — share memory across your org via namespaces (`org:acme`, `project:backend`, `personal:default`)
- **Multi-agent tasks** — one command forks N parallel background workers, collects results, tears them down
- **Encrypted vault** — store API keys and credentials with AES-256-GCM envelope encryption; auto-redacts credentials found in memory writes
- **Self-improving** — nightly reflection rewrites heuristics from failures; successful patterns become reusable skill templates
- **Mobile access** — send tasks from Telegram or WhatsApp; engram runs them and replies
- **No OpenAI key for embeddings** — ships with `all-MiniLM-L6-v2` (sentence-transformers, runs on CPU); OpenAI embeddings are optional

---

## How is engram different from Obsidian, Letta, mem0, or Hermes agents?

| | engram | Obsidian vault | Letta (formerly MemGPT) | mem0 |
|---|---|---|---|---|
| **What it is** | Memory + orchestration layer | Manual note vault | Stateful agent framework | Cloud memory API |
| **Works with Claude Code** | Native MCP (stdio + SSE) | Manual CLAUDE.md file | No direct MCP | API wrapper needed |
| **Memory capture** | CLAUDE.md-instructed; Claude writes automatically | Manual — you write notes | Sophisticated in-agent memory | API call required |
| **Knowledge graph** | ArcadeDB (graph + HNSW vector, single container) | No — flat Markdown | Structured in-context store | Flat key-value |
| **Team sharing** | Real-time shared graph | Git/iCloud sync (async) | No | API-based (cloud) |
| **Encrypted secrets** | Yes — AES-256-GCM vault + audit log | No | No | No |
| **Self-improving** | Nightly reflection, heuristic decay | No | No | No |
| **Multi-agent** | Built-in fork/join, critic loop | No | Agent-in-memory only | No |
| **Runs locally** | Yes — one Docker container | Yes — plain files | Partial | Cloud-only |
| **No cloud embeddings** | Yes (local sentence-transformers) | N/A | Depends | No |

### Where each tool wins

**Obsidian** wins when you want human-readable, manually curated notes with zero infrastructure. See [Migrating from Obsidian](#migrating-from-obsidian).

**Letta** has the most architecturally sophisticated approach to in-agent memory — the agent itself manages its memory policies. Best for stateful agents with complex self-directed memory needs.

**mem0** wins on operational simplicity: one REST call to write, one to search. Best for app developers who need a drop-in memory layer with no infrastructure.

**engram** wins when you need all three things in one self-hosted system: cross-session memory, a temporal knowledge graph, and multi-agent orchestration — with a single ArcadeDB container and no external API key for embeddings.

See [docs/enterprise-ai-engineering.md](docs/enterprise-ai-engineering.md) for the enterprise team model, and [docs/enterprise-team-setup.md](docs/enterprise-team-setup.md) for step-by-step team deployment.

---

## Quick Install

**One command (macOS or Linux):**

```bash
curl -fsSL https://raw.githubusercontent.com/thameema/engram/main/install.sh | bash
```

The installer will:
- Check Docker, Python 3.11+, and curl are installed
- Prompt for your Anthropic API key and generate secure credentials
- Pull the ArcadeDB Docker image and start it
- Install the Python packages and register `engram-mcp-stdio` in your PATH
- Create `~/.engram/` with your config and credentials
- Optionally wire engram into Claude Code's MCP servers automatically

**Or manually** (if you already have ArcadeDB running):

```bash
pip install engram-core engram-mcp-server
engram start
```

See [docs/quickstart.md](docs/quickstart.md) for the full step-by-step guide.

---

## Starting the stack

### Docker Compose (recommended)

```bash
git clone https://github.com/thameema/engram.git && cd engram
cp engram.yaml.example engram.yaml
# Edit engram.yaml — set ENGRAM_API_KEY, ARCADEDB_PASSWORD, ENGRAM_VAULT_KEY, ANTHROPIC_API_KEY

docker compose up -d

# Watch until ready
docker compose logs -f engram
# Look for: "MCP SSE server ready on :8765" and "engram API ready on :8766"
```

### Manual (dev mode)

```bash
git clone https://github.com/thameema/engram.git && cd engram

# Start ArcadeDB only
docker compose up -d arcadedb

# Install packages
pip install -e packages/core -e packages/mcp-server -e packages/api

# Start the server
ENGRAM_CONFIG=engram.yaml \
ARCADEDB_PASSWORD=your-password \
ENGRAM_API_KEY=your-api-key \
ENGRAM_VAULT_KEY=your-vault-key \
ANTHROPIC_API_KEY=sk-ant-... \
engram-server --config engram.yaml
```

---

## Connecting to Claude Code

engram connects to Claude Code as an MCP server. Two transports are available:

### Option A — stdio (recommended for local use)

The stdio transport spawns `engram-mcp-stdio` as a subprocess. No HTTP server needed; Claude Code manages the process lifetime.

Add to **`~/.claude.json`**:

```json
{
  "mcpServers": {
    "engram": {
      "type": "stdio",
      "command": "/path/to/engram-mcp-stdio",
      "env": {
        "ENGRAM_CONFIG": "/path/to/engram.yaml",
        "ARCADEDB_PASSWORD": "your-arcadedb-password",
        "ENGRAM_API_KEY": "your-engram-api-key",
        "ENGRAM_VAULT_KEY": "your-vault-key",
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

Find the binary path after installation: `which engram-mcp-stdio`

### Option B — SSE (HTTP, for remote/team servers)

Requires the `engram-server` process to be running separately (see above).

Add to **`~/.claude.json`**:

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

For a team server, replace `localhost:8765` with your shared server URL and issue each team member their own API key.

Fully restart Claude Code (quit and reopen), then run `/mcp` to confirm engram is connected.

### Step 2 — Add CLAUDE.md instructions

Registering the MCP server makes the tools available — but Claude won't use them automatically without instructions. Add to **`~/.claude/CLAUDE.md`**:

```markdown
## Memory System — engram MCP

ALWAYS call `memory_search` first when the user asks about past decisions, context, or anything previously remembered.
ALWAYS call `memory_write` when a key decision is made or the user says "remember this".
Never use Bash grep or file search to recall knowledge — use memory_search instead.
MCP results come back as plain text — read them directly, never spawn agents to parse them.

### Namespace guide
| Content type          | Namespace              |
|-----------------------|------------------------|
| Personal notes        | personal:me            |
| Shared team knowledge | org:myteam             |
| Project-specific      | project:myproject      |
```

See the complete guide in [docs/claude-code-setup.md](docs/claude-code-setup.md).

---

## MCP Tools

| Tool | What it does |
|------|-------------|
| `memory_search` | Semantic + graph search across persistent memory |
| `memory_write` | Persist a memory to the knowledge graph |
| `memory_delete` | Remove a memory entry by ID |
| `memory_get` | Retrieve a specific memory by ID |
| `graph_query` | Run ArcadeDB SQL queries on the knowledge graph |
| `get_entity` | Look up a named entity and its relationships |
| `get_related` | Get entities related to a given node (graph traversal) |
| `add_fact` | Add a subject-predicate-object triple to the graph |
| `spawn_task` | Fork a background worker agent |
| `get_task_result` | Retrieve a spawned task's output |
| `list_tasks` | List tasks for a namespace |
| `get_heuristics` | View learned rules distilled from past sessions |
| `list_agents` | List available agent definitions |
| `secret_set` | Store an encrypted secret in the vault |
| `secret_get` | Retrieve a secret by name |
| `secret_list` | List vault secrets (metadata only, never plaintext) |
| `secret_rotate` | Re-encrypt a secret with a fresh key |
| `secret_audit` | View the vault access audit log |

---

## Architecture

| Component | Purpose | Technology |
|-----------|---------|------------|
| `packages/core` | Memory client — graph + HNSW vector | ArcadeDB, sentence-transformers |
| `packages/mcp-server` | MCP tools for Claude Code | MCP Python SDK, FastAPI (SSE + stdio) |
| `packages/orchestrator` | Multi-agent task forking | asyncio, Anthropic SDK |
| `packages/api` | REST API and dashboard | FastAPI |
| `packages/gateway` | Mobile messaging | python-telegram-bot, Evolution API |
| `packages/learning` | Self-improvement | Reflection, skill extraction, APScheduler |

**Infrastructure:** one Docker container (ArcadeDB) — no Neo4j, no Qdrant, no Graphiti.

ArcadeDB provides the full graph + vector stack: native graph traversal (entities, facts, edges), HNSW vector index for semantic search, and transactional storage — all in a single JVM process.

---

## Encrypted Vault

engram ships a built-in secrets vault using AES-256-GCM envelope encryption:

- Each secret is encrypted with a unique data-encryption key (DEK)
- The DEK is encrypted with the key-encryption key (KEK) derived from `ENGRAM_VAULT_KEY`
- The vault stores only ciphertext — plaintext never touches ArcadeDB
- Every access (set, get, list, rotate) is written to an immutable audit log
- **Auto-redaction**: if a write to `memory_write` contains a credential pattern (API key, JWT, AWS key, etc.), engram automatically redacts it before storage and logs a warning

```bash
# Store a secret
curl -X POST http://localhost:8766/api/v1/vault/secrets \
  -H "Authorization: Bearer your-key" \
  -H "Content-Type: application/json" \
  -d '{"key_name": "OPENAI_KEY", "value": "sk-...", "namespace": "personal:me", "note": "OpenAI key for embeddings"}'

# Or via Claude Code: "Store my OpenAI key in the vault as OPENAI_KEY"
```

For production, switch the KMS provider to Azure Key Vault or AWS KMS in `engram.yaml`.

---

## Knowledge Graph

When you write a memory, engram automatically:
1. Embeds the content with `all-MiniLM-L6-v2` (or OpenAI if configured)
2. Stores the vector in ArcadeDB's HNSW index
3. Extracts named entities with spaCy (no LLM needed)
4. Creates Entity vertices and MENTIONS edges in the graph
5. Returns the memory ID

Searches use hybrid scoring: `0.7 × semantic_similarity + 0.3 × recency`.

You can also query the graph directly:

```
# In Claude Code
"Show me all memories that mention the auth service and are related to JWT"
→ Claude calls graph_query with ArcadeDB SQL
```

---

## Self-learning

engram improves over time through five mechanisms:

1. **Episodic memory** — every task is stored; the planner learns from past approaches
2. **Feedback loop** — correction detection and thumbs-up/down signals
3. **Reflection** — nightly LLM job distils failures into heuristics
4. **Skill extraction** — successful approaches become reusable templates
5. **Critic-worker loop** — optional critique + revision pass for high-stakes tasks

---

## For enterprise AI engineering teams

If your organisation runs AI-assisted engineering at scale — architects, developers, QA, DevOps all using Claude Code — engram is the shared memory layer that connects them.

With engram, the institutional knowledge accumulated by each role becomes immediately available to every team member's Claude Code session, including new hires on day one.

Read the guide: [docs/enterprise-ai-engineering.md](docs/enterprise-ai-engineering.md)
Step-by-step setup: [docs/enterprise-team-setup.md](docs/enterprise-team-setup.md)

---

## Mobile gateway (Telegram & WhatsApp)

The gateway lets you query your memory and run agent tasks from your phone:

```
Your phone ──► engram server ──► LLM (Anthropic API)
              └──► knowledge graph (shared with Claude Code)
```

The gateway shares the same namespaces as your Claude Code sessions. Memories written from Claude Code are searchable from your phone and vice versa.

See [docs/gateway.md](docs/gateway.md) for full setup and troubleshooting.

---

## Migrating from Obsidian

Import your entire Obsidian vault into engram in one command:

```bash
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/my-vault \
  --namespace obsidian:my-vault \
  --api-key your-engram-api-key
```

Imports every note as a memory, maps `[[wikilinks]]` to graph edges, and maps folder structure to sub-namespaces. Run `--dry-run` first to preview. See [docs/obsidian-migration.md](docs/obsidian-migration.md).

---

## Developer Setup

```bash
git clone https://github.com/thameema/engram.git && cd engram
make setup          # copies engram.yaml.example, installs all packages in dev mode
docker compose up -d arcadedb
ENGRAM_CONFIG=engram.yaml ARCADEDB_PASSWORD=... ENGRAM_API_KEY=... ENGRAM_VAULT_KEY=... \
  python -m engram_api.main
```

Run the test suite (requires ArcadeDB running):

```bash
cd /path/to/engram
ARCADEDB_PASSWORD=engram-dev-password \
ENGRAM_API_KEY=engram-local-dev-key \
ENGRAM_VAULT_KEY=dev-key-for-local-testing-only \
ENGRAM_CONFIG=engram.yaml \
.venv/bin/python -m pytest tools/test_engram.py -v

# MCP stdio transport tests
.venv/bin/python -m pytest tools/test_mcp_stdio.py -v
```

See [docs/quickstart.md](docs/quickstart.md) for the full guide.

---

## Contributing

engram is MIT-licensed and actively welcomes contributions.

**Where to start:**
- Browse [open issues](https://github.com/thameema/engram/issues) — anything tagged `good first issue` is a solid entry point
- Check [DESIGN.md](DESIGN.md) to understand what is planned vs what is built
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR

**What we need most:**
- **Integrations** — new MCP tools, gateway adapters (Discord, Slack, SMS)
- **KMS backends** — improve Azure Key Vault and AWS KMS vault providers
- **Embedding backends** — Cohere, voyage-ai, local Ollama alternatives
- **Learning algorithms** — better reflection prompts, smarter heuristic decay
- **Tests** — integration test coverage; Robot Framework suites welcome
- **Docs** — tutorials, recipes for common patterns, video walkthroughs

Before contributing: open an issue, fork and branch from `main`, run tests before submitting. We aim to review within 48 hours.

---

## Anthropic terms compliance

- engram uses Anthropic API keys only (not OAuth)
- Each user provides and pays for their own Anthropic key
- engram augments Claude Code; it is not a replacement or competing product

---

## License

MIT — see [LICENSE](LICENSE).
