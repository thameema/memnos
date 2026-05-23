# engram for Enterprise Teams — Step-by-Step Setup

This guide walks through deploying a shared engram instance for an engineering organisation: one server, per-engineer API keys, namespace hierarchy, and onboarding workflows.

For the conceptual overview of how enterprise teams use engram — roles, knowledge flow, examples — see [enterprise-ai-engineering.md](enterprise-ai-engineering.md).

---

## What you are deploying

```
                      ┌─────────────────────────────────┐
Alice (Architect)─────►                                  │
Bob (Developer) ──────►   engram server (shared)         │
Carol (QA) ───────────►   ArcadeDB (graph + vector)      │
Dave (DevOps) ────────►   Encrypted vault                │
New Hire ─────────────►   Namespace ACL                  │
                      └─────────────────────────────────┘
                             ▲ each engineer connects via
                             │ their own API key + MCP
```

Every engineer keeps their own Claude Code installation. They connect to a shared engram server that holds the team's living knowledge graph.

---

## Step 1 — Provision the server

### Minimum specs

| Team size | RAM | CPU | Disk |
|-----------|-----|-----|------|
| 1–10 engineers | 4 GB | 2 vCPU | 50 GB SSD |
| 10–50 engineers | 8 GB | 4 vCPU | 100 GB SSD |
| 50+ engineers | 16 GB | 8 vCPU | 500 GB SSD |

ArcadeDB is the memory-hungry component. The Python engram server is lightweight.

### Supported platforms

- Any VPS (Hetzner, DigitalOcean, AWS EC2, Azure VM) running Ubuntu 22.04+ or Debian 12+
- Private network reachable by team engineers (VPN, Tailscale, or internal network)

### Install

```bash
# On the server
git clone https://github.com/thameema/engram.git
cd engram

# Generate credentials
export ARCADEDB_PASSWORD=$(openssl rand -hex 24)
export ENGRAM_API_KEY=$(openssl rand -hex 32)   # admin key — keep this private
export ENGRAM_VAULT_KEY=$(python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
export ANTHROPIC_API_KEY=sk-ant-...              # for reflection jobs and agent tasks

# Save to .env (git-ignored)
cat > .env << EOF
ARCADEDB_PASSWORD=${ARCADEDB_PASSWORD}
ENGRAM_API_KEY=${ENGRAM_API_KEY}
ENGRAM_VAULT_KEY=${ENGRAM_VAULT_KEY}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
EOF

cp engram.yaml.example engram.yaml
# Edit engram.yaml to configure team API keys (see Step 2)

docker compose up -d
docker compose logs -f engram   # wait for "MCP SSE server ready on :8765"
```

### TLS (required for non-localhost deployments)

Put Caddy or Nginx in front of ports 8765 and 8766 with a valid certificate:

```
# Caddy example
engram.yourcompany.com {
    reverse_proxy localhost:8765
}
engram-api.yourcompany.com {
    reverse_proxy localhost:8766
}
```

Engineers then use `https://engram.yourcompany.com/sse` in their `~/.claude.json`.

---

## Step 2 — Configure team namespaces and API keys

Edit `engram.yaml` on the server. Use explicit per-engineer keys with scoped namespace access:

```yaml
auth:
  api_keys:

    # ── Admin key (platform team only — never distribute) ──
    - key: "${ENGRAM_API_KEY}"
      user_id: admin
      namespaces: ["*"]

    # ── Senior Architect ──
    - key: "alice-key-change-me"
      user_id: alice
      namespaces:
        - personal:alice
        - team:architecture
        - project:*

    # ── Backend Developer ──
    - key: "bob-key-change-me"
      user_id: bob
      namespaces:
        - personal:bob
        - project:payments:*
        - team:architecture   # read-only (write requires namespace ownership)

    # ── QA Engineer ──
    - key: "carol-key-change-me"
      user_id: carol
      namespaces:
        - personal:carol
        - project:*:qa
        - project:*

    # ── DevOps / Platform ──
    - key: "dave-key-change-me"
      user_id: dave
      namespaces:
        - personal:dave
        - infra:*
        - team:architecture

    # ── New hire (read-heavy, write to personal + onboarding) ──
    - key: "newhire-key-change-me"
      user_id: newhire
      namespaces:
        - personal:newhire
        - team:onboarding
        - team:architecture   # read-only

    # ── CI/CD system (read-write to CI namespaces only) ──
    - key: "ci-key-change-me"
      user_id: ci
      namespaces:
        - infra:*
        - project:*:ci
      read_only: false

    # ── Web app / knowledge base consumer (query-only) ──
    # This key is safe to embed in a server-side web application.
    # It can call /api/v1/knowledge/ask and memory_search but cannot
    # write, delete, or access the vault.
    - key: "webapp-key-change-me"
      user_id: webapp
      namespaces:
        - team:architecture
        - team:onboarding
        - project:*
      read_only: true

namespaces:
  default: personal:default
  definitions:
    team:architecture:
      owners: [alice, admin]
      writers: [alice, dave]
      readers: ["*"]          # all keys can read architecture ADRs

    team:onboarding:
      owners: [admin]
      writers: ["*"]          # everyone contributes onboarding context
      readers: ["*"]

    project:payments:
      owners: [alice, admin]
      writers: [bob, carol, dave]
      readers: ["*"]

    infra:kubernetes:
      owners: [dave, admin]
      writers: [dave]
      readers: [alice, bob, carol]
```

> **Security note:** Generate a unique random key for each engineer (`openssl rand -hex 32`). Rotate keys when engineers leave or quarterly.

---

## Step 3 — Per-engineer client setup

Each engineer adds the following to their **`~/.claude.json`** on their local machine:

```json
{
  "mcpServers": {
    "engram": {
      "type": "sse",
      "url": "https://engram.yourcompany.com/sse",
      "headers": {
        "Authorization": "Bearer <their-personal-key>"
      }
    }
  }
}
```

Or, if engineers prefer the stdio transport with a local `engram-mcp-stdio` process that connects to the remote ArcadeDB:

```json
{
  "mcpServers": {
    "engram": {
      "type": "stdio",
      "command": "/path/to/engram-mcp-stdio",
      "env": {
        "ENGRAM_CONFIG": "/path/to/engram.yaml",
        "ARCADEDB_HOST": "engram.yourcompany.com",
        "ARCADEDB_PORT": "2480",
        "ARCADEDB_PASSWORD": "your-arcadedb-password",
        "ENGRAM_API_KEY": "their-personal-key",
        "ENGRAM_VAULT_KEY": "shared-vault-key"
      }
    }
  }
}
```

> **Note:** The stdio transport with a remote ArcadeDB requires network access to port 2480. For most teams the SSE/HTTP approach is simpler.

---

## Step 4 — Team CLAUDE.md template

Give every engineer this template for their **`~/.claude/CLAUDE.md`**. Customise the namespace table to match your team's hierarchy:

```markdown
## Memory System — engram (Team Server)

engram is connected. Use it for all memory and recall. NEVER use Bash grep or file search.

### Recall (always do this first)
Call `memory_search` before answering questions about:
- Architecture decisions, system design choices
- Technical patterns and conventions used in this codebase
- Past incidents, gotchas, or hard-won lessons
- Customer/project context

### Save (do this when something worth keeping happens)
Call `memory_write` when:
- A key technical decision is made (architecture, library choice, approach)
- You discover a non-obvious pattern, gotcha, or failure mode
- A production incident is resolved
- The user says "remember this" or "note that"

### Namespace routing
Write to the namespace that matches the *audience*, not the topic:

| Content | Namespace |
|---|---|
| My personal session notes | personal:me |
| Architecture decision records (ADRs) | team:architecture |
| Production incident runbooks | infra:incidents |
| Payment service patterns | project:payments |
| QA failure patterns | project:payments:qa |
| New hire onboarding context | team:onboarding |

Rule of thumb: who should be able to search this?
- Whole team → team:architecture or the relevant project namespace
- DevOps/platform → infra:*
- Just you → personal:me

### End of session
Write a brief session summary before closing:
  memory_write(
    content="Session [date]: <what was worked on, key decisions, discoveries>",
    namespace="<the main namespace used today>",
    tags=["session-summary"]
  )
```

---

## Step 5 — Recommended namespace hierarchy

Start simple. You can always add sub-namespaces later — but migrating existing memories to a new namespace requires a one-time query.

```
team:architecture          # ADRs, technology choices, system constraints
team:onboarding            # context for new hires
team:security              # security policies, compliance constraints
team:process               # sprint retros, process decisions

project:<name>             # general project knowledge
project:<name>:qa          # QA failure patterns, test coverage gaps
project:<name>:infra       # deployment and infrastructure specifics
project:<name>:incidents   # production incidents and resolutions

infra:kubernetes           # K8s deployment patterns and gotchas
infra:incidents            # cross-project incident runbooks
infra:ci                   # CI/CD pipeline knowledge

personal:<username>        # private per-engineer memory (never shared)
```

Namespace access uses **prefix matching**: a search against `project:payments` returns results from `project:payments`, `project:payments:qa`, `project:payments:infra`, etc.

---

## Step 6 — Build web apps on top of the knowledge base

The shared engram instance doubles as a **knowledge base API** for internal tooling — Slack bots, onboarding apps, internal docs search, or any web application that needs to answer questions from your team's accumulated knowledge.

### The pattern

```
Web app / Slack bot
    │
    ▼ POST /api/v1/knowledge/ask
engram REST API (read-only key)
    │
    ├─► semantic search over team namespaces
    └─► LLM synthesises answer from top-k memories
         └─► returns answer + sources (for citations)
```

### Example: internal Slack bot

```python
import httpx

ENGRAM_URL = "https://engram.yourcompany.com"
ENGRAM_KEY = "webapp-key-change-me"   # read-only key from Step 2

async def ask_knowledge_base(question: str, namespace: str = "team:architecture") -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{ENGRAM_URL}/api/v1/knowledge/ask",
            headers={"Authorization": f"Bearer {ENGRAM_KEY}"},
            json={"question": question, "namespace": namespace, "top_k": 6},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

# usage
result = await ask_knowledge_base("How do we handle database migrations?")
print(result["answer"])
# Sources: result["sources"] — list of {content, namespace, score}
```

### What it can and cannot do

| Capability | Read-only web app key |
|------------|----------------------|
| Ask questions (`knowledge/ask`) | ✅ Yes |
| Search memories (`memory_search`) | ✅ Yes |
| Read graph / entities | ✅ Yes |
| Write memories | ❌ No (403) |
| Delete memories | ❌ No (403) |
| Access vault secrets | ❌ No (403) |

### Runtime key management

You can create, list, and revoke API keys without restarting the server — useful for giving time-limited access to contractors or rotating keys after someone leaves.

**Via the dashboard** — open `https://engram.yourcompany.com/dashboard` and click **API Keys**.

**Via REST:**

```bash
# Create a read-only contractor key
curl -X POST https://engram.yourcompany.com/api/v1/admin/keys \
  -H "Authorization: Bearer ${ENGRAM_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "contractor-jane", "namespaces": ["project:payments"], "read_only": true}'

# The response shows the plaintext key ONCE — copy it immediately
# { "key": "eng_abc123...", "id": "uuid-...", "created_at": "..." }

# Revoke when contract ends
curl -X DELETE https://engram.yourcompany.com/api/v1/admin/keys/{id} \
  -H "Authorization: Bearer ${ENGRAM_API_KEY}"
```

Runtime keys live in `~/.engram/keys.db` on the server (SHA-256 hashed). They are checked after YAML-configured keys on every request.

---

## Step 7 — Onboarding new engineers

When a new engineer joins:

1. Generate them a key: `openssl rand -hex 32`
2. Add it to `engram.yaml` with appropriate namespace access (start with read-heavy: `team:*`, `personal:<name>`, `project:*` read, `project:<name>:*` write)
3. `docker compose restart engram` to reload the config
4. Give them the key and the `~/.claude.json` snippet from Step 3
5. Share the CLAUDE.md template from Step 4

**Onboarding task for the new engineer:**

```
"I just joined the team. Search team:architecture and team:onboarding for context
about how we build services, what technology choices have been made, and what I
should know as a new engineer."
```

Claude will call `memory_search` across the onboarding and architecture namespaces and surface institutional knowledge immediately — architecture ADRs, patterns, gotchas, and anything past engineers wrote in `team:onboarding`.

**What new hires should write:**

Have new engineers write their fresh observations into `team:onboarding`. New hire perspectives capture things long-timers have stopped noticing — confusing docs, unclear conventions, things that weren't obvious on day one. These become valuable for the next new hire.

---

## Step 8 — Team rituals

Make engram part of your regular engineering workflow:

| When | Who | What to write | Namespace |
|------|-----|---------------|-----------|
| After architecture review | Architect | Decision, alternatives considered, rationale | `team:architecture` |
| After production incident | On-call | Root cause, resolution steps, prevention | `infra:incidents` |
| During code review | Reviewer | Recurring feedback patterns, conventions to enforce | `project:<name>` |
| After debugging session | Developer | The non-obvious root cause, how to spot it again | `project:<name>` |
| After sprint retro | Scrum master | Process learnings, what to keep/change | `team:process` |
| After QA finds a bug | QA engineer | Failure pattern, edge case, regression risk | `project:<name>:qa` |
| After infra change | DevOps | Configuration gotcha, deployment constraint | `infra:kubernetes` |

A 30-second `memory_write` at the end of a task pays dividends for every team member who works in that area in the future.

---

## Backup and disaster recovery

The knowledge graph is a team asset. Back it up daily.

```bash
# Backup ArcadeDB data directory
docker compose exec arcadedb arcadectl backup --output /backup/engram-$(date +%Y%m%d).tar.gz

# Or back up the Docker volume directly
docker run --rm \
  -v engram_arcadedb-data:/data \
  -v /path/to/backups:/backup \
  alpine tar czf /backup/engram-arcadedb-$(date +%Y%m%d).tar.gz /data
```

Store backups off-machine (S3, Azure Blob, GCS). Test your restore procedure monthly.

---

## Access control review

- Review API key permissions **quarterly**
- Remove keys for team members who have left (immediately on departure)
- Rotate keys **annually**
- Keep the admin key (`namespaces: ["*"]`) only with the platform team lead
- Enable vault audit log (`vault.audit_log: true` in `engram.yaml`) and review it monthly

---

## What NOT to put in the knowledge graph

The knowledge graph is for engineering decisions, patterns, and context — not for sensitive data:

| Write this | Never write this |
|------------|-----------------|
| Architecture decision records | API keys or passwords (use the vault instead) |
| Technical patterns and conventions | Customer PII or personal data |
| Production incident runbooks | Proprietary business data or pricing |
| Codebase gotchas and footguns | Security vulnerabilities with exploit details |
| Test failure patterns | Credentials or secrets of any kind |

engram will automatically detect and redact credential patterns (API keys, JWTs, AWS keys) from `memory_write` calls, but this is a safety net — not a substitute for team hygiene.

---

## Troubleshooting

**`/mcp` shows disconnected for some engineers but not others**
- Check their API key is in `engram.yaml` and the server was restarted after the change
- Check TLS certificate is valid: `curl -v https://engram.yourcompany.com/sse`
- Check firewall rules allow the engineer's IP to reach port 8765

**`memory_search` returns results for namespace A but not B**
- Verify the engineer's key includes namespace B in their `namespaces` list
- Check the search used the correct namespace string (exact prefix, case-sensitive)

**Knowledge graph is growing slowly**
- Remind the team to use `memory_write` after key moments (see Step 7)
- Add to each project CLAUDE.md: "After completing a task, write what you learned to `project:<name>`"

**New hire can't find any knowledge**
- Confirm they searched `team:onboarding` and `team:architecture` (not just `personal:<name>`)
- Verify their key has read access to those namespaces
