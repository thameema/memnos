# engram Quickstart

## Prerequisites

- Docker + Docker Compose
- Python 3.11+
- An Anthropic API key
- An OpenAI API key (for embeddings)

## 1. Clone and configure

```bash
git clone https://github.com/yourorg/engram.git
cd engram

cp .env.example .env
# Edit .env:
#   NEO4J_PASSWORD=your-strong-password
#   ENGRAM_API_KEY=your-engram-api-key
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...

cp engram.yaml.example engram.yaml
# Edit engram.yaml if you need non-default settings
```

## 2. Start the stack

```bash
docker compose up -d

# Watch logs until ready
docker compose logs -f engram
# Look for: "engram API ready on :8766" and "MCP SSE server ready on :8765"
```

## 3. Connect Claude Code (local mode)

Add to `~/.claude/settings.json`:

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

Restart Claude Code. Type `/mcp` — you should see `engram` listed with 10+ tools.

## 4. Test memory

In a Claude Code session:
```
Use memory_write to save:
  content: "engram is working correctly"
  namespace: "personal:me"
  tags: ["test"]
```

Then in a new session:
```
Use memory_search to find:
  query: "engram working"
  namespace: "personal:me"
```

The memory persists across sessions.

## 5. Spawn a background task

```
Use spawn_task with:
  prompt: "Search memory for all test entries and summarize what's there"
  namespace: "personal:me"
  runtime: "api"
```

Get the task_id back, then:
```
Use get_task_result with task_id: <id> and wait: true
```

## 6. Optional: Enable Telegram

1. Create a bot via [@BotFather](https://t.me/BotFather) — copy the token
2. Find your Telegram user ID (send `/start` to [@userinfobot](https://t.me/userinfobot))
3. Edit `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your-token
   TELEGRAM_ALLOWED_USERS=your-numeric-user-id
   ```
4. Edit `engram.yaml`:
   ```yaml
   gateway:
     telegram:
       enabled: true
   ```
5. `docker compose restart engram`

Send a message to your bot — engram will respond.

## 7. View the knowledge graph dashboard

Open `http://localhost:8766/dashboard` in your browser. Enter your API key when prompted. You will see:
- Live stats: memory count, graph nodes, edges, namespaces
- Interactive force-directed graph — click any node to inspect it
- Namespace distribution and 30-day activity timeline

## 8. Optional: Migrate your Obsidian vault

If you have existing notes in Obsidian, import them:

```bash
python3 tools/migrate_obsidian.py \
  --vault ~/path/to/your/vault \
  --namespace obsidian:my-vault \
  --api-key your-engram-api-key
```

See [docs/obsidian-migration.md](obsidian-migration.md) for the full guide.

## 9. Optional: Telegram / WhatsApp gateway

See [docs/gateway.md](gateway.md) for full setup instructions, mode compatibility (API vs Claude Code), per-user namespaces, and troubleshooting.

## 10. Optional: Remote deployment

See [docs/remote-deployment.md](remote-deployment.md).
