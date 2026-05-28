# engram

**Persistent memory and AI governance for Claude Code and any MCP-compatible LLM client.**

engram gives Claude Code a long-term memory that persists across sessions and the ability to fork parallel background agents — all backed by a single Docker container (ArcadeDB) with no external vector database or graph database required.

```
Claude Code  ──── MCP stdio or SSE ────►  engram server
                                            ├── Knowledge graph  (ArcadeDB — graph + vector search)
                                            ├── Encrypted vault  (AES-256-GCM envelope encryption)
                                            ├── Multi-agent orchestrator
                                            ├── Self-learning    (reflection + heuristics)
                                            └── Mobile gateway   (Telegram / WhatsApp)
```

> **v1.1.0** — Corpus ingestion + architecture enforcement (`engram-sdk[corpus]`), LangChain and LlamaIndex integrations, 93 tests. ArcadeDB backend — one container, no OpenAI key required for embeddings. See [DESIGN.md](DESIGN.md) for the full architecture.

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

**engram** wins when you need all three things in one self-hosted system: cross-session memory, a temporal knowledge graph, and AI governance (decisions, constraints, ADRs) — with a single ArcadeDB container and no external API key for embeddings.

See [docs/guides/enterprise-ai-engineering.md](docs/guides/enterprise-ai-engineering.md) for the enterprise team model, and [docs/guides/enterprise-team-setup.md](docs/guides/enterprise-team-setup.md) for step-by-step team deployment.

---

## Quick Install

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/thameema/engram/master/install.sh | bash
```

The installer:
- Verifies Docker and Python 3.11+ are available
- Clones the engram source to `~/.engram-src/` (code only — safe to wipe + re-clone)
- Asks for **optional** Anthropic and OpenAI API keys (you can skip both — see below)
- Auto-generates `ENGRAM_API_KEY`, `ARCADEDB_PASSWORD`, and `ENGRAM_VAULT_KEY`
- Writes `~/.engram/.env` and `~/.engram/engram.yaml` (config + secrets — persistent)
- Builds the engram Docker image and starts the stack (arcadedb + engram, optionally qdrant)
- Installs Claude Code hooks + slash command + MCP registration (when "Full install" is chosen)
- Saves an install log at `/tmp/engram-install-*.log` for diagnostics

### What the installer prompts for

**Required (auto-generated if you press Enter):**
- Data directory — defaults to `~/.engram/`
- engram API key, ArcadeDB password, vault encryption key — all auto-generated as strong random tokens

**Anthropic API key — optional:**
- Only needed if you want engram to call the Anthropic API directly for reflection/skill extraction.
- If skipped, engram uses Claude Code's built-in `claude --print` CLI (the recommended path if you have Claude Code installed).

**Embeddings backend — pick carefully, the choice is mostly permanent.**

The installer shows a red warning before this prompt. Choosing OpenAI = paste your key, choosing Local = press Enter (skip OpenAI key).

| | **Local** (sentence-transformers all-MiniLM-L6-v2) | **OpenAI** (text-embedding-3-small) |
|---|---|---|
| Vector dim | 384 | 1536 |
| Lifetime cost | $0 | ~$0.02 per 1M tokens — pennies/month for personal use |
| Privacy | 100% offline | every memory's text sent to OpenAI |
| Disk weight | +2 GB on engram image | none |
| Build time impact | +3-5 min | none |
| Quality | ~80% of OpenAI on relevance benchmarks | best |
| Pick if | privacy-sensitive, offline, free | heavy use, want best relevance, ok with cloud |

> ⚠️ **Switching backends later is expensive and not always scripted.**
> Different models produce vectors in incompatible spaces — every existing memory must be re-encoded, and search breaks until the migration finishes.
> The repo ships `tools/reembed.py` for **local → OpenAI** only. Other transitions (OpenAI → local, local-model-A → local-model-B) require a custom migration script. **Decide now based on your real use case** — don't pick local "just to try" if you'll have 100K memories you can't reach with a script.

**Qdrant prompt:**
- Default off — ArcadeDB native vectors handle up to ~100K memories per namespace fine.
- Enable if you expect larger namespaces (HNSW ANN search).

### Verifying your install

```bash
bash ~/.engram-src/tools/verify-install.sh
```

Runs 9 sections of checks — file layout, configuration, container health, API auth, memory write+search roundtrip (proves embeddings work), namespaces, corpus, MCP/SSE, Claude Code wiring. Exit 0 means everything works; exit 1 prints remediation hints. Add `--skip-write` for a read-only check.

### File layout after install

| Path | What | Stability |
|---|---|---|
| `~/.engram-src/` | git clone (code) | Wipeable — re-clone with installer |
| `~/.engram/.env` | secrets (API keys, vault key, ArcadeDB password) | Persistent, mode 600 |
| `~/.engram/engram.yaml` | user-editable configuration | Persistent |
| `~/.engram/arcadedb/` | graph + vector data | Persistent |
| `~/.engram/qdrant/` | HNSW ANN index (when enabled) | Persistent |
| `~/.claude/hooks/engram*.sh` | Claude Code hooks | Persistent |
| `~/.claude.json` | Claude Code MCP config (entry added under `mcpServers.engram`) | Persistent |

### Choosing a version (default vs frozen release)

**The default `curl|bash` install pulls from `master`** — the always-current branch. Every commit that lands on master goes out to new installs immediately. Re-running the installer on top of an existing install does a `git pull` of master.

To pin a frozen release instead (e.g. for production deployments), pass `--version`:

```bash
# Pin to a frozen release tag
curl -fsSL https://raw.githubusercontent.com/thameema/engram/master/install.sh \
  | bash -s -- --version v1.4.0

# Pin a specific commit
curl -fsSL https://raw.githubusercontent.com/thameema/engram/master/install.sh \
  | bash -s -- --version <sha>

# Explicitly request master (same as default)
curl -fsSL https://raw.githubusercontent.com/thameema/engram/master/install.sh \
  | bash -s -- --version master
```

Available release tags: [github.com/thameema/engram/releases](https://github.com/thameema/engram/releases). Releases use semver — minor bumps (v1.x.0) ship new features, patch bumps (v1.x.y) ship fixes.

The `--version` flag is honoured on every re-run, so passing `--version v1.5.0` later upgrades your install to that exact release. Re-running with no flag refreshes from master.

### Re-running the installer

The installer detects an existing install and offers three modes:

| Mode | What it does |
|---|---|
| **1) Upgrade** | `git pull` source, rebuild image, restart. **Keeps your `.env` and data.** Recommended for routine updates. |
| **2) Fresh** | Re-prompt all configuration, rewrite `.env`. **Data directory left untouched** (no memory loss). |
| **3) Abort** | Exit, leave everything as-is. |

### Windows

Open **PowerShell as Administrator** and run:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser   # one-time, allows local scripts
irm https://raw.githubusercontent.com/thameema/engram/master/install-client.ps1 | iex
```

> **Requirements:** Windows 10/11, [Docker Desktop](https://www.docker.com/products/docker-desktop/),
> Python 3.10+ (`winget install Python.Python.3.11`), and
> [Claude Code for Windows](https://claude.ai/download).

The Windows installer:
- Downloads the Claude Code automation hooks (`engram-inject.ps1`, `engram-git-write.ps1`, etc.)
- Installs the heartbeat daemon (`engram-heartbeat.py`)
- Registers hooks in `%APPDATA%\Claude\claude_desktop_config.json`
- Points to your engram server (local or remote)

If engram is running on a different machine, pass the server URL and API key:

```powershell
irm https://raw.githubusercontent.com/thameema/engram/master/install-client.ps1 | iex -Args "-Server http://YOUR_SERVER:8766 -Key YOUR_API_KEY"
```

### Manual (all platforms)

```bash
git clone https://github.com/thameema/engram.git && cd engram
docker compose up -d
```

See [docs/guides/quickstart.md](docs/guides/quickstart.md) for the full step-by-step guide.

---

## Starting the stack

### Docker Compose (recommended for development)

The installer handles this for you. If you want to run compose manually from a clone, the config layout is:

```bash
git clone https://github.com/thameema/engram.git && cd engram

# Config and secrets live in ~/.engram/, NOT in the source clone.
# The installer normally writes these for you; for manual setup:
mkdir -p ~/.engram
cp .env.example ~/.engram/.env
# Then EDIT ~/.engram/.env and set at minimum:
#   ENGRAM_API_KEY       (any strong random string)
#   ARCADEDB_PASSWORD    (any strong random string)
#   ENGRAM_VAULT_KEY     (`python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`)
#   ENGRAM_EMBED_MODE    (`local` if you want offline embeddings, `online` for OpenAI)
#   ENGRAM_DATA_DIR=$HOME/.engram
#   ENGRAM_CONFIG_FILE=$HOME/.engram/engram.yaml
chmod 600 ~/.engram/.env

cp engram.yaml.example ~/.engram/engram.yaml

docker compose --env-file ~/.engram/.env up -d --build

# Watch until ready
docker compose --env-file ~/.engram/.env logs -f engram
# Look for: "Uvicorn running on http://0.0.0.0:8766" and "ArcadeDB ready"
```

> **Note:** all secrets come from `~/.engram/.env` via env-var interpolation
> (`engram.yaml` references `${ARCADEDB_PASSWORD}`, `${ENGRAM_API_KEY}`, etc).
> The `.env` file MUST live in `~/.engram/` so `docker compose --env-file` can
> find it and bind-mount the right `engram.yaml` into the container.

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
| Personal notes        | personal:default            |
| Shared team knowledge | org:myteam             |
| Project-specific      | project:myproject      |
```

See the complete guide in [docs/guides/claude-code-setup.md](docs/guides/claude-code-setup.md).

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

> **REST-only tools** (not MCP, call via HTTP): `GET /admin/keys`, `POST /admin/keys`, `DELETE /admin/keys/{id}` — runtime API key management.

---

## Architecture

| Component | Purpose | Technology |
|-----------|---------|------------|
| `packages/core` | Memory client — graph + vector search | ArcadeDB, numpy, sentence-transformers |
| `packages/mcp-server` | MCP tools for Claude Code | MCP Python SDK, FastAPI (SSE + stdio) |
| `packages/orchestrator` | Multi-agent task forking | asyncio, Anthropic SDK |
| `packages/api` | REST API and dashboard | FastAPI |
| `packages/gateway` | Mobile messaging | python-telegram-bot, Evolution API |
| `packages/learning` | Self-improvement | Reflection, skill extraction, APScheduler |
| `packages/sdk` | Python SDK — programmatic access, LangChain & LlamaIndex integrations | httpx, pydantic, langchain-core (optional) |

**Infrastructure (default):** one Docker container (ArcadeDB) — no Neo4j, no Graphiti. Vector search uses numpy-accelerated cosine similarity in the Python layer with a 5-minute TTL cache, scaling comfortably to ~100K memories.

**Optional Qdrant backend:** set `ENGRAM_VECTOR_BACKEND=qdrant` and start the `qdrant` profile to enable HNSW ANN search. See [Enabling Qdrant](#enabling-qdrant-optional) below. Recommended for corpora that will grow beyond ~100K memories or for single users wanting search quality that does not degrade over time.

---

## Embeddings and the LLM

### How engram uses two separate AI models

engram uses your conversational LLM (Claude, via Anthropic API) for reasoning and your embedding model for semantic search. These are different tasks:

| Task | Model | When |
|------|-------|------|
| Store a memory | Embedding model | At write time — content → stored vector |
| Search memories | Embedding model | At search time — query → query vector → cosine similarity |
| Reflect / summarise | LLM (Anthropic) | Nightly background job |
| Answer your question | LLM (Claude Code) | In conversation |

The LLM never does vector search. The embedding model never reasons. Both run every session.

**Why can't the search query go directly to the LLM?** The LLM would need to read all memories in its context window to find the relevant ones — at ~1K tokens per memory, 1000 memories = 1M tokens per query. That is too slow, too expensive, and hits context limits. Embeddings compress each memory into a fixed-size vector (384 or 1536 numbers). Cosine similarity finds the nearest vectors in milliseconds without reading the content.

### Anthropic does not provide an embedding API

Anthropic's Claude models are decoder-only LLMs — they cannot produce the fixed-dimension vectors that semantic search requires. A separate encoder-only model is needed.

engram ships three options:

| Mode | Model | Cost | Disk | Quality |
|------|-------|------|------|---------|
| `local` (default) | `all-MiniLM-L6-v2` | Free | ~90 MB | Good |
| `local-large` | `BAAI/bge-large-en-v1.5` | Free | ~1.3 GB | Better |
| `openai` | `text-embedding-3-small` | ~$0.02/1M tokens | None | Best |

Set `ENGRAM_EMBED_MODE` in your `.env` to choose. `auto` uses OpenAI if `OPENAI_API_KEY` is present, otherwise falls back to `all-MiniLM-L6-v2`.

### ⚠️ Embedding model lock-in — read before you start

**You cannot switch embedding models after writing memories without running a migration.**

Every memory stored in engram contains a vector produced by the embedding model that was active at write time. Different models produce different vector dimensions (384 vs 1536) and incompatible vector spaces — a query embedded with model B cannot find memories embedded with model A.

**If you switch models, all existing memories become invisible to search.**

A migration script (`tools/reembed.py`) exists to re-embed all ArcadeDB memories with the new model, and `tools/migrate_to_qdrant.py` syncs those vectors into Qdrant. But this process:
- Costs API tokens if switching to OpenAI embeddings
- Takes time proportional to your corpus size (749 memories ≈ 30 seconds with batching)
- Requires a maintenance window (search quality degrades mid-migration)

**Recommendation:** decide on your embedding model before writing your first memory. If you are an individual developer, `local` (free, no API key) is fine for most corpora. If you want the best semantic quality and don't mind a small ongoing cost, use `openai`.

---

## Enabling Qdrant (optional)

The default ArcadeDB vector search fetches the 500 most recent memories and does cosine similarity in Python. This works well up to a few thousand memories but degrades as the corpus grows — older memories fall outside the 500-record window and become unsearchable.

Qdrant replaces this with an HNSW index that searches all memories in ~3 ms regardless of corpus size.

### First-time setup

```bash
# 1. Install the Qdrant client inside the engram container
pip install 'qdrant-client>=1.9'
# Or rebuild: ENGRAM_EMBED_MODE=... docker compose build engram

# 2. Start Qdrant
docker compose --profile qdrant up -d qdrant

# 3. Backfill your existing memories into Qdrant (run once)
python3 tools/migrate_to_qdrant.py

# 4. Enable the Qdrant backend — add to ~/.engram/.env or your .env:
echo "ENGRAM_VECTOR_BACKEND=qdrant" >> .env
echo "QDRANT_URL=http://localhost:6333" >> .env

# 5. Restart engram to pick up the new config
docker compose restart engram
```

### Verify it's working

```bash
curl -s "http://localhost:8766/api/v1/memory/search?q=test&ns=all" \
  -H "Authorization: Bearer your-key" | python3 -m json.tool | head -20
```

Response time should drop from ~200 ms to ~10 ms on a warm query after enabling Qdrant.

### Data directory

Qdrant data is persisted at `~/.engram/qdrant/` (or `$ENGRAM_DATA_DIR/qdrant/`). Include this directory in your backups.

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
  -d '{"key_name": "OPENAI_KEY", "value": "sk-...", "namespace": "personal:default", "note": "OpenAI key for embeddings"}'

# Or via Claude Code: "Store my OpenAI key in the vault as OPENAI_KEY"
```

For production, switch the KMS provider to Azure Key Vault or AWS KMS in `engram.yaml`.

---

## API Key Management

### YAML keys (static, in `engram.yaml`)

Keys in `engram.yaml` are loaded at startup. Use these for permanent integrations and team members.

```yaml
auth:
  api_keys:
    - key: "${ENGRAM_API_KEY}"
      user_id: admin
      namespaces: ["*"]           # admin: access everything
      read_only: false

    - key: "${WEBAPP_KEY}"
      user_id: webapp
      namespaces: ["team:docs"]
      read_only: true             # web app: query-only, cannot write or delete
```

### Runtime keys (via dashboard or REST API)

Create, list, and revoke keys without restarting the server. Runtime keys are stored in `~/.engram/keys.db` (SHA-256 hashed; plaintext shown exactly once on creation).

**Via the dashboard** — open `/dashboard` and click the **API Keys** tab.

**Via REST** (admin key required):

```bash
# List runtime keys
curl http://localhost:8766/api/v1/admin/keys \
  -H "Authorization: Bearer your-admin-key"

# Create a read-only key scoped to one namespace
curl -X POST http://localhost:8766/api/v1/admin/keys \
  -H "Authorization: Bearer your-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "webapp", "namespaces": ["team:docs"], "read_only": true}'

# Response includes the plaintext key — copy it now, it is not stored
# { "key": "eng_abc123...", "id": "uuid", "user_id": "webapp", ... }

# Revoke a key
curl -X DELETE http://localhost:8766/api/v1/admin/keys/{id} \
  -H "Authorization: Bearer your-admin-key"
```

### Read-only enforcement

A key with `read_only: true` will receive HTTP 403 on any `memory_write`, `memory_delete`, or vault mutation. It can call `memory_search`, `memory_get`, `graph_query`, `get_entity`, `get_related`, `secret_get`, and `secret_list` freely.

---

## Knowledge Graph

When you write a memory, engram automatically:
1. Embeds the content with `all-MiniLM-L6-v2` (or OpenAI if configured)
2. Stores the vector in ArcadeDB alongside the memory record
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

Read the guide: [docs/guides/enterprise-ai-engineering.md](docs/guides/enterprise-ai-engineering.md)
Step-by-step setup: [docs/guides/enterprise-team-setup.md](docs/guides/enterprise-team-setup.md)

---

## Mobile gateway (Telegram & WhatsApp)

The gateway lets you query your memory and run agent tasks from your phone:

```
Your phone ──► engram server ──► LLM (Anthropic API)
              └──► knowledge graph (shared with Claude Code)
```

The gateway shares the same namespaces as your Claude Code sessions. Memories written from Claude Code are searchable from your phone and vice versa.

See [docs/guides/gateway.md](docs/guides/gateway.md) for full setup and troubleshooting.

---

## Backup & Restore

### Run a backup

```bash
bash tools/backup.sh
```

Stops both containers for ~15 seconds, rsyncs `~/.engram/arcadedb/` plus the SQLite sidecars to a timestamped directory, then restarts everything. Keeps the last 7 backups automatically.

```bash
# Backup with record-count verification
bash tools/backup.sh --verify

# Backup to a custom location (e.g. external drive)
bash tools/backup.sh /Volumes/External/engram-backups
```

### Schedule daily backups

Add to your crontab (`crontab -e`):

```
0 2 * * * cd ~/git/engram && bash tools/backup.sh >> ~/.engram/backup.log 2>&1
```

### Restore from a backup

```bash
# 1. Stop containers
docker compose stop engram arcadedb

# 2. Replace data directory with the backup
rsync -a --delete \
  ~/.engram/backups/20260523_203208/arcadedb/ \
  ~/.engram/arcadedb/

# 3. Optionally restore SQLite sidecars
cp ~/.engram/backups/20260523_203208/keys.db ~/.engram/
cp ~/.engram/backups/20260523_203208/learning.db ~/.engram/
cp ~/.engram/backups/20260523_203208/tasks.db ~/.engram/

# 4. Restart
docker compose start arcadedb engram
```

Backups are stored at `~/.engram/backups/<timestamp>/` and include the full ArcadeDB database plus the encrypted vault key store, learning database, and task database.

---

## Migrating from Obsidian

Import your entire Obsidian vault into engram in one command:

```bash
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/my-vault \
  --namespace obsidian:my-vault \
  --api-key your-engram-api-key
```

Imports every note as a memory, maps `[[wikilinks]]` to graph edges, and maps folder structure to sub-namespaces. Run `--dry-run` first to preview. See [docs/guides/obsidian-migration.md](docs/guides/obsidian-migration.md).

---

## Python SDK

Install the SDK to access engram from any Python application or AI framework:

```bash
pip install engram-sdk                        # core SDK
pip install 'engram-sdk[langchain]'           # + LangChain memory backend
pip install 'engram-sdk[llamaindex]'          # + LlamaIndex reader
pip install 'engram-sdk[all]'                 # all integrations
```

### Basic usage

```python
from engram_sdk import EngramClient

with EngramClient(url="http://localhost:8766", api_key="your-key") as client:
    # Write a memory
    client.write(
        "Selected ArcadeDB over Neo4j+Qdrant — single container, multi-model",
        namespace="org:acme:engineering",
        memory_type="decision",
        affects=["database", "infrastructure"],
        rationale="Eliminates two separate services, no external vector DB",
    )

    # Search memories
    results = client.search("database architecture decisions", namespace="org:acme:engineering")
    for r in results:
        print(f"[{r.memory_type}] {r.content}")
```

### LangChain integration

Drop engram in as a memory backend for any LangChain chain or agent:

```python
from langchain.chains import ConversationChain
from engram_sdk import EngramClient
from engram_sdk.integrations.langchain import EngramMemory

client = EngramClient(url="http://localhost:8766", api_key="your-key")
memory = EngramMemory(client=client, namespace="org:acme", session_id="session-42")

chain = ConversationChain(llm=your_llm, memory=memory)
chain.run("What database should we use for the user service?")
# → memories from past sessions automatically injected as context
```

Install: `pip install 'engram-sdk[langchain]'`

### LlamaIndex integration

Load engram memories as LlamaIndex Documents for RAG pipelines:

```python
from llama_index.core import VectorStoreIndex
from engram_sdk import EngramClient
from engram_sdk.integrations.llamaindex import EngramReader

client = EngramClient(url="http://localhost:8766", api_key="your-key")
reader = EngramReader(client=client, namespace="org:acme:engineering")

# Load memories as documents and build an index
documents = reader.load_data(query="authentication decisions", top_k=20)
index = VectorStoreIndex.from_documents(documents)
query_engine = index.as_query_engine()
print(query_engine.query("What auth approach did we choose?"))
```

Install: `pip install 'engram-sdk[llamaindex]'`

### Async client

All methods are available in async form via `AsyncEngramClient`:

```python
from engram_sdk import AsyncEngramClient

async with AsyncEngramClient(url="http://localhost:8766", api_key="your-key") as client:
    results = await client.search("auth decisions", namespace="org:acme")
    await client.write("JWT is our auth standard", namespace="org:acme", memory_type="decision")
```

---

## Corpus Ingestion & Architecture Enforcement

engram can ingest a repository of architecture documents (decisions, constraints, facts) and enforce them automatically in CI.

### Register a corpus

```bash
curl -X POST http://localhost:8766/api/v1/corpus/ \
  -H "Authorization: Bearer your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "architecture-docs",
    "connector_type": "git_doc",
    "config": {
      "repo_url": "https://github.com/acme/architecture",
      "branch": "main",
      "namespace": "org:acme:engineering"
    }
  }'

# Sync it
curl -X POST http://localhost:8766/api/v1/corpus/{id}/sync \
  -H "Authorization: Bearer your-key"
```

### Check code against architecture in CI

```python
from engram_sdk import EngramClient

with EngramClient(url="http://localhost:8766", api_key="your-key") as client:
    result = client.corpus.check(corpus_id, content=pull_request_diff)

    # SHALL violations = hard failures
    if result.shall_violations:
        print(result.format())
        raise SystemExit(1)   # blocks the merge

    # SHOULD violations = warnings
    for v in result.should_violations:
        print(f"Warning: {v.rule}")
```

**Severity levels:**

| Marker | Level | CI effect |
|--------|-------|-----------|
| `SHALL` / `MUST` | Hard constraint | Blocks merge |
| `MUST NOT` | Hard prohibition | Blocks merge |
| `SHOULD` | Recommendation | Warning annotation |
| `MAY` | Suggestion | Informational |

### Add a quality gate to CI

```bash
# In your CI pipeline
python -m pytest tools/test_decision_coverage.py -v
```

This enforces that architecture decisions in engram have `affects[]` and `rationale` populated — catching low-quality memory writes before they accumulate.

---

## Developer Setup

```bash
git clone https://github.com/thameema/engram.git && cd engram
make setup          # copies engram.yaml.example, installs all packages in dev mode
docker compose up -d arcadedb
ENGRAM_CONFIG=engram.yaml ARCADEDB_PASSWORD=... ENGRAM_API_KEY=... ENGRAM_VAULT_KEY=... \
  python -m engram_api.main
```

Run the test suite:

```bash
cd /path/to/engram

# Unit tests — no ArcadeDB required (93 tests)
.venv/bin/python -m pytest tools/test_learning.py tools/test_api_features.py \
  tools/test_corpus.py tools/test_subscriptions.py -v

# Architecture decision quality gate
.venv/bin/python -m pytest tools/test_decision_coverage.py -v

# Integration tests — requires ArcadeDB running
ARCADEDB_PASSWORD=engram-dev-password \
ENGRAM_API_KEY=engram-local-dev-key \
ENGRAM_VAULT_KEY=dev-key-for-local-testing-only \
ENGRAM_CONFIG=engram.yaml \
.venv/bin/python -m pytest tools/test_arcadedb.py tools/test_corpus.py -v

# MCP stdio transport tests
.venv/bin/python -m pytest tools/test_mcp_stdio.py -v
```

See [docs/guides/quickstart.md](docs/guides/quickstart.md) for the full guide.

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
