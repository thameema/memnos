# Gateway Guide — Telegram & WhatsApp

The engram gateway lets you talk to your persistent memory and run agent tasks from your phone using Telegram or WhatsApp. This guide explains how it works, how it relates to Claude Code, and how to set it up.

---

## What the gateway is

The gateway is a two-way messaging interface that runs inside the engram server. It receives messages from your phone, runs them through the orchestrator (which uses an LLM), and sends the response back to your phone.

```
Your phone
  │  (Telegram or WhatsApp)
  ▼
engram server ──► Orchestrator ──► LLM (Anthropic API / OpenRouter)
  │                                     │
  │             ┌───────────────────────┘
  └─◄───────── Response sent back to your phone
```

**The gateway talks to the engram server — not to Claude Code on your desktop.**

Claude Code and the gateway are two separate paths into the same engram server:

```
Claude Code desktop  ──── MCP/SSE ────►┐
                                        ├──► engram server ──► memory + orchestrator
Your phone (Telegram/WhatsApp)  ───────►┘
```

Both share the same knowledge graph, the same namespaces, and the same orchestrator. A task you spawn from your phone appears in your task list in Claude Code, and memories written from Claude Code are searchable from your phone.

---

## Two-way communication

Yes, both gateways are fully two-way:

1. You send a message to the bot
2. engram replies "Working…" immediately so you know it received the message
3. The orchestrator runs the task (this may take 10–90 seconds depending on complexity)
4. engram edits the "Working…" message with the result, or sends the full result as a file attachment if it is very long
5. For long tasks, engram sends "Still thinking…" updates every 45 seconds so you know it hasn't stalled

You do not need to wait for the response. Send the task, put your phone down, and the reply arrives when it is done.

---

## Gateway mode vs Claude Code mode

This is the most important thing to understand before setting up the gateway.

The engram orchestrator runs tasks in one of three modes:

| Mode | How it works | Gateway compatible? |
|------|-------------|---------------------|
| `api` | Calls Anthropic or OpenRouter API directly | **Yes** — works everywhere |
| `openrouter` | Calls OpenRouter API directly | **Yes** — works everywhere |
| `claude-code` | Runs `claude` CLI as a subprocess | Only if Claude Code is installed **on the same machine as engram** |

**The gateway works best in `api` or `openrouter` mode.**

`claude-code` mode requires the `claude` CLI binary to be present on the machine where engram is running. If engram is on a remote server, that means installing Claude Code on the server (possible, but unusual). If engram is running locally on your development machine, `claude-code` mode works fine from the gateway too.

For most users: set `default_runtime: api` in `engram.yaml` and use your Anthropic key. The gateway will work from anywhere.

```yaml
orchestrator:
  default_runtime: api   # api | openrouter | claude-code
  model: claude-sonnet-4-6
```

---

## Desktop vs remote: what works where

| Scenario | Claude Code MCP tools | Telegram gateway | WhatsApp gateway |
|----------|----------------------|-----------------|-----------------|
| engram local, Claude Code desktop | Yes | Yes (api mode) | Yes (api mode) |
| engram local, claude-code mode | Yes | Yes | Yes |
| engram on remote server, api mode | Yes (over network) | Yes | Yes |
| engram on remote server, claude-code mode | Yes | Only if `claude` CLI installed on server | Only if `claude` CLI installed on server |

The typical setup for solo developers: engram runs locally, Claude Code connects via MCP, and the gateway uses `api` mode so you can query your memory from your phone.

The typical setup for teams: engram runs on a VPS or cloud VM, everyone connects their Claude Code to it over the network, and the gateway uses `api` mode so all team members can query shared namespaces from their phones.

---

## Telegram setup

### 1. Create a bot

1. Open Telegram and search for `@BotFather`
2. Send `/newbot` and follow the prompts to name your bot
3. BotFather gives you a token like `7123456789:AAHx...` — copy it

### 2. Find your Telegram user ID

Send `/start` to `@userinfobot` in Telegram. It replies with your numeric user ID (e.g. `123456789`). This is what you put in `allowed_users` to restrict the bot to yourself.

### 3. Configure engram

In `.env`:
```
TELEGRAM_BOT_TOKEN=7123456789:AAHxYourTokenHere
TELEGRAM_ALLOWED_USERS=123456789
```

In `engram.yaml`:
```yaml
gateway:
  telegram:
    enabled: true
    bot_token: "${TELEGRAM_BOT_TOKEN}"
    allowed_users:
      - "${TELEGRAM_ALLOWED_USERS}"
    default_namespace: "personal:default"
```

To allow multiple users, add each ID on a new line:
```yaml
allowed_users:
  - "123456789"   # you
  - "987654321"   # teammate
```

If `allowed_users` is empty, the bot accepts messages from anyone. Do not do this on a public server.

### 4. Restart engram

```bash
engram restart
```

Open Telegram, find your bot by the username you gave it, and send `/start`.

### 5. Test it

```
/start
/help
/memory list
What was the last architectural decision I made about the auth service?
```

---

## Telegram commands reference

| Command | What it does |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | List all commands |
| `/memory search <query>` | Search your persistent memory |
| `/memory list` | Show recent memories |
| `/task status <task_id>` | Check the status of a background task |
| `/ns <namespace>` | Switch your active namespace for this session |
| `/ns` (no args) | Show your current namespace |
| Any other text | Runs as a task through the orchestrator |

Namespaces set with `/ns` are per-user and per-bot-session. They reset when the bot restarts. Your default namespace comes from `engram.yaml`.

---

## Long responses

When a response exceeds 4000 characters (Telegram's message limit), engram:
1. Sends the first 4000 characters as an edited message
2. Attaches the full response as a `.txt` file named `engram_result_<timestamp>.txt`

For WhatsApp the limit is 3500 characters — same behaviour.

---

## WhatsApp setup

WhatsApp does not have a public bot API. engram uses **Evolution API**, an open-source bridge that connects to WhatsApp via its web protocol. You need to run Evolution API yourself.

### 1. Run Evolution API

```bash
docker run -d \
  --name evolution-api \
  -p 8080:8080 \
  -e AUTHENTICATION_API_KEY=your-evolution-key \
  atendai/evolution-api:latest
```

Open `http://localhost:8080/manager` to access the Evolution API dashboard.

### 2. Create an instance and connect WhatsApp

In the Evolution API dashboard:
1. Create a new instance (e.g. `engram`)
2. It shows a QR code — scan it with WhatsApp on your phone
3. After scanning, the instance shows as "connected"

### 3. Set the webhook

Tell Evolution API to POST incoming messages to engram:
```bash
curl -X POST "http://localhost:8080/webhook/set/engram" \
  -H "apikey: your-evolution-key" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://host.docker.internal:8766/webhook/whatsapp",
    "webhook_by_events": false,
    "events": ["MESSAGES_UPSERT"]
  }'
```

Replace `host.docker.internal` with your server IP if engram is not on the same machine as Evolution API.

### 4. Configure engram

In `engram.yaml`:
```yaml
gateway:
  whatsapp:
    enabled: true
    evolution_api_url: "http://localhost:8080"
    evolution_api_key: "your-evolution-key"
    evolution_instance: "engram"
    default_namespace: "personal:default"
    allowed_phones:
      - "15551234567"   # your phone number, digits only, no + or spaces
```

If `allowed_phones` is empty, engram responds to all incoming WhatsApp messages. Restrict it to your number on a shared or public server.

### 5. Restart engram

```bash
engram restart
```

Send a WhatsApp message to the number associated with your Evolution API instance. engram will reply.

---

## Per-user namespaces

Each Telegram user ID maps to its own namespace by default: `personal:<user_id>`. This means two different users on the same bot have separate memory by default.

You can override this:
- Use `/ns team:backend` in Telegram to switch to a shared namespace for that session
- Configure `default_namespace: "team:backend"` in `engram.yaml` to make it the default for all users

For WhatsApp, the namespace is `personal:<phone_number>` by default.

---

## Using the gateway to query memories written from Claude Code

Because the gateway and Claude Code share the same engram server, you can access anything Claude Code wrote from your phone:

In a Claude Code session:
```
Use memory_write to save:
  content: "We decided to use JWT with 24h expiry for the auth service. Refresh tokens in Redis."
  namespace: "project:backend"
  tags: ["auth", "decision"]
```

From Telegram:
```
/ns project:backend
What auth approach did we decide on for the backend?
```

engram searches the knowledge graph and returns the stored decision.

---

## Spawning tasks from your phone

You can spawn background agent tasks from Telegram or WhatsApp just by sending a message:

```
Telegram: "Audit all memories in project:backend for outdated API endpoints
           and summarize what needs updating"
```

engram runs this as an orchestrator task in `api` mode. The result comes back as a Telegram reply, however long it takes.

For very long-running tasks, you can also ask for the task ID and check it later:

```
Telegram: "Run a full audit of project:backend and give me the task ID"
engram: "Task spawned: task_abc123. I'll run this in the background."

# later
Telegram: /task status task_abc123
engram: "COMPLETED — [full audit result]"
```

---

## Security

- **Always set `allowed_users` / `allowed_phones`** unless you want anyone to query your memory
- Your Telegram bot token and WhatsApp credentials are in `.env` — keep that file private (`chmod 600 .env`)
- Do not commit `.env` to version control
- For remote deployments, keep the Evolution API port (8080) firewalled and only accessible from your engram server
- Telegram bot tokens can be revoked and regenerated via `@BotFather` at any time

---

## Troubleshooting

**Bot does not respond to messages**

Check that engram is running and the gateway started:
```bash
engram logs
# Look for: "Telegram bot started (polling)"
```

**"Access denied" reply**

Your Telegram user ID is not in `allowed_users`. Get your ID from `@userinfobot` and add it.

**"Still thinking…" for more than 3 minutes**

The orchestrator task is running but taking a long time. Check the engram logs for errors. You can also check task status with `/task status <id>` if you have the ID.

**WhatsApp messages arrive but engram does not reply**

1. Verify the webhook URL is correct: `http://your-engram-host:8766/webhook/whatsapp`
2. Check that Evolution API is delivering events: look at the Evolution API dashboard
3. Check engram logs for `WhatsApp webhook received event=`

**WhatsApp instance disconnects**

Evolution API instances can drop if WhatsApp logs out the session. Re-scan the QR code in the Evolution API dashboard. Consider setting up Evolution API with a persistent session directory.

**Long responses truncated**

If you only see the first part of a long response with no file attachment, check that your Telegram client can receive files. The full response is always sent as a `.txt` attachment for responses over 4000 characters.
