# Connecting Claude Code to engram

This guide covers the full setup: registering engram as a Claude Code MCP server, and configuring `CLAUDE.md` so Claude actually uses it.

## Prerequisites

- engram installed and ArcadeDB running (see [quickstart.md](quickstart.md))
- Claude Code v2.0+ (CLI or desktop app)

---

## Step 1 — Register the MCP server

Claude Code reads MCP server configuration from **`~/.claude.json`** (not `~/.claude/settings.json`).

Two transport modes are available. Choose one:

### Transport A — stdio (recommended for local use)

The stdio transport is simpler: Claude Code spawns `engram-mcp-stdio` as a subprocess. No HTTP server needs to be running separately; ArcadeDB must still be accessible.

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

Find the binary path: `which engram-mcp-stdio`

> **Important:** Use **absolute paths** for `command` and `ENGRAM_CONFIG`. Claude Code spawns the process from a different working directory; relative paths will fail silently.

> **Startup time:** On first invocation, `engram-mcp-stdio` loads the sentence-transformers embedding model (~25–40s). Claude Code's stdio timeout is 60s by default — if your machine is slow, consider setting `ENGRAM_LOG_LEVEL=WARNING` to reduce I/O during startup.

### Transport B — SSE (HTTP, for shared/remote servers)

Requires `engram-server` to be running (see [quickstart.md](quickstart.md)). Best for team deployments where all engineers share one server.

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

For a team server, replace `localhost:8765` with your shared server URL.

---

After adding either entry: **fully restart Claude Code** (quit and reopen), then run:

```
/mcp
```

You should see `engram  ✓ connected  · 18 tools`.

---

## Step 2 — Add CLAUDE.md instructions

Registering the MCP server makes the tools *available*, but Claude won't use them automatically without instructions. Add the following to **`~/.claude/CLAUDE.md`**:

```markdown
## Memory System — engram MCP

engram is connected as an MCP server. Use it for all memory and recall operations.

### Recall (MANDATORY — do this before answering from training data)
ALWAYS call `memory_search` first when the user asks about:
- Past decisions, architecture choices, or context
- Customer or project specifics
- Anything the user has previously asked you to remember

Never use Bash grep, file search, or Obsidian MCP to recall knowledge.
The MCP result comes back in plain text — read it directly, do not spawn agents or run scripts.

### Save (do this when something worth keeping happens)
Call `memory_write` when:
- A significant technical decision is made
- The user says "remember this" or "note that"
- A meeting or call produces actionable context
- You discover something non-obvious about a codebase or system

### End of session
When the user wraps up or says goodbye, write a session summary:
`memory_write(content="Session [date]: ...", namespace="personal:me", tags=["session"])`

### Namespace guide
| Content type          | Namespace              |
|-----------------------|------------------------|
| Personal notes        | personal:me            |
| Shared team knowledge | org:myteam             |
| Project-specific      | project:myproject      |
| Customer context      | org:myteam:customers:customername |

### Tool reference
| Tool | When to use |
|---|---|
| `memory_search(query, namespace)` | Recall — always try this first |
| `memory_write(content, namespace, tags)` | Save a decision or learning |
| `memory_delete(memory_id, namespace)` | Remove an outdated entry |
| `secret_set(key_name, value, namespace)` | Store a credential in the encrypted vault |
| `secret_get(key_name, namespace)` | Retrieve a vault secret |
```

---

## Step 3 — Verify end-to-end

In a fresh Claude Code session:

```
What do you remember about the auth service?
```

Claude should call `memory_search` (visible in the tool-use output). If it runs Bash or searches files instead, the CLAUDE.md changes aren't loaded — restart Claude Code or run `Read ~/.claude/CLAUDE.md`.

To write a memory:

```
Remember that we use JWT with 24h expiry for the auth service
```

Claude should call `memory_write`. Verify at `http://localhost:8766/dashboard`.

The dashboard has two main tabs:
- **Memory Graph** — interactive force-directed graph of all memories and their relationships
- **API Keys** — create, list, and revoke runtime API keys without restarting the server

---

## What is and isn't automatic

| Behaviour | Automatic? | How |
|---|---|---|
| Recall when asked about past context | ✅ Yes (with CLAUDE.md) | Claude calls `memory_search` |
| Saving key decisions | ✅ Yes (with CLAUDE.md) | Claude calls `memory_write` |
| Full conversation transcript | ❌ No | Claude summarises — it doesn't record every message |
| Knowledge graph entity extraction | ✅ Yes (spaCy, no LLM needed) | Runs on every `memory_write` |
| Cross-session persistence | ✅ Yes | Stored in ArcadeDB on disk |
| Credential auto-redaction | ✅ Yes | Detected and redacted before any write reaches ArcadeDB |

---

## Zero-touch automation (optional but recommended)

The MCP approach above requires Claude to actively call `memory_write`. If Claude skips it, forgets, or a session ends abruptly, memories are lost.

**Zero-touch automation** wires engram directly into Claude Code's lifecycle via shell hooks, so memories are written and injected automatically — no agent action required.

Three hooks work together:

| Hook | When it fires | What it does |
|------|--------------|--------------|
| **UserPromptSubmit** | Every Claude Code prompt | Queries engram and injects relevant past context |
| **Stop** | Every turn end | Writes a session-state memory (branch, recent commits, CWD) |
| **git post-commit** | Every `git commit` | Writes the commit to engram (conventional prefix → memory type) |

### Install the hooks

**Step 1 — Create the hook scripts**

```bash
mkdir -p ~/.claude/hooks
```

Create `~/.claude/hooks/engram-inject.sh`:

```bash
#!/usr/bin/env bash
# UserPromptSubmit hook — injects engram context on every Claude Code prompt

set -euo pipefail

ENGRAM_API="http://localhost:8766"    # or your remote server
ENGRAM_KEY="your-engram-api-key"
ENGRAM_NS="project:myproject"         # adjust to your namespace
ENGRAM_TOP_K=5

INPUT=$(cat)
CWD=$(echo "$INPUT"    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")
PROMPT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('prompt',''))" 2>/dev/null || echo "")

if ! curl -sf --max-time 2 "$ENGRAM_API/api/v1/admin/health" -o /dev/null 2>/dev/null; then
  exit 0  # engram not running — fail silently
fi

QUERY=$(echo "$PROMPT" | head -c 200 | python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip()))" 2>/dev/null || echo "")
[[ -z "$QUERY" ]] && exit 0

RESPONSE=$(curl -sf --max-time 5 \
  "$ENGRAM_API/api/v1/memory/search?q=$QUERY&ns=$ENGRAM_NS&top_k=$ENGRAM_TOP_K" \
  -H "X-API-Key: $ENGRAM_KEY" 2>/dev/null || echo "[]")

CONTEXT=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
results = data if isinstance(data, list) else data.get('results', [])
if not results:
    sys.exit(0)
lines = ['[engram: relevant past context]']
for r in results:
    mem = r.get('memory', r)
    mtype = mem.get('memory_type', 'fact')
    content = mem.get('content', '').strip()
    score = r.get('score', '')
    score_str = f'  (similarity: {score:.2f})' if isinstance(score, float) else ''
    if content:
        lines.append(f'[{mtype}]{score_str} {content[:280]}')
if len(lines) <= 1:
    sys.exit(0)
print('\n'.join(lines))
" 2>/dev/null || echo "")

[[ -z "$CONTEXT" ]] && exit 0

python3 -c "
import json, sys
print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': sys.argv[1]}}))
" "$CONTEXT"
```

Create `~/.claude/hooks/engram-session-write.sh`:

```bash
#!/usr/bin/env bash
# Stop hook — writes session state to engram after every Claude Code turn

set -euo pipefail

ENGRAM_API="http://localhost:8766"
ENGRAM_KEY="your-engram-api-key"
ENGRAM_NS="project:myproject"

INPUT=$(cat)
CWD=$(echo "$INPUT"        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || echo "")

[[ -z "$CWD" ]] && exit 0

if ! curl -sf --max-time 2 "$ENGRAM_API/api/v1/admin/health" -o /dev/null 2>/dev/null; then
  exit 0
fi

PROJECT=$(basename "$CWD")
BRANCH="" RECENT_COMMITS="" UNCOMMITTED=0

if git -C "$CWD" rev-parse --git-dir >/dev/null 2>&1; then
  BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
  RECENT_COMMITS=$(git -C "$CWD" log --oneline -5 --no-decorate 2>/dev/null || echo "")
  UNCOMMITTED=$(git -C "$CWD" status --short 2>/dev/null | wc -l | tr -d ' ')
fi

if [[ -n "$RECENT_COMMITS" ]]; then
  CONTENT="session ended | project: $PROJECT | dir: $CWD${BRANCH:+ | branch: $BRANCH} | uncommitted: $UNCOMMITTED
Recent commits:
$RECENT_COMMITS"
else
  CONTENT="session ended | project: $PROJECT | dir: $CWD"
fi

PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'content': sys.argv[1], 'namespace': sys.argv[2], 'memory_type': 'fact',
    'tags': ['session-log', 'auto', sys.argv[3]],
    'metadata': {'session_id': sys.argv[4], 'project': sys.argv[3], 'source': 'claude-code-stop-hook'}
}))
" "$CONTENT" "$ENGRAM_NS" "$PROJECT" "$SESSION_ID" 2>/dev/null)

curl -sf --max-time 5 -X POST "$ENGRAM_API/api/v1/memory/" \
  -H "Content-Type: application/json" -H "X-API-Key: $ENGRAM_KEY" \
  -d "$PAYLOAD" -o /dev/null 2>/dev/null || true

exit 0
```

Make them executable:

```bash
chmod +x ~/.claude/hooks/engram-inject.sh
chmod +x ~/.claude/hooks/engram-session-write.sh
```

**Step 2 — Register in `~/.claude/settings.json`**

Add the `hooks` block (merge with any existing entries):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/yourname/.claude/hooks/engram-inject.sh",
            "timeout": 8
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/yourname/.claude/hooks/engram-session-write.sh",
            "timeout": 8,
            "async": true
          }
        ]
      }
    ]
  }
}
```

> Use **absolute paths** for `command`. Relative paths fail silently.  
> `async: true` on the Stop hook means it runs after Claude responds — the user never waits for it.

**Step 3 — Global git post-commit hook**

This writes every commit in every repo to engram automatically.

```bash
mkdir -p ~/.git-hooks
```

Create `~/.git-hooks/post-commit`:

```bash
#!/usr/bin/env bash
# Global post-commit — writes every git commit to engram
# Memory type: feat/refactor → decision | fix → incident | others → fact

set -euo pipefail

ENGRAM_API="http://localhost:8766"
ENGRAM_KEY="your-engram-api-key"
ENGRAM_NS="project:myproject"

if ! curl -sf --max-time 2 "$ENGRAM_API/api/v1/admin/health" -o /dev/null 2>/dev/null; then
  exit 0
fi

REPO_NAME=$(basename "$(git rev-parse --show-toplevel 2>/dev/null || echo "unknown")")
COMMIT_HASH=$(git rev-parse --short HEAD)
COMMIT_FULL=$(git rev-parse HEAD)
COMMIT_MSG=$(git log -1 --pretty=%B | head -5)
COMMIT_AUTHOR=$(git log -1 --pretty=%an)
CHANGED_FILES=$(git diff-tree --no-commit-id -r --name-only HEAD | head -20 | tr '\n' ' ')
BRANCH=$(git rev-parse --abbrev-ref HEAD)

MEMORY_TYPE="fact"
if echo "$COMMIT_MSG" | grep -qiE '^(feat|feature|refactor|arch):'; then
  MEMORY_TYPE="decision"
elif echo "$COMMIT_MSG" | grep -qiE '^(fix|hotfix|bug):'; then
  MEMORY_TYPE="incident"
fi

CONTENT="[engram-commit] $COMMIT_MSG
repo: $REPO_NAME | commit: $COMMIT_HASH | branch: $BRANCH | author: $COMMIT_AUTHOR
files: $CHANGED_FILES"

PAYLOAD=$(python3 -c "
import json, sys
msg, ns, mtype, commit, branch, author, files, repo = sys.argv[1:9]
print(json.dumps({
    'content': msg, 'namespace': ns, 'memory_type': mtype, 'author': author,
    'tags': ['git-commit', 'auto', repo, mtype],
    'metadata': {'commit_hash': commit, 'repo': repo, 'branch': branch, 'source': 'post-commit-hook'}
}))
" "$CONTENT" "$ENGRAM_NS" "$MEMORY_TYPE" "$COMMIT_FULL" "$BRANCH" "$COMMIT_AUTHOR" "$CHANGED_FILES" "$REPO_NAME" 2>/dev/null)

curl -sf --max-time 5 -X POST "$ENGRAM_API/api/v1/memory/" \
  -H "Content-Type: application/json" -H "X-API-Key: $ENGRAM_KEY" \
  -d "$PAYLOAD" -o /dev/null 2>/dev/null || true

# Delegate to per-repo override if present
LOCAL_HOOK="$(git rev-parse --git-dir 2>/dev/null)/hooks/post-commit.local"
if [[ -x "$LOCAL_HOOK" ]]; then
  exec "$LOCAL_HOOK" "$@"
fi

exit 0
```

```bash
chmod +x ~/.git-hooks/post-commit
git config --global core.hooksPath ~/.git-hooks
```

> **Per-repo overrides:** name your repo-specific post-commit script `post-commit.local` (not `post-commit`) and the global hook will call it after writing to engram.

### Namespace routing across projects

If you work in multiple contexts (e.g. separate namespaces for different teams or security levels), add a routing function to both hooks:

```bash
get_namespace() {
    local cwd="$1"
    case "$cwd" in
        *"/work/projectA"*)  echo "project:projectA" ;;
        *"/work/projectB"*)  echo "project:projectB" ;;
        *)                   echo "personal:me" ;;
    esac
}

ENGRAM_NS=$(get_namespace "$CWD")
```

### What each hook writes

**inject hook** (reads): surfaces the 5 most semantically similar past memories as context before every Claude response. Claude sees them as `[engram: relevant past context]` at the top of its input — no extra tool calls needed.

**session hook** (writes): after every turn, records the current branch, recent commits, and CWD so the next session can orient itself without reading git history.

**commit hook** (writes): every `git commit` produces an engram memory tagged `git-commit`. `feat:`/`refactor:` commits become `decision` type; `fix:` commits become `incident` type. These surface automatically when you later ask "why did we change X?" or "what was the fix for Y?".

### Updated automation table

| Behaviour | Automatic? | How |
|---|---|---|
| Recall when asked about past context | ✅ Yes (with CLAUDE.md) | Claude calls `memory_search` |
| Context injected before every prompt | ✅ Yes (with inject hook) | UserPromptSubmit hook |
| Session state written after every turn | ✅ Yes (with stop hook) | Stop hook (async) |
| Every git commit recorded | ✅ Yes (with git hook) | `core.hooksPath` global hook |
| Saving key decisions | ✅ Yes (with CLAUDE.md) | Claude calls `memory_write` |
| Full conversation transcript | ❌ No | Claude summarises — it doesn't record every message |
| Knowledge graph entity extraction | ✅ Yes (spaCy, no LLM needed) | Runs on every `memory_write` |
| Cross-session persistence | ✅ Yes | Stored in ArcadeDB on disk |
| Credential auto-redaction | ✅ Yes | Detected and redacted before any write reaches ArcadeDB |

---

## Team setup — shared server with per-engineer API keys

If engram runs on a shared server:

```json
{
  "mcpServers": {
    "engram": {
      "type": "sse",
      "url": "https://engram.yourcompany.com/sse",
      "headers": {
        "Authorization": "Bearer alice-personal-key"
      }
    }
  }
}
```

Each engineer uses their own API key. Keys are scoped to namespaces in `engram.yaml`:

```yaml
auth:
  api_keys:
    - key: "alice-personal-key"
      user_id: "alice"
      namespaces: ["personal:alice", "team:architecture", "project:*"]
    - key: "bob-personal-key"
      user_id: "bob"
      namespaces: ["personal:bob", "project:payments:*"]
```

See [enterprise-team-setup.md](enterprise-team-setup.md) for the full team deployment guide.

---

## Troubleshooting

**`/mcp` shows engram as disconnected**
- SSE: Is the server running? `curl http://localhost:8765/health`
- stdio: Does the binary exist and run? `ENGRAM_CONFIG=/path/to/engram.yaml ARCADEDB_PASSWORD=... engram-mcp-stdio`
- Did you fully restart Claude Code (quit, not just new tab)?
- Are all env vars set correctly? Check `ENGRAM_CONFIG` is an absolute path.

**Claude uses Bash/grep instead of memory_search**
- CLAUDE.md isn't loaded. Run `Read ~/.claude/CLAUDE.md` at session start.
- Or move the engram instructions to the project-level `CLAUDE.md` in your working directory.

**memory_search returns no results**
- Check namespace: `org:myteam` won't find results stored under `personal:me`
- Use `namespace="all"` to search everything: `memory_search(query="...", namespace="all")`
- Verify memories exist: `curl http://localhost:8766/api/v1/graph/stats?namespace=all -H "Authorization: Bearer your-key"`

**stdio takes too long to start**
- First start downloads/loads the `all-MiniLM-L6-v2` model (~90MB). Allow 40–60s.
- Subsequent starts are faster once the model is cached (`~/.cache/huggingface/`).
- Set `ENGRAM_LOG_LEVEL=WARNING` in the env block to reduce logging overhead.

**Claude spawns agents or runs scripts to parse results**
- Add to CLAUDE.md: `"MCP results come back as plain text — read them directly, never spawn agents to parse tool results."`
