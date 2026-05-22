# engram for Enterprise AI Engineering Teams

## The problem this document addresses

AI coding assistants — Claude Code, Cursor, GitHub Copilot — are powerful individually but blind collectively. Each session starts from zero. Each engineer has their own context. When a senior architect makes a critical design decision at 10am, the junior developer asking Claude Code the same question at 2pm gets a different answer. Nobody learns from each other's AI-assisted discoveries. The team gets ten times the output of one engineer, but not one tenth the collective intelligence of ten engineers.

This document explains how engram changes that — and how it compares to the most common current workaround, which is using Obsidian or Markdown vaults as a manual memory layer.

---

## How teams use Obsidian with Claude Code today

Obsidian has become the de facto "external brain" for AI-assisted engineering. The common pattern:

1. Engineers write notes in Obsidian (architecture decisions, patterns, conventions)
2. They keep a `CLAUDE.md` file at the root of their Obsidian vault or project
3. Claude Code reads `CLAUDE.md` at session start, loading context manually curated by the engineer
4. When Claude Code discovers something new, the engineer manually copies it into a note

This works. It is better than nothing. But it has fundamental limitations at team scale that no amount of vault organisation fixes:

| Limitation | What it means in practice |
|------------|--------------------------|
| **Manual curation only** | Engineers must decide what to save and manually write it. Things discovered by AI assistants — successful patterns, failure modes, debugging approaches — are lost unless a human intervenes |
| **No semantic retrieval** | Claude Code reads your `CLAUDE.md` file from top to bottom. It does not search your vault for the most relevant context for a given task. You get whatever you put in the file, in order |
| **No temporal understanding** | Obsidian does not know that the auth approach from March was superseded by a new decision in June. Everything is a flat timestamp, and Claude Code has no way to know which facts are current |
| **No real-time team sharing** | Vault sync via Git or iCloud is asynchronous. When a developer solves a problem at 3pm, no team member's Claude Code session knows about it until they pull the vault |
| **No programmatic write access** | AI agents cannot write to Obsidian. The memory is read-only from the AI's perspective — you are always the bottleneck |
| **No cross-session learning** | Obsidian does not observe what worked and what failed. It stores what you tell it, not what the AI learned |
| **No access control** | Either everyone can see everything in the vault, or you manage complex folder structures. There is no concept of "this namespace is visible to the backend team but not the mobile team" |
| **No agent coordination** | Obsidian is passive storage. It cannot trigger agents, route tasks, or coordinate work across team members |

---

## engram vs Obsidian: direct comparison

| Capability | Obsidian + CLAUDE.md | engram |
|------------|---------------------|--------|
| **Memory persistence** | Manual — engineer writes notes | Automatic — every session writes to memory |
| **Retrieval** | File read at session start | Semantic + graph search, most relevant context surfaced automatically |
| **Temporal knowledge** | Flat — no concept of superseded facts | Temporal graph — knows which decisions are current vs historical |
| **Team sharing** | Async via Git/iCloud sync | Real-time — all engineers share the same live knowledge graph |
| **AI write access** | No — read-only from AI perspective | Yes — agents write, update, and cross-reference memories directly |
| **Learning from failures** | No | Nightly reflection distils failures into heuristics injected into future sessions |
| **Access control** | Folder structure only | Namespace ACL per API key — fine-grained per team/project/role |
| **Multi-agent** | No | Yes — spawn parallel workers, collect results, share context |
| **Search from phone** | No | Yes — Telegram/WhatsApp gateway |
| **API access** | No | Yes — REST API + MCP tools |
| **Self-improving** | No | Yes — heuristics, skill templates, episodic replay |

### The fundamental difference in mental model

Obsidian is a **notebook you manage**. You are the librarian. You decide what gets stored, how it is organised, and what gets loaded into context.

engram is a **living knowledge graph the team builds together**. The AI is also a contributor. Every engineering decision, every debugging session, every architectural choice is captured automatically, cross-referenced, and made available to every engineer and every AI session without anyone having to curate it.

---

## The enterprise AI engineering model

Consider a software engineering team of 20 people. Without shared AI memory, you have 20 parallel AI sessions with no connection between them. The architect's context never reaches the developer. The QA engineer's test failure patterns never surface in the developer's next session. The DevOps team's infrastructure discoveries are invisible to everyone else.

With engram, the team operates as a single connected knowledge organism:

```
                        engram knowledge graph
                        ┌─────────────────────────────────┐
Architect ─────────────►│  architecture:decisions          │◄──────── reads
Developer ─────────────►│  project:payments:patterns       │◄──────── reads
QA Engineer ───────────►│  project:payments:failures       │◄──────── reads
DevOps ────────────────►│  infra:kubernetes:config         │◄──────── reads
Product Manager ───────►│  product:requirements            │◄──────── reads
New hire ──────────────►│  team:onboarding:context         │◄──────── reads
                        └─────────────────────────────────┘
                                       ▲
                            All roles write here.
                            All roles read from here.
                     New hires get institutional knowledge on day one.
```

Every role is both a contributor and a consumer of the shared knowledge graph. When one person discovers something, everyone benefits immediately.

---

## How each role uses engram

### Architects

**What they write:**
- Architecture decision records (ADRs) — the decision, the alternatives considered, and the reasoning
- System constraints — "this service must be stateless because we deploy to spot instances"
- Interface contracts — "the payment service API must never break backward compatibility without a major version bump"
- Technology choices and rationale — "we use Qdrant over Pinecone because we need on-premise deployment"

**What they get back:**
- When Claude Code works on any component in the `architecture:*` namespace, it automatically surfaces the relevant ADRs and constraints without the developer having to find them
- Reflection jobs surface when implementation decisions drift from architectural intent
- Cross-references show which implementation decisions were influenced by which architectural choices

**Example:**
```
Architect writes via Claude Code:
  memory_write: "We chose event sourcing over CRUD for the audit service because
                 the compliance team requires full replay capability. All writes
                 must go through the event store. Direct DB writes are forbidden."
  namespace: "architecture:audit-service"
  tags: ["decision", "event-sourcing", "compliance"]

Developer, six weeks later, asks Claude Code:
  "Can I add a direct UPDATE to the audit_events table for performance?"

Claude Code calls memory_search("audit service database writes")
→ surfaces the ADR automatically
→ explains why direct writes are forbidden without the developer needing to find the doc
```

### Developers

**What they write:**
- Implementation patterns that worked — "this is the right way to handle pagination in this codebase"
- Gotchas and footguns — "the payment gateway times out after 8s but our default HTTP client timeout is 30s — always override it"
- Module-specific context — "the legacy order service has a race condition in the refund path; until TICKET-4521 is resolved, use distributed lock X"
- Code review feedback that gets repeated — patterns that reviewers always flag

**What they get back:**
- When starting work on a module, Claude Code automatically surfaces the relevant patterns, gotchas, and historical decisions from previous sessions
- Heuristics from nightly reflection inject learned lessons — "when working on the payment service, always check for the 8-second timeout issue"
- Cross-team discoveries surface automatically — QA failures become developer warnings

**Example:**
```
Developer A writes via Claude Code after a painful debugging session:
  memory_write: "The user service's findByEmail() does a full table scan if the
                 email domain index is stale. Call refreshEmailIndex() before any
                 bulk email query or you'll time out in production."
  namespace: "project:user-service"
  tags: ["performance", "gotcha", "email"]

Developer B, on a completely different feature, asks Claude Code:
  "Write a batch job to send emails to users who haven't logged in for 90 days"

Claude Code calls memory_search("user service email query")
→ surfaces Developer A's note automatically
→ generates code that calls refreshEmailIndex() first, avoiding the production timeout
```

### QA Engineers

**What they write:**
- Failure patterns — "the checkout flow fails intermittently when two sessions hit the same coupon code concurrently"
- Test coverage gaps — "the refund edge case for partial subscription cancellations has never been tested"
- Regression triggers — "any change to the session token format breaks the mobile app — always run the mobile auth suite"
- Environment-specific issues — "the staging database has a different timezone config than production; date-dependent tests fail on staging"

**What they get back:**
- When a developer is implementing a feature, Claude Code surfaces QA-written failure patterns and coverage gaps proactively
- When QA writes a new test, Claude Code can search for related past failures and suggest edge cases automatically
- Regression triggers surface automatically when a developer touches a risky area

**Example:**
```
QA writes via Claude Code after a production incident:
  memory_write: "The subscription cancellation endpoint silently fails if the
                 billing cycle has already been invoiced for the current period.
                 It returns HTTP 200 but does not cancel. We found this in the
                 2026-03 incident. Always test with already-invoiced accounts."
  namespace: "project:billing:failures"
  tags: ["incident", "cancellation", "silent-failure"]

Developer implementing a new proration feature asks Claude Code:
  "Write the subscription cancellation logic for mid-cycle downgrades"

Claude Code calls memory_search("subscription cancellation billing")
→ surfaces the QA note about silent failures
→ adds explicit verification that the cancellation actually processed
→ suggests a test case with an already-invoiced account
```

### DevOps and Platform Engineers

**What they write:**
- Infrastructure gotchas — "EKS node autoscaling takes 4-6 minutes; health check timeouts in the deployment config must be at least 7 minutes"
- Runbooks — the actual steps that worked to resolve a given class of outage
- Configuration discoveries — "the Azure SQL connection pool exhaustion issue was caused by a missing `Pooling=true` in the connection string, not a code leak"
- Security constraints — "all new services must run as non-root; the admission controller will reject pods with UID 0"

**What they get back:**
- When developers are writing deployment configs, Claude Code surfaces DevOps-written constraints automatically
- When an on-call engineer responds to an incident, Claude Code surfaces the relevant runbook memories from past incidents
- Infrastructure constraints propagate to all developers without requiring documentation updates

**Example:**
```
DevOps writes via Claude Code after fixing a deployment issue:
  memory_write: "Always set terminationGracePeriodSeconds to 90 for the API pods.
                 The default 30s is not enough for in-flight requests to drain.
                 We had 5xx errors during rolling deploys until we fixed this."
  namespace: "infra:kubernetes:api-pods"
  tags: ["kubernetes", "deployment", "graceful-shutdown"]

Developer writing a new service's Helm chart asks Claude Code:
  "Write the Kubernetes deployment manifest for the new notification service"

Claude Code calls memory_search("kubernetes api deployment graceful")
→ surfaces the DevOps note
→ generates the manifest with terminationGracePeriodSeconds: 90
→ the production deployment issue never happens for this service
```

### Product Managers

**What they write:**
- Requirement decisions — "the export feature must support CSV and Excel; PDF was considered and rejected because enterprise customers use Excel macros"
- Customer constraints — "customer X cannot accept any breaking changes to the API before their Q3 integration is complete"
- Feature context — "the reason the dashboard loads slowly is known; it is a deliberate trade-off while we wait for the data pipeline migration"

**What they get back:**
- When engineers are making implementation choices, product context surfaces automatically — they know why a constraint exists, not just that it exists
- When PMs are writing requirements, Claude Code can surface related past decisions and flag potential conflicts

### New Hires

**What they get on day one:**
- All of the above — automatically

A new engineer joins the team. They clone the repo, set up engram, and connect to the shared engram server. Their Claude Code sessions immediately have access to:
- Every architectural decision ever recorded
- Every production incident and its resolution
- Every gotcha, footgun, and hard-won lesson
- Every pattern that the team has validated as the right approach

They do not need to shadow a senior engineer for three months to absorb institutional knowledge. The knowledge graph is the institutional memory.

---

## Knowledge flow across the team

Without shared AI memory, knowledge flows like this — slowly, manually, and incompletely:

```
Architect ──► Architecture doc (written by human, read by human)
                        │
                        ▼ (weeks later, in a code review)
             Developer discovers constraint was violated
                        │
                        ▼ (incident post-mortem, if it makes it that far)
             QA adds a regression test
                        │
                        ▼ (never, or in a wiki nobody reads)
             New hire learns about the constraint
```

With engram, knowledge flows in real time:

```
Architect writes ADR → immediately searchable by all team members' Claude Code sessions

Developer discovers a gotcha → immediately available to all other developers,
                               QA engineers, and new hires

QA finds a failure pattern → immediately surfaces in developers' sessions when
                             they touch the affected code

DevOps documents a runbook → immediately available to on-call engineers in the
                             middle of an incident

All of the above happens without any human routing the knowledge.
```

---

## Namespace architecture for enterprise teams

engram namespaces are hierarchical strings separated by colons. Design them to match your org structure:

```yaml
# Suggested namespace hierarchy for an engineering org

# Global / cross-team
team:platform                # platform team knowledge
team:security                # security constraints and policies
team:architecture            # ADRs, system design decisions

# Project-level
project:payments             # payments domain (all teams)
project:payments:backend     # payments backend specifics
project:payments:mobile      # payments mobile specifics
project:payments:qa          # QA failure patterns for payments
project:payments:infra       # infrastructure specifics for payments

# Role-scoped
personal:alice               # Alice's private memory (only her key can write/read)
personal:bob                 # Bob's private memory

# Onboarding
team:onboarding              # knowledge useful for new hires
```

In `engram.yaml`, API keys map to namespaces:

```yaml
auth:
  api_keys:
    # Senior architect — can write to architecture namespace
    - key: "alice-key"
      user_id: "alice"
      namespaces: ["personal:alice", "team:architecture", "project:*"]

    # Backend developer — can write to project namespaces, read architecture
    - key: "bob-key"
      user_id: "bob"
      namespaces: ["personal:bob", "project:payments:*", "team:architecture"]

    # QA engineer — write access to qa namespaces, read access to everything
    - key: "carol-key"
      user_id: "carol"
      namespaces: ["personal:carol", "project:*:qa", "project:*"]

    # New hire — read-only access to team namespaces
    - key: "david-key"
      user_id: "david"
      namespaces: ["personal:david", "team:onboarding", "team:architecture"]

    # CI/CD system — write to infra namespaces
    - key: "ci-key"
      user_id: "ci"
      namespaces: ["infra:*"]
```

This gives you fine-grained control: a contractor working on one project sees only that project's namespace. A new hire gets read access to onboarding and architecture knowledge but cannot modify shared namespaces until they are onboarded.

---

## Practical setup for an AI engineering team

### Step 1 — Shared engram server

Run a single engram server accessible to the whole team. See [remote-deployment.md](remote-deployment.md) for VPS and Tailscale options.

For a small team (under 20 engineers), a single VPS with 4 GB RAM is sufficient. Neo4j and Qdrant are the memory-hungry components; the engram Python server itself is lightweight.

### Step 2 — Per-engineer API keys

Give each engineer their own API key with appropriate namespace access. They add it to their own `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "engram": {
      "type": "sse",
      "url": "http://your-engram-server:8765/sse",
      "headers": {
        "Authorization": "Bearer <their-personal-key>"
      }
    }
  }
}
```

### Step 3 — Establish namespace conventions

Agree on the namespace hierarchy before engineers start using it. Once knowledge is written under a namespace, moving it requires a migration. A simple hierarchy is better than a perfect one.

### Step 4 — Onboarding new engineers

When a new engineer joins:
1. Give them a read-only key scoped to `team:onboarding` and `team:architecture`
2. Their first task: use `memory_search` to explore the knowledge graph and understand the codebase
3. After their first week, upgrade their key to include write access to their project namespace
4. Have them write their onboarding discoveries into `team:onboarding` — their fresh perspective captures things long-timers have stopped noticing

### Step 5 — Team rituals

Make engram part of your engineering workflow:

- **After architecture decisions:** architect writes the ADR to `team:architecture` via `memory_write`
- **After production incidents:** on-call writes the root cause and resolution to `infra:incidents`
- **During code review:** reviewers write recurring feedback patterns to the project namespace so future Claude Code sessions catch them automatically
- **After debugging sessions:** developers write hard-won discoveries to the project namespace
- **Sprint retrospectives:** PM or scrum master writes the team's process learnings to `team:process`

---

## What this looks like in practice

Here is a week in the life of an AI-assisted engineering team using engram.

**Monday morning — architect plans a new microservice:**
```
Architect → Claude Code:
  "Design the interface contract for the new notification service.
   It needs to decouple the sending mechanism from the business logic."

Claude Code calls memory_search("notification service interface design")
→ finds a prior ADR about event-driven decoupling
→ finds a DevOps note about the message broker they already have licensed
→ generates a contract that is consistent with existing architecture

Architect writes the final decision:
  memory_write("Notification service uses CloudEvents schema over NATS.
               The sender (email/SMS/push) is determined at delivery time,
               not at emit time. Never hardcode delivery type in emitters.")
  namespace: "team:architecture"
```

**Tuesday — developer implements the service:**
```
Developer → Claude Code:
  "Implement the notification service emitter for the order confirmation flow"

Claude Code calls memory_search("notification service emit order")
→ surfaces Monday's ADR automatically
→ generates code using CloudEvents + NATS, not hardcoded email calls
→ the implementation is consistent with architecture on the first try
```

**Wednesday — QA finds an edge case:**
```
QA → Claude Code:
  "Write integration tests for the notification service"

Claude Code calls memory_search("notification service test edge cases")
→ finds nothing specific yet

QA discovers that notifications duplicate when the order service retries
QA writes:
  memory_write("Notification service must be idempotent on event ID.
               We saw duplicate sends when order service retried on timeout.
               Test: send the same CloudEvent ID twice; expect one delivery.")
  namespace: "project:notifications:qa"
```

**Thursday — different developer adds a new notification type:**
```
Developer B → Claude Code:
  "Add a delivery failure notification to the notification service"

Claude Code calls memory_search("notification service")
→ surfaces the architecture ADR (CloudEvents + NATS)
→ surfaces QA's idempotency discovery
→ generates code that includes idempotency key handling from the start
→ QA's discovery prevents a bug the developer didn't know to look for
```

**Friday — new hire joins:**
```
New hire → Claude Code:
  "I just joined the team. What do I need to know about how we build services here?"

Claude Code calls memory_search("architecture conventions patterns standards",
                                namespace="team:architecture")
→ surfaces all ADRs, technology choices, and patterns written this week
→ and everything written by the team before this week
→ new hire gets institutional knowledge in minutes, not months
```

---

## Governance and security considerations

### What goes in the knowledge graph

The knowledge graph is for engineering decisions, patterns, discoveries, and context — not for credentials, PII, or customer data. Establish a team norm:

- **Write:** architecture decisions, patterns, gotchas, failure modes, runbooks, conventions
- **Never write:** API keys, passwords, customer data, PII, proprietary business data

### Backup and disaster recovery

The knowledge graph is a team asset. Back it up. See [remote-deployment.md](remote-deployment.md) for backup scripts. Run daily backups to a separate storage location. Test your restore procedure before you need it.

### Access control review

Review API key permissions quarterly. Remove keys for team members who have left. Rotate keys annually. Keep the admin key (`namespaces: ["*"]`) only with the platform team.

### Namespace isolation

Do not put production credentials or sensitive customer context in engram. If you need to reference a customer constraint, describe the constraint, not the customer's data.

```
Good: "Customer in the financial sector requires all data to stay in EU regions.
       Never deploy new services to us-east without checking with their team."

Bad:  "Acme Bank (contact: john@acmebank.com, contract #A-44291) requires EU data residency."
```

---

## Summary

The difference between an AI-assisted team using Obsidian vaults and one using engram is the difference between a team where each engineer carries their own notebook and a team that shares a living, searchable, self-improving institutional brain.

Obsidian makes one engineer more productive. engram makes the whole team smarter together — and makes each new hire immediately as context-rich as a senior engineer.

The knowledge a senior architect accumulated over three years does not walk out the door when they go on holiday. The lesson a developer learned from a 4am production incident surfaces automatically the next time anyone touches that code. The QA engineer's hard-won understanding of edge cases becomes a real-time guardrail for every developer on the team.

That is the enterprise value of engram: **it turns individual AI-assisted work into collective intelligence.**
