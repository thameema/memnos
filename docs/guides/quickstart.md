# engram Quickstart

## Prerequisites

- Docker + Docker Compose
- Python 3.11+
- An Anthropic API key (for multi-agent tasks and reflection; **not** required for memory/search)

> **No OpenAI key required.** engram uses `all-MiniLM-L6-v2` (sentence-transformers) for embeddings by default — it runs locally on CPU and requires no API key. OpenAI embeddings are available as an optional alternative.

---

## 1. Clone and configure

```bash
git clone https://github.com/thameema/engram.git
cd engram

cp engram.yaml.example engram.yaml
# Edit engram.yaml and set the following environment variables, or export them:
#
#   ARCADEDB_PASSWORD   — ArcadeDB root password (choose any strong password)
#   ENGRAM_API_KEY      — API key for engram's REST/MCP endpoints (generate one: openssl rand -hex 32)
#   ENGRAM_VAULT_KEY    — 32-byte key for vault encryption (generate: python -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
#   ANTHROPIC_API_KEY   — Only needed for multi-agent tasks and nightly reflection
```

---

## 2. Start the stack

```bash
docker compose up -d

# Watch logs until ready (usually 30–60s for first start)
docker compose logs -f engram
# Look for:
#   "engram API ready on :8766"
#   "MCP SSE server ready on :8765"
```

ArcadeDB starts on port 2480. The engram Python server starts on ports 8765 (MCP) and 8766 (API/dashboard).

---

## 3. Connect Claude Code

Choose one of two transport modes:

### Option A — stdio (recommended for local use)

The stdio transport requires no persistent HTTP server. Claude Code spawns `engram-mcp-stdio` on demand.

```bash
which engram-mcp-stdio   # confirm it is in PATH after pip install
```

Add to **`~/.claude.json`**:

```json
{
  "mcpServers": {
    "engram": {
      "type": "stdio",
      "command": "/Users/yourname/.venv/bin/engram-mcp-stdio",
      "env": {
        "ENGRAM_CONFIG": "/Users/yourname/engram/engram.yaml",
        "ARCADEDB_PASSWORD": "your-arcadedb-password",
        "ENGRAM_API_KEY": "your-engram-api-key",
        "ENGRAM_VAULT_KEY": "your-vault-key",
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

### Option B — SSE (HTTP, requires server running)

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

Fully restart Claude Code (quit and reopen), then run `/mcp` — you should see `engram  ✓ connected  · 18 tools`.

---

## 4. Add CLAUDE.md instructions

Add to **`~/.claude/CLAUDE.md`** so Claude uses engram automatically:

```markdown
## Memory System — engram MCP

ALWAYS call `memory_search` first when asked about past decisions, prior context, or anything previously remembered.
ALWAYS call `memory_write` when a key technical decision is made, or the user says "remember this".
Never use Bash grep or Obsidian file search to recall knowledge — use memory_search instead.
MCP results come back as plain text — read them directly, never spawn agents to parse them.

### Namespace guide
| Content type     | Namespace           |
|------------------|---------------------|
| Personal notes   | personal:me         |
| Team knowledge   | org:myteam          |
| Project-specific | project:myproject   |
```

---

## 5. Test memory

In a Claude Code session:

```
Use memory_write to save:
  content: "engram is working correctly"
  namespace: "personal:me"
  tags: ["test"]
```

In a new session (to confirm persistence):

```
Use memory_search to find:
  query: "engram working"
  namespace: "personal:me"
```

The memory persists across sessions.

---

## 6. Spawn a background task

```
Use spawn_task with:
  prompt: "Search memory for all test entries and summarize what's there"
  namespace: "personal:me"
  runtime: "api"
```

Then:

```
Use get_task_result with task_id: <the id from above> and wait: true
```

---

## 7. View the dashboard

Open `http://localhost:8766/dashboard` in your browser. Enter your API key when prompted.

You will see:
- Live stats: memory count, graph nodes, edges, namespaces
- Interactive force-directed knowledge graph — click any node to inspect it
- Namespace distribution and activity timeline

---

## 8. Encrypted vault

Store API keys and credentials securely:

```bash
# Store a secret
curl -X POST http://localhost:8766/api/v1/vault/secrets \
  -H "Authorization: Bearer your-engram-api-key" \
  -H "Content-Type: application/json" \
  -d '{"key_name": "GITHUB_TOKEN", "value": "ghp_...", "namespace": "personal:me", "note": "GitHub PAT"}'

# Retrieve a secret
curl http://localhost:8766/api/v1/vault/secrets/GITHUB_TOKEN?namespace=personal:me \
  -H "Authorization: Bearer your-engram-api-key"
```

Or via Claude Code: `"Store my GitHub token in the vault as GITHUB_TOKEN"`.

All vault access is written to an immutable audit log. See `GET /api/v1/vault/audit?namespace=personal:me`.

---

## 9. Optional: Enable Telegram gateway

1. Create a bot via [@BotFather](https://t.me/BotFather) — copy the token
2. Find your Telegram user ID (send `/start` to [@userinfobot](https://t.me/userinfobot))
3. Edit `engram.yaml`:
   ```yaml
   gateway:
     telegram:
       enabled: true
       bot_token: ${TELEGRAM_BOT_TOKEN}
       allowed_users: [your-numeric-user-id]
   ```
4. Set `TELEGRAM_BOT_TOKEN` and restart: `docker compose restart engram`

Send a message to your bot — engram will respond.

---

## 10. Optional: Migrate your Obsidian vault

```bash
python3 tools/migrate_obsidian.py \
  --vault ~/path/to/your/vault \
  --namespace obsidian:my-vault \
  --api-key your-engram-api-key
```

See [obsidian-migration.md](obsidian-migration.md) for the full guide.

---

## 11. Optional: Remote/team deployment

See [remote-deployment.md](remote-deployment.md) for VPS and Tailscale team setups.

For enterprise team configuration (namespace hierarchy, per-engineer API keys, shared server): see [enterprise-team-setup.md](enterprise-team-setup.md).

---

## Troubleshooting

**`/mcp` shows engram as disconnected (SSE mode)**
- Is the server running? `curl http://localhost:8765/health`
- Did you fully restart Claude Code?
- Is the API key in `~/.claude.json` correct?

**`/mcp` shows engram as disconnected (stdio mode)**
- Does the binary exist? `ls -la $(which engram-mcp-stdio)`
- Can it run? `ENGRAM_CONFIG=engram.yaml ARCADEDB_PASSWORD=... engram-mcp-stdio` (should start without error)
- Check `ENGRAM_CONFIG` points to an absolute path — relative paths may fail when Claude spawns the process
- The binary takes up to 40s on first start (embedding model loading). Claude Code's stdio timeout is 60s by default.

**`memory_search` returns no results**
- Check namespace: `org:myteam` won't find results stored under `personal:me`
- Use `namespace="all"` to search everything

**Claude uses Bash/grep instead of memory_search**
- CLAUDE.md instructions aren't loaded. Run `Read ~/.claude/CLAUDE.md` at session start.
- Or add the engram instructions to the project-level `CLAUDE.md` in your working directory.
