# memnos

**Persistent memory and AI governance for Claude Code and any MCP-compatible LLM client.**

memnos gives Claude Code a long-term memory that persists across sessions and the ability to fork parallel background agents — all backed by a single Docker container (ArcadeDB) with no external vector database or graph database required.

```
Claude Code  ──── MCP stdio or SSE ────►  memnos server
                                            ├── Knowledge graph  (ArcadeDB — graph + vector search)
                                            ├── Encrypted vault  (AES-256-GCM envelope encryption)
                                            ├── Multi-agent orchestrator
                                            └── Self-learning    (reflection + heuristics)
```

> **v1.1.0** — Corpus ingestion + architecture enforcement (`memnos-sdk[corpus]`), LangChain and LlamaIndex integrations, 93 tests. ArcadeDB backend — one container, no OpenAI key required for embeddings. See [DESIGN.md](DESIGN.md) for the full architecture.

---

## Why memnos?

Claude Code and similar AI coding tools forget everything when you close the session. Every conversation starts from zero — past decisions, project context, architectural choices, and lessons learned are gone.

memnos is a self-hosted memory layer that sits alongside these tools. It stores what matters across sessions, across agents, and across your team — and injects it back into context automatically when it is relevant.

**You do not need to build your own infrastructure.** One Docker container handles the knowledge graph, vector search, encrypted vault, and multi-agent coordination. No external database, no cloud account, no API key required beyond what you already have.

### What memnos does

- Remembers decisions, patterns, and context across Claude Code sessions
- Shares memory across parallel background agents running simultaneously
- Enforces architecture constraints — written once, injected automatically into every future agent
- Stores secrets with AES-256-GCM encryption and an immutable audit log
- Runs entirely on your machine or your server — your data never leaves
- Integrates with LangChain, LlamaIndex, and other agent frameworks via the Python SDK
- Works with any MCP-compatible client, not just Claude Code

### What memnos does not do

- It is not an agent execution framework — it does not orchestrate tool calls or build chains
- It is not an observability or tracing platform
- It is not a managed cloud service — there is no hosted version
- It is not a replacement for your LLM — it stores and retrieves context; the LLM reasons
- Default ArcadeDB vector search scales to ~100K memories; use the optional Qdrant backend for larger corpora

### The four gaps memnos closes

**Code review with persistent context.** Your code review agent starts cold on every PR. Without memnos, the agent reviewing PR #200 doesn't know the architectural decision from PR #15, or the production incident it caused. memnos carries the full institutional context of your codebase into every review.

**Cross-agent coordination.** When a code reviewer, test writer, and deploy agent run in parallel, Agent B doesn't know what Agent A decided 10 minutes ago. memnos is the shared memory layer across all independent concurrent agents — visible to all of them simultaneously.

**Architecture constraint enforcement.** Rules live in system prompts — manually maintained, forgotten across sessions, invisible to new agents. memnos auto-injects stored constraints into every future search result across your entire agent fleet. Write once, enforced everywhere.

**Knowledge retention across the org.** When a senior engineer documents a decision in a meeting, no agent ever sees it. memnos is the bridge — decisions, incidents, patterns, and constraints written once are queryable by every agent, forever.

See [docs/guides/enterprise-ai-engineering.md](docs/guides/enterprise-ai-engineering.md) for the enterprise team model, and [docs/guides/enterprise-team-setup.md](docs/guides/enterprise-team-setup.md) for step-by-step team deployment.

---

## Quick Install

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/thameema/memnos/master/install.sh | bash
```

The installer:
- Verifies Docker and Python 3.11+ are available
- Clones the memnos source to `~/.memnos-src/` (code only — safe to wipe + re-clone)
- Asks for **optional** Anthropic and OpenAI API keys (you can skip both — see below)
- Auto-generates `MEMNOS_API_KEY`, `ARCADEDB_PASSWORD`, and `MEMNOS_VAULT_KEY`
- Writes `~/.memnos/.env` and `~/.memnos/memnos.yaml` (config + secrets — persistent)
- Builds the memnos Docker image and starts the stack (arcadedb + memnos, optionally qdrant)
- Installs Claude Code hooks + slash command + MCP registration (when "Full install" is chosen)
- Saves an install log at `/tmp/memnos-install-*.log` for diagnostics

### What the installer prompts for

**Required (auto-generated if you press Enter):**
- Data directory — defaults to `~/.memnos/`
- memnos API key, ArcadeDB password, vault encryption key — all auto-generated as strong random tokens

**Anthropic API key — optional:**
- Only needed if you want memnos to call the Anthropic API directly for reflection/skill extraction.
- If skipped, memnos uses Claude Code's built-in `claude --print` CLI (the recommended path if you have Claude Code installed).

**Embeddings backend — pick carefully, the choice is mostly permanent.**

The installer shows a red warning before this prompt. Choosing OpenAI = paste your key, choosing Local = press Enter (skip OpenAI key).

| | **Local** (sentence-transformers all-MiniLM-L6-v2) | **OpenAI** (text-embedding-3-small) |
|---|---|---|
| Vector dim | 384 | 1536 |
| Lifetime cost | $0 | ~$0.02 per 1M tokens — pennies/month for personal use |
| Privacy | 100% offline | every memory's text sent to OpenAI |
| Disk weight | +2 GB on memnos image | none |
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
bash ~/.memnos-src/tools/verify-install.sh
```

Runs 9 sections of checks — file layout, configuration, container health, API auth, memory write+search roundtrip (proves embeddings work), namespaces, corpus, MCP/SSE, Claude Code wiring. Exit 0 means everything works; exit 1 prints remediation hints. Add `--skip-write` for a read-only check.

### File layout after install

| Path | What | Stability |
|---|---|---|
| `~/.memnos-src/` | git clone (code) | Wipeable — re-clone with installer |
| `~/.memnos/.env` | secrets (API keys, vault key, ArcadeDB password) | Persistent, mode 600 |
| `~/.memnos/memnos.yaml` | user-editable configuration | Persistent |
| `~/.memnos/arcadedb/` | graph + vector data | Persistent |
| `~/.memnos/qdrant/` | HNSW ANN index (when enabled) | Persistent |
| `~/.claude/hooks/memnos*.sh` | Claude Code hooks | Persistent |
| `~/.claude.json` | Claude Code MCP config (entry added under `mcpServers.memnos`) | Persistent |

### Choosing a version (default vs frozen release)

**The default `curl|bash` install pulls from `master`** — the always-current branch. Every commit that lands on master goes out to new installs immediately. Re-running the installer on top of an existing install does a `git pull` of master.

To pin a frozen release instead (e.g. for production deployments), pass `--version`:

```bash
# Pin to a frozen release tag
curl -fsSL https://raw.githubusercontent.com/thameema/memnos/master/install.sh \
  | bash -s -- --version v1.4.0

# Pin a specific commit
curl -fsSL https://raw.githubusercontent.com/thameema/memnos/master/install.sh \
  | bash -s -- --version <sha>

# Explicitly request master (same as default)
curl -fsSL https://raw.githubusercontent.com/thameema/memnos/master/install.sh \
  | bash -s -- --version master
```

Available release tags: [github.com/thameema/memnos/releases](https://github.com/thameema/memnos/releases). Releases use semver — minor bumps (v1.x.0) ship new features, patch bumps (v1.x.y) ship fixes.

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
irm https://raw.githubusercontent.com/thameema/memnos/master/install-client.ps1 | iex
```

> **Requirements:** Windows 10/11, [Docker Desktop](https://www.docker.com/products/docker-desktop/),
> Python 3.10+ (`winget install Python.Python.3.11`), and
> [Claude Code for Windows](https://claude.ai/download).

The Windows installer:
- Downloads the Claude Code automation hooks (`memnos-inject.ps1`, `memnos-git-write.ps1`, etc.)
- Installs the heartbeat daemon (`memnos-heartbeat.py`)
- Registers hooks in `%APPDATA%\Claude\claude_desktop_config.json`
- Points to your memnos server (local or remote)

If memnos is running on a different machine, pass the server URL and API key:

```powershell
irm https://raw.githubusercontent.com/thameema/memnos/master/install-client.ps1 | iex -Args "-Server http://YOUR_SERVER:8766 -Key YOUR_API_KEY"
```

### Manual (all platforms)

```bash
git clone https://github.com/thameema/memnos.git && cd memnos
docker compose up -d
```

See [docs/guides/quickstart.md](docs/guides/quickstart.md) for the full step-by-step guide.

---

## Starting the stack

### Docker Compose (recommended for development)

The installer handles this for you. If you want to run compose manually from a clone, the config layout is:

```bash
git clone https://github.com/thameema/memnos.git && cd memnos

# Config and secrets live in ~/.memnos/, NOT in the source clone.
# The installer normally writes these for you; for manual setup:
mkdir -p ~/.memnos
cp .env.example ~/.memnos/.env
# Then EDIT ~/.memnos/.env and set at minimum:
#   MEMNOS_API_KEY       (any strong random string)
#   ARCADEDB_PASSWORD    (any strong random string)
#   MEMNOS_VAULT_KEY     (`python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`)
#   MEMNOS_EMBED_MODE    (`local` if you want offline embeddings, `online` for OpenAI)
#   MEMNOS_DATA_DIR=$HOME/.memnos
#   MEMNOS_CONFIG_FILE=$HOME/.memnos/memnos.yaml
chmod 600 ~/.memnos/.env

cp memnos.yaml.example ~/.memnos/memnos.yaml

docker compose --env-file ~/.memnos/.env up -d --build

# Watch until ready
docker compose --env-file ~/.memnos/.env logs -f memnos
# Look for: "Uvicorn running on http://0.0.0.0:8766" and "ArcadeDB ready"
```

> **Note:** all secrets come from `~/.memnos/.env` via env-var interpolation
> (`memnos.yaml` references `${ARCADEDB_PASSWORD}`, `${MEMNOS_API_KEY}`, etc).
> The `.env` file MUST live in `~/.memnos/` so `docker compose --env-file` can
> find it and bind-mount the right `memnos.yaml` into the container.

### Manual (dev mode)

```bash
git clone https://github.com/thameema/memnos.git && cd memnos

# Start ArcadeDB only
docker compose up -d arcadedb

# Install packages
pip install -e packages/core -e packages/mcp-server -e packages/api

# Start the server
MEMNOS_CONFIG=memnos.yaml \
ARCADEDB_PASSWORD=your-password \
MEMNOS_API_KEY=your-api-key \
MEMNOS_VAULT_KEY=your-vault-key \
ANTHROPIC_API_KEY=sk-ant-... \
memnos-server --config memnos.yaml
```

---

## Connecting to Claude Code

memnos connects to Claude Code as an MCP server. Two transports are available:

### Option A — stdio (recommended for local use)

The stdio transport spawns `memnos-mcp-stdio` as a subprocess. No HTTP server needed; Claude Code manages the process lifetime.

Add to **`~/.claude.json`**:

```json
{
  "mcpServers": {
    "memnos": {
      "type": "stdio",
      "command": "/path/to/memnos-mcp-stdio",
      "env": {
        "MEMNOS_CONFIG": "/path/to/memnos.yaml",
        "ARCADEDB_PASSWORD": "your-arcadedb-password",
        "MEMNOS_API_KEY": "your-memnos-api-key",
        "MEMNOS_VAULT_KEY": "your-vault-key",
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

Find the binary path after installation: `which memnos-mcp-stdio`

### Option B — SSE (HTTP, for remote/team servers)

Requires the `memnos-server` process to be running separately (see above).

Add to **`~/.claude.json`**:

```json
{
  "mcpServers": {
    "memnos": {
      "type": "sse",
      "url": "http://localhost:8765/sse",
      "headers": {
        "Authorization": "Bearer your-memnos-api-key"
      }
    }
  }
}
```

For a team server, replace `localhost:8765` with your shared server URL and issue each team member their own API key.

Fully restart Claude Code (quit and reopen), then run `/mcp` to confirm memnos is connected.

### Step 2 — Add CLAUDE.md instructions

Registering the MCP server makes the tools available — but Claude won't use them automatically without instructions. Add to **`~/.claude/CLAUDE.md`**:

```markdown
## Memory System — memnos MCP

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
| `packages/learning` | Self-improvement | Reflection, skill extraction, APScheduler |
| `packages/sdk` | Python SDK — programmatic access, LangChain & LlamaIndex integrations | httpx, pydantic, langchain-core (optional) |

**Infrastructure (default):** one Docker container (ArcadeDB) — no Neo4j, no Graphiti. Vector search uses numpy-accelerated cosine similarity in the Python layer with a 5-minute TTL cache, scaling comfortably to ~100K memories.

**Optional Qdrant backend:** set `MEMNOS_VECTOR_BACKEND=qdrant` and start the `qdrant` profile to enable HNSW ANN search. See [Enabling Qdrant](#enabling-qdrant-optional) below. Recommended for corpora that will grow beyond ~100K memories or for single users wanting search quality that does not degrade over time.

---

## Embeddings and the LLM

### How memnos uses two separate AI models

memnos uses your conversational LLM (Claude, via Anthropic API) for reasoning and your embedding model for semantic search. These are different tasks:

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

memnos ships three options:

| Mode | Model | Cost | Disk | Quality |
|------|-------|------|------|---------|
| `local` (default) | `all-MiniLM-L6-v2` | Free | ~90 MB | Good |
| `local-large` | `BAAI/bge-large-en-v1.5` | Free | ~1.3 GB | Better |
| `openai` | `text-embedding-3-small` | ~$0.02/1M tokens | None | Best |

Set `MEMNOS_EMBED_MODE` in your `.env` to choose. `auto` uses OpenAI if `OPENAI_API_KEY` is present, otherwise falls back to `all-MiniLM-L6-v2`.

### ⚠️ Embedding model lock-in — read before you start

**You cannot switch embedding models after writing memories without running a migration.**

Every memory stored in memnos contains a vector produced by the embedding model that was active at write time. Different models produce different vector dimensions (384 vs 1536) and incompatible vector spaces — a query embedded with model B cannot find memories embedded with model A.

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
# 1. Install the Qdrant client inside the memnos container
pip install 'qdrant-client>=1.9'
# Or rebuild: MEMNOS_EMBED_MODE=... docker compose build memnos

# 2. Start Qdrant
docker compose --profile qdrant up -d qdrant

# 3. Backfill your existing memories into Qdrant (run once)
python3 tools/migrate_to_qdrant.py

# 4. Enable the Qdrant backend — add to ~/.memnos/.env or your .env:
echo "MEMNOS_VECTOR_BACKEND=qdrant" >> .env
echo "QDRANT_URL=http://localhost:6333" >> .env

# 5. Restart memnos to pick up the new config
docker compose restart memnos
```

### Verify it's working

```bash
curl -s "http://localhost:8766/api/v1/memory/search?q=test&ns=all" \
  -H "Authorization: Bearer your-key" | python3 -m json.tool | head -20
```

Response time should drop from ~200 ms to ~10 ms on a warm query after enabling Qdrant.

### Data directory

Qdrant data is persisted at `~/.memnos/qdrant/` (or `$MEMNOS_DATA_DIR/qdrant/`). Include this directory in your backups.

---

## Encrypted Vault

memnos ships a built-in secrets vault using AES-256-GCM envelope encryption:

- Each secret is encrypted with a unique data-encryption key (DEK)
- The DEK is encrypted with the key-encryption key (KEK) derived from `MEMNOS_VAULT_KEY`
- The vault stores only ciphertext — plaintext never touches ArcadeDB
- Every access (set, get, list, rotate) is written to an immutable audit log
- **Auto-redaction**: if a write to `memory_write` contains a credential pattern (API key, JWT, AWS key, etc.), memnos automatically redacts it before storage and logs a warning

```bash
# Store a secret
curl -X POST http://localhost:8766/api/v1/vault/secrets \
  -H "Authorization: Bearer your-key" \
  -H "Content-Type: application/json" \
  -d '{"key_name": "OPENAI_KEY", "value": "sk-...", "namespace": "personal:default", "note": "OpenAI key for embeddings"}'

# Or via Claude Code: "Store my OpenAI key in the vault as OPENAI_KEY"
```

For production, switch the KMS provider to Azure Key Vault or AWS KMS in `memnos.yaml`.

---

## API Key Management

### YAML keys (static, in `memnos.yaml`)

Keys in `memnos.yaml` are loaded at startup. Use these for permanent integrations and team members.

```yaml
auth:
  api_keys:
    - key: "${MEMNOS_API_KEY}"
      user_id: admin
      namespaces: ["*"]           # admin: access everything
      read_only: false

    - key: "${WEBAPP_KEY}"
      user_id: webapp
      namespaces: ["team:docs"]
      read_only: true             # web app: query-only, cannot write or delete
```

### Runtime keys (via dashboard or REST API)

Create, list, and revoke keys without restarting the server. Runtime keys are stored in `~/.memnos/keys.db` (SHA-256 hashed; plaintext shown exactly once on creation).

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

When you write a memory, memnos automatically:
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

memnos improves over time through five mechanisms:

1. **Episodic memory** — every task is stored; the planner learns from past approaches
2. **Feedback loop** — correction detection and thumbs-up/down signals
3. **Reflection** — nightly LLM job distils failures into heuristics
4. **Skill extraction** — successful approaches become reusable templates
5. **Critic-worker loop** — optional critique + revision pass for high-stakes tasks

---

## For enterprise AI engineering teams

If your organisation runs AI-assisted engineering at scale — architects, developers, QA, DevOps all using Claude Code — memnos is the shared memory layer that connects them.

With memnos, the institutional knowledge accumulated by each role becomes immediately available to every team member's Claude Code session, including new hires on day one.

Read the guide: [docs/guides/enterprise-ai-engineering.md](docs/guides/enterprise-ai-engineering.md)
Step-by-step setup: [docs/guides/enterprise-team-setup.md](docs/guides/enterprise-team-setup.md)

---

---

## Backup & Restore

### Run a backup

```bash
bash tools/backup.sh
```

Stops both containers for ~15 seconds, rsyncs `~/.memnos/arcadedb/` plus the SQLite sidecars to a timestamped directory, then restarts everything. Keeps the last 7 backups automatically.

```bash
# Backup with record-count verification
bash tools/backup.sh --verify

# Backup to a custom location (e.g. external drive)
bash tools/backup.sh /Volumes/External/memnos-backups
```

### Schedule daily backups

Add to your crontab (`crontab -e`):

```
0 2 * * * cd ~/git/memnos && bash tools/backup.sh >> ~/.memnos/backup.log 2>&1
```

### Restore from a backup

```bash
# 1. Stop containers
docker compose stop memnos arcadedb

# 2. Replace data directory with the backup
rsync -a --delete \
  ~/.memnos/backups/20260523_203208/arcadedb/ \
  ~/.memnos/arcadedb/

# 3. Optionally restore SQLite sidecars
cp ~/.memnos/backups/20260523_203208/keys.db ~/.memnos/
cp ~/.memnos/backups/20260523_203208/learning.db ~/.memnos/
cp ~/.memnos/backups/20260523_203208/tasks.db ~/.memnos/

# 4. Restart
docker compose start arcadedb memnos
```

Backups are stored at `~/.memnos/backups/<timestamp>/` and include the full ArcadeDB database plus the encrypted vault key store, learning database, and task database.

---

## Migrating from Obsidian

Import your entire Obsidian vault into memnos in one command:

```bash
python3 tools/migrate_obsidian.py \
  --vault ~/vaults/my-vault \
  --namespace obsidian:my-vault \
  --api-key your-memnos-api-key
```

Imports every note as a memory, maps `[[wikilinks]]` to graph edges, and maps folder structure to sub-namespaces. Run `--dry-run` first to preview. See [docs/guides/obsidian-migration.md](docs/guides/obsidian-migration.md).

---

## Python SDK

Install the SDK to access memnos from any Python application or AI framework:

```bash
pip install memnos-sdk                        # core SDK
pip install 'memnos-sdk[langchain]'           # + LangChain memory backend
pip install 'memnos-sdk[llamaindex]'          # + LlamaIndex reader
pip install 'memnos-sdk[all]'                 # all integrations
```

### Basic usage

```python
from memnos_sdk import MemnosClient

with MemnosClient(url="http://localhost:8766", api_key="your-key") as client:
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

Drop memnos in as a memory backend for any LangChain chain or agent:

```python
from langchain.chains import ConversationChain
from memnos_sdk import MemnosClient
from memnos_sdk.integrations.langchain import MemnosMemory

client = MemnosClient(url="http://localhost:8766", api_key="your-key")
memory = MemnosMemory(client=client, namespace="org:acme", session_id="session-42")

chain = ConversationChain(llm=your_llm, memory=memory)
chain.run("What database should we use for the user service?")
# → memories from past sessions automatically injected as context
```

Install: `pip install 'memnos-sdk[langchain]'`

### LlamaIndex integration

Load memnos memories as LlamaIndex Documents for RAG pipelines:

```python
from llama_index.core import VectorStoreIndex
from memnos_sdk import MemnosClient
from memnos_sdk.integrations.llamaindex import MemnosReader

client = MemnosClient(url="http://localhost:8766", api_key="your-key")
reader = MemnosReader(client=client, namespace="org:acme:engineering")

# Load memories as documents and build an index
documents = reader.load_data(query="authentication decisions", top_k=20)
index = VectorStoreIndex.from_documents(documents)
query_engine = index.as_query_engine()
print(query_engine.query("What auth approach did we choose?"))
```

Install: `pip install 'memnos-sdk[llamaindex]'`

### Async client

All methods are available in async form via `AsyncMemnosClient`:

```python
from memnos_sdk import AsyncMemnosClient

async with AsyncMemnosClient(url="http://localhost:8766", api_key="your-key") as client:
    results = await client.search("auth decisions", namespace="org:acme")
    await client.write("JWT is our auth standard", namespace="org:acme", memory_type="decision")
```

---

## Corpus Ingestion & Architecture Enforcement

memnos can ingest a repository of architecture documents (decisions, constraints, facts) and enforce them automatically in CI.

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
from memnos_sdk import MemnosClient

with MemnosClient(url="http://localhost:8766", api_key="your-key") as client:
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

This enforces that architecture decisions in memnos have `affects[]` and `rationale` populated — catching low-quality memory writes before they accumulate.

---

## Developer Setup

```bash
git clone https://github.com/thameema/memnos.git && cd memnos
make setup          # copies memnos.yaml.example, installs all packages in dev mode
docker compose up -d arcadedb
MEMNOS_CONFIG=memnos.yaml ARCADEDB_PASSWORD=... MEMNOS_API_KEY=... MEMNOS_VAULT_KEY=... \
  python -m memnos_api.main
```

Run the test suite:

```bash
cd /path/to/memnos

# Unit tests — no ArcadeDB required (93 tests)
.venv/bin/python -m pytest tools/test_learning.py tools/test_api_features.py \
  tools/test_corpus.py tools/test_subscriptions.py -v

# Architecture decision quality gate
.venv/bin/python -m pytest tools/test_decision_coverage.py -v

# Integration tests — requires ArcadeDB running
ARCADEDB_PASSWORD=memnos-dev-password \
MEMNOS_API_KEY=memnos-local-dev-key \
MEMNOS_VAULT_KEY=dev-key-for-local-testing-only \
MEMNOS_CONFIG=memnos.yaml \
.venv/bin/python -m pytest tools/test_arcadedb.py tools/test_corpus.py -v

# MCP stdio transport tests
.venv/bin/python -m pytest tools/test_mcp_stdio.py -v
```

See [docs/guides/quickstart.md](docs/guides/quickstart.md) for the full guide.

---

## Contributing

memnos is MIT-licensed and actively welcomes contributions.

**Where to start:**
- Browse [open issues](https://github.com/thameema/memnos/issues) — anything tagged `good first issue` is a solid entry point
- Check [DESIGN.md](DESIGN.md) to understand what is planned vs what is built
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR

**What we need most:**
- **Integrations** — new MCP tools, agent framework adapters (AutoGen, CrewAI, Semantic Kernel)
- **KMS backends** — improve Azure Key Vault and AWS KMS vault providers
- **Embedding backends** — Cohere, voyage-ai, local Ollama alternatives
- **Learning algorithms** — better reflection prompts, smarter heuristic decay
- **Tests** — integration test coverage; Robot Framework suites welcome
- **Docs** — tutorials, recipes for common patterns, video walkthroughs

Before contributing: open an issue, fork and branch from `main`, run tests before submitting. We aim to review within 48 hours.

---

## Anthropic terms compliance

- memnos uses Anthropic API keys only (not OAuth)
- Each user provides and pays for their own Anthropic key
- memnos augments Claude Code; it is not a replacement or competing product

---

## License

MIT — see [LICENSE](LICENSE).
