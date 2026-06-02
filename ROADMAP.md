# Engram Roadmap

Engram is an open-source persistent memory and agent memory policy layer for engineering teams. This document describes what we are building, why, and in what order.

Tiers reflect priority and dependency order, not fixed time boxes. Tier 1 must be substantially complete before Tier 2 delivers full value.

---

## TIER 1 — Foundation (Build Now)

### 1.1 Typed Memory — Decision Records, Constraints, Incidents

**What to build**

Add `memory_type` enum to `MemoryEntry`:

```
fact | decision | constraint | incident | adr | skill
```

Add `status` field:

```
active | superseded | deprecated | proposed
```

Add fields: `author`, `affects: list[str]`, `rationale`, `expires_at`, `review_by`

Add `AFFECTS` graph edge: `Memory → Entity` — binds a decision or constraint to the code entities it governs.

**Why it matters**

ADR tooling exists — adr-tools, Log4brains, MADR — but none of it reaches AI agents automatically. `AGENTS.md` files are static text that goes stale and gets forgotten. Engram makes decisions machine-readable and ambient: a constraint written once surfaces automatically when any agent touches the governed code path.

The Atlan 2026 review of eight memory frameworks found no tool supporting decision records, constraint injection, or agent policy enforcement. This is unoccupied ground.

---

### 1.2 Constraint Injection — Agent Policy Enforcement

**What to build**

`CONSTRAINT` memories bypass the score threshold entirely. When any agent calls `memory_search`, all active `CONSTRAINT` memories matching the namespace path are prepended to results before the top-k vector results are appended.

No special API call required. No agent-side configuration. Constraints are always present when the namespace matches.

**Why it matters**

This is the agent policy enforcement layer. Engineering standards, security rules, approved patterns — stored once in engram, enforced on every agent call touching that namespace. The alternative is maintaining `CLAUDE.md` files that drift, get copied inconsistently, and are silently ignored. Engram makes compliance structural rather than documentary.

---

### 1.3 Git Hook Integration (`engram-git`)

**What to build**

A new installable package `engram-git` providing:

- `post-commit` hook: extracts commit message and changed files, writes a memory to the project namespace tagged with SHA, author, and file paths
- `pre-review` hook: given a diff or PR, retrieves all engram memories relevant to changed files and outputs them as context
- `engram-git install` CLI command to wire hooks into any repo

**Why it matters**

Without this, memory accumulation requires explicit agent action. With it, engram passively learns from the development workflow. Every commit becomes a memory write. Every review surfaces what the team already knows about the changed code. The knowledge graph grows without anyone thinking about it.

---

### 1.4 Skill Coach — AI Coding Agent Capability Discovery

**What to build**

This feature is entirely novel. No competing tool exists.

Architecture:

- `tool:claude-code:capabilities` namespace: seeded catalog of all Claude Code features and patterns
- Each memory entry represents one capability or technique, tagged with: `category` (hooks / commands / patterns / mcp), `when-to-use`, and an executable `example`
- `skill_suggest(task, namespace)` MCP tool: given a description of what the developer is trying to do, returns relevant capabilities they may not know exist
- `skill_discover()` MCP tool: refreshes the capability catalog from public documentation
- Coaching is ambient — relevant skills surface naturally in search results, no explicit query needed

**Examples of what gets surfaced automatically**

| Developer is doing... | Surfaced capability |
|-----------------------|---------------------|
| Running a test loop | `/loop` command |
| Code review on a diff | `git diff \| claude -p "review"` pipe pattern |
| Architecture planning | `/model opusplan` |
| Writing repetitive prompts | Custom slash commands in `.claude/commands/` |
| Wiring automation | Hooks system (PreToolUse, PostToolUse, UserPromptSubmit) |

**Why it matters**

Most developers use 10% of Claude Code's capabilities because they do not know what they do not know. Skill Coach closes that gap without requiring developers to read documentation. The capabilities come to them in context when they are relevant.

---

## TIER 2 — Team Collaboration (Next Quarter)

### 2.1 Namespace Subscriptions (Pub-Sub)

Teams subscribe to namespaces they care about. When the backend team writes to `org:myteam:engineering:api-contracts`, the frontend team's next session receives those memories automatically.

Configuration options per subscription: filter by `memory_type`, delivery mode (`on_next_session | webhook | immediate`).

This solves cross-team knowledge fragmentation without Slack announcements or coordination meetings. Backend changes, security requirements, and deprecation notices are delivered in context at the point of work.

---

### 2.2 Structured Memory Provenance

Replace the free-string `source` field with a structured provenance object:

```json
{
  "agent_id": "claude-code-session-abc",
  "user_id": "alice",
  "tool": "claude-code",
  "git_commit": "abc123",
  "jira_ticket": "HPTE-242",
  "team": "platform"
}
```

Every decision has a chain of custody. Every memory is traceable to a person, session, commit, and ticket. This is the audit trail regulated industries require and currently cannot get from any memory framework.

---

### 2.3 Contradiction Detection

When a memory is written, compare it against existing memories in the same namespace with overlapping entity references. Flag semantic contradictions — high similarity score combined with opposite claim direction — and surface them for human review before they are committed to the graph.

Without this, teams silently corrupt their knowledge base as architectural decisions evolve. The system should resist corruption structurally, not rely on humans to notice conflicts manually.

---

### 2.4 Memory Expiry and Review Contracts

`expires_at` (defined in Tier 1 data model) is enforced: expired memories are filtered from all search results.

`review_by` enforcement: a periodic job surfaces memories past their review date to their owners for confirmation or deprecation.

Library pinning decisions, temporary workarounds, and time-boxed architecture choices should decay when they are no longer true. A memory system that accumulates stale facts without decay becomes a liability.

---

## TIER 3 — Intelligence Layer (6-Month Horizon)

### 3.1 Incident Intelligence

**Workflow**

1. Webhook from PagerDuty or AlertManager creates an incident entry in the `incident:` namespace
2. On creation: automatic vector + graph search for similar past incidents
3. Before the oncall engineer starts debugging, they receive ranked similar incidents with resolution steps
4. On resolution: write a structured RCA memory linked to affected entities
5. Graph traversal enables multi-hop reasoning: `incident → SIMILAR_TO → past_incident → RESOLVED_BY → config_change → CHANGES → service.yaml`

**Why it matters**

Post-mortems currently live in Confluence pages that no one reads under pressure. Incident intelligence makes RCA knowledge ambient — it surfaces when the same failure pattern recurs, not when someone remembers to search for it.

---

### 3.2 Knowledge Health Metrics

API endpoints and a lightweight dashboard exposing:

- Stale namespaces (no writes in N days)
- Entities referenced frequently but with no `decision` memories attached (undocumented decisions)
- `CONSTRAINT` memories that have never been retrieved (potentially irrelevant)
- Contradiction count per namespace
- Knowledge coverage score per service or team

Gives engineering leads visibility into where knowledge is concentrated versus where it is thin. Surfaces the decisions that exist only in someone's head.

---

### 3.3 LLM-Enriched Relationship Extraction (opt-in)

**Current behavior**: spaCy extracts entity names and writes `MENTIONS` edges.

**Enhanced behavior**: An async background job runs LLM extraction on each write to produce typed relationship edges with semantics:

```
"Acme wants SaaS delivery by Q4 2026"
→ (Acme) --WANTS--> (SaaS)
→ (Acme) --DEADLINE--> (2026-10-01)
```

Gated behind a config flag. Does not block the write path. Makes graph traversal and multi-hop reasoning dramatically richer without requiring authors to structure their memories manually.

---

### 3.4 Community Detection on Entity Graph

A periodic, configurable job runs the Leiden community detection algorithm on the ArcadeDB entity graph. Results are stored as `COMMUNITY` vertex types with `BELONGS_TO` edges.

Surfaces cross-namespace clusters that were never explicitly modeled — the payments-related cluster, the security-compliance cluster, the platform-infrastructure cluster — without requiring anyone to categorize them. Useful for understanding what concepts are architecturally coupled even when they live in different namespaces.

---

### 3.5 Skill Coach v2 — Cross-Tool Awareness

Extend Skill Coach beyond Claude Code to cover:

- Any MCP-connected tool
- GitHub CLI patterns, Docker Compose patterns, Kubernetes patterns
- Team-specific patterns authored by senior engineers and stored as `skill` memories

Senior engineers write skill memories once. Every engineer on the team gets coached from them automatically, in context, without onboarding documents or wiki pages.

---

## Market Analysis

### What Exists and Why It Is Not Enough

| Category | Tools | Gap |
|----------|-------|-----|
| ADR / Decision Records | adr-tools, Log4brains, MADR, AGENTS.md | Static files, not injected into AI context, no graph relationships |
| AI Memory | Mem0, Zep, Letta, LangMem, Graphiti | None support decision records, constraint injection, or cross-team pub-sub (Atlan 2026) |
| Developer Portal | Backstage / Spotify Portal | Heavy infrastructure, not agent-facing, no real-time memory write |
|  Agent Memory Policies | TrueFoundry Gateway, NeMo Guardrails | LLM proxy layer, not knowledge layer — filters outputs but cannot inject org knowledge |
| Incident Intelligence | incident.io (proprietary), Keep (OSS) | No memory graph, no cross-incident learning, not integrated with AI coding agents |
| Skill Coaching | None | No tool discovers and proactively surfaces AI coding tool capabilities to developers |

### The White Space Engram Occupies

No existing tool combines all five of:

1. Real-time read/write memory (not batch RAG)
2. Typed decision and constraint memories with agent policy enforcement
3. Graph relationships between memories and code entities
4. Team namespace sharing with ACL
5. Ambient skill coaching for AI coding tools

The closest competitor is Zep/Graphiti for temporal graph memory. It targets conversational agents, not engineering workflows, not policy management, not skill coaching.

---

## What Not to Build

**A Confluence replacement.** Wrong data model. Painful to import from. The goal is to replace the habit of writing docs, not docs themselves.

**A web UI for knowledge browsing.** Engineers will not use it. The value is ambient injection into agent context, not a knowledge portal. Build APIs; let others build UIs if they want.

**Fine-grained per-memory ACL.** Namespace-level access control is sufficient for v1. Per-memory ACL adds complexity without meaningful security improvement at the team scale engram targets.

**Real-time collaboration.** Engram is an async knowledge store. It is not a live editor or a shared whiteboard.

**Hosted SaaS.** Self-hosted is the value proposition. Engineering teams do not want their architectural decisions and constraint rules in a third-party cloud. Offer a hosted option only after the self-hosted base is strong.
