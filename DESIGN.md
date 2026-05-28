# engram — Comprehensive Design Document

**Version:** 0.2  
**Status:** Draft  
**Date:** 2026-05-22

> **v0.2 Changes:** Replaced Neo4j + Qdrant + Graphiti with ArcadeDB (single Apache 2.0 multi-model database). Removed LLM-dependency for entity extraction (replaced with spaCy). Added UTC timestamps as first-class temporal properties. Added binary asset reference model. Clarified namespace/ACL model. Introduced AI Governance positioning. Simplified to single Docker deployment.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [High-Level Architecture](#3-high-level-architecture)
4. [Component Architecture](#4-component-architecture)
5. [Data Models](#5-data-models)
6. [MCP Tool Definitions](#6-mcp-tool-definitions)
7. [REST API Specification](#7-rest-api-specification)
8. [Runtime Modes](#8-runtime-modes)
9. [Namespace and ACL Model](#9-namespace-and-acl-model)
10. [Orchestrator Design](#10-orchestrator-design)
11. [Gateway Design (Telegram / WhatsApp)](#11-gateway-design)
12. [Configuration Reference](#12-configuration-reference)
13. [Repository Structure](#13-repository-structure)
14. [Docker Deployment](#14-docker-deployment)
15. [Remote Deployment](#15-remote-deployment)
16. [Security and Terms Compliance](#16-security-and-terms-compliance)
17. [Developer Quickstart](#17-developer-quickstart)
18. [Implementation Roadmap](#18-implementation-roadmap)
19. [Agents and Skills](#19-agents-and-skills)
20. [Self-Learning Architecture](#20-self-learning-architecture)
21. [AI Governance Positioning](#21-ai-governance-positioning)
22. [Binary Asset Handling](#22-binary-asset-handling)

---

## 1. Executive Summary

**engram** is an open-source, portable persistent memory and AI governance layer for LLM-based developer and engineering workflows. It solves two fundamental problems: (1) LLM context windows end, losing all learned context; (2) AI agents in engineering teams lack access to organizational decisions, architecture patterns, and domain knowledge — causing hallucinated or off-specification output.

engram provides:

- **Persistent knowledge graph** backed by **ArcadeDB** — a single Apache 2.0 multi-model database providing graph traversal, vector similarity search, and document storage in one unified query layer
- **Temporal knowledge model** — all facts carry UTC `created_at` and nullable `superseded_at` timestamps; search combines semantic relevance with recency weighting; older superseded facts remain accessible as history
- **MCP server** that plugs into Claude Code (and any MCP-compatible client) over local stdio or remote SSE
- **Multi-agent orchestrator** that decomposes tasks, forks worker sessions, collects results, and tears down
- **Mobile gateway** (Telegram, WhatsApp) so the user can interact from any device
- **Multi-runtime support**: Claude Code CLI (desktop), Anthropic API (headless server), OpenRouter (multi-model)
- **Namespace isolation with ACL**: hierarchical `org:team:project` scopes with key-based access control; a single API key can span multiple namespaces
- **Binary asset references**: diagrams, PDFs, and other binaries are indexed by reference (path + SHA-256 hash + extracted content) — never stored in the graph

**Key design principle:** engram is infrastructure. It never holds LLM API keys on behalf of users. Each user/org supplies their own credentials. engram stores memory and orchestrates — it does not serve as an AI proxy or reseller.

**AI Governance:** engram can be deployed as the organizational knowledge layer that governs AI agent behavior. Agents query engram before generating code or decisions, ensuring they operate within the bounds of the organization's actual technical decisions, architecture standards, and compliance rules. See [Section 21](#21-ai-governance-positioning).

---

## 2. Goals and Non-Goals

### Goals

| # | Goal |
|---|------|
| G1 | Give Claude Code (and any MCP client) persistent memory across sessions |
| G2 | Support team/org knowledge sharing via shared namespaces with ACL |
| G3 | Enable multi-agent task forking and collection from a single entry-point session |
| G4 | Run locally (single Docker on laptop) and remotely (VPS, cloud VM) |
| G5 | Support headless/server-side autonomous agents via Anthropic API or OpenRouter |
| G6 | Mobile-first interaction via Telegram (primary) and WhatsApp (optional) |
| G7 | Be fully open-source (Apache 2.0), portable, and self-hostable with no vendor lock-in |
| G8 | Comply with Anthropic's API terms of service |
| G9 | Provide AI governance: agents must query organizational knowledge before acting |
| G10 | Temporal knowledge: every fact is timestamped (UTC); superseded facts are preserved as history |
| G11 | No external API dependency for entity extraction — graph edges built without LLM calls |

### Non-Goals

| # | Non-Goal |
|---|----------|
| N1 | Not a replacement for Claude Code — engram augments it |
| N2 | Not an AI model provider — users supply their own API keys |
| N3 | Not a general-purpose RAG system — memory is agent-scoped |
| N4 | Not a hosted SaaS — self-hosted only in v1 |
| N5 | No fine-tuning or model training on stored memories |

---

## 3. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          DEVELOPER INTERFACES                           │
│                                                                         │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐  │
│  │  Claude Code CLI │  │  Telegram / WA   │  │  REST API / curl     │  │
│  │  (local laptop)  │  │  (mobile)        │  │  (CI/CD, scripts)    │  │
│  └────────┬─────────┘  └────────┬─────────┘  └──────────┬───────────┘  │
└───────────┼─────────────────────┼──────────────────────┼───────────────┘
            │ MCP (stdio/SSE)     │ Telegram/WA API       │ HTTP/REST
            ▼                     ▼                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          ENGRAM SERVER                                  │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  MCP Server  (FastAPI + SSE, port 8765)                         │   │
│  │  Exposes: memory_search, memory_write, memory_delete,           │   │
│  │           graph_query, spawn_task, get_task_result              │   │
│  └──────────────────────────┬───────────────────────────────────────┘   │
│                             │                                           │
│  ┌──────────────────────────▼───────────────────────────────────────┐   │
│  │  Orchestrator                                                    │   │
│  │  - Task decomposition (planner)                                 │   │
│  │  - Worker session lifecycle (fork → await → collect → teardown) │   │
│  │  - Runtime selector (claude-code / api / openrouter)            │   │
│  └──────────────────────────┬───────────────────────────────────────┘   │
│                             │                                           │
│  ┌──────────────────────────▼───────────────────────────────────────┐   │
│  │  Memory Core                                                     │   │
│  │  ┌──────────────────────────────────────────────────────────┐   │   │
│  │  │  ArcadeDB  (Apache 2.0 — graph + vector + document)      │   │   │
│  │  │  · Property graph: entities, relations, facts            │   │   │
│  │  │  · Vector index: HNSW on all node content                │   │   │
│  │  │  · Temporal: created_at + superseded_at on every node    │   │   │
│  │  │  · Cypher + SQL + GraphQL queries                        │   │   │
│  │  │  · One DB, one query, no application-side join           │   │   │
│  │  └──────────────────────────────────────────────────────────┘   │   │
│  │  ┌──────────────────────────────────────────────────────────┐   │   │
│  │  │  Entity Extractor (spaCy — no LLM required)              │   │   │
│  │  │  Extracts named entities on every write → MENTIONS edges │   │   │
│  │  └──────────────────────────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  Gateway (optional)                                              │   │
│  │  ┌──────────────────┐  ┌──────────────────────────────────────┐  │   │
│  │  │  Telegram Bot    │  │  WhatsApp (Evolution API bridge)     │  │   │
│  │  │  (official API)  │  │  (Baileys, unofficial)               │  │   │
│  │  └──────────────────┘  └──────────────────────────────────────┘  │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                   ┌────────────────────┐
                   │  ArcadeDB          │
                   │  (multi-model)     │
                   │  port 2480 (HTTP)  │
                   │  port 2424 (bin)   │
                   └────────────────────┘
```

**Storage model comparison (v0.1 vs v0.2):**

| | v0.1 | v0.2 |
|---|---|---|
| Graph DB | Neo4j (BSL license, JVM, ~1.5GB RAM) | ArcadeDB (Apache 2.0, ~256MB RAM) |
| Vector DB | Qdrant (separate service) | ArcadeDB built-in HNSW |
| Entity extraction | Graphiti (LLM call per write, OpenAI required) | spaCy (local NLP, no API key needed) |
| Hybrid query | Two-system join in application code | Single ArcadeDB query |
| Docker services | 3 (neo4j + qdrant + engram) | 2 (arcadedb + engram) |

---

## 4. Component Architecture

### 4.1 Memory Core (`packages/core`)

The memory core is a Python library. It wraps ArcadeDB behind a single unified API, providing graph traversal, vector similarity search, and document storage through one query interface.

**Responsibilities:**
- CRUD for memories, entities, relations, facts, and asset references in ArcadeDB
- Embed text using local embeddings (nomic-embed-text or sentence-transformers — no external API required)
- Namespace + ACL routing — all reads/writes are scoped to a namespace, enforced by API key
- Entity extraction via spaCy — extracts named entities from every write to build MENTIONS edges without an LLM call
- Temporal management — `created_at` (UTC, immutable) and `superseded_at` (nullable) on all nodes; search applies recency weighting
- Hybrid search — single ArcadeDB query combining vector similarity + graph hops + namespace filter + recency weight

**Key classes:**

```python
class EngramClient:
    """Main entry point for all memory operations."""

    def __init__(self, config: EngramConfig): ...

    async def add(self, content: str, namespace: str,
                  tags: list[str] = None, metadata: dict = None) -> MemoryEntry
    async def search(self, query: str, namespace: str,
                     top_k: int = 10, include_superseded: bool = False) -> list[SearchResult]
    async def delete(self, memory_id: str, namespace: str) -> bool
    async def supersede(self, memory_id: str, namespace: str) -> bool
    async def get_entity(self, name: str, namespace: str) -> Entity | None
    async def get_related(self, entity_name: str, namespace: str, depth: int = 2) -> Graph
    async def add_fact(self, subject: str, predicate: str, object: str,
                       namespace: str) -> Fact
    async def add_asset(self, path: str, namespace: str,
                        related_memory_id: str = None) -> AssetReference
    async def query_graph(self, cypher: str, namespace: str) -> list[dict]
```

**ArcadeDB schema:**

```
// Memory node — every piece of stored knowledge
Memory {
  @rid:        string          // ArcadeDB record ID
  id:          string          // uuid4
  content:     string          // raw text (also vector-indexed)
  namespace:   string          // hierarchical: org:acme:engineering
  created_at:  datetime        // UTC, immutable — set once on write
  superseded_at: datetime|null // UTC — set when a newer version replaces this
  tags:        list[string]
  source:      string          // "agent" | "user" | "file" | "api"
  metadata:    map
}

// Entity node — named concept extracted by spaCy
Entity {
  id:          string
  name:        string          // normalized lowercase
  entity_type: string          // "PERSON" | "ORG" | "TECH" | "DECISION" | "CONCEPT"
  namespace:   string
  created_at:  datetime        // UTC
  superseded_at: datetime|null
}

// Fact node — explicit subject-predicate-object assertion
Fact {
  id:          string
  subject:     string          // entity name
  predicate:   string          // e.g. "uses", "decided", "supersedes"
  object:      string          // entity name or literal
  namespace:   string
  created_at:  datetime        // UTC — when this fact became true
  superseded_at: datetime|null // UTC — when this fact was replaced
}

// Asset reference — pointer to binary file (never stores the binary)
Asset {
  id:          string
  path:        string          // file system path or git URL
  format:      string          // "drawio" | "pdf" | "png" | "docx" | ...
  sha256:      string          // content hash — change detection
  extracted_content: string    // text extracted from the binary (vector-indexed)
  namespace:   string
  created_at:  datetime        // UTC
  superseded_at: datetime|null // UTC — set when file hash changes and new Asset created
  created_by:  string          // user/agent that registered this asset
}

// Edges
Memory   -[MENTIONS]->    Entity     // spaCy extracted this entity from this memory
Fact     -[SUBJECT_OF]->  Entity
Fact     -[OBJECT_OF]->   Entity
Memory   -[DOCUMENTED_IN]-> Asset    // this memory is illustrated by this asset
Entity   -[RELATED_TO]->  Entity     // semantic relationship between entities
Memory   -[SUPERSEDED_BY]-> Memory  // explicit lineage when a decision is updated
```

**Hybrid search query (single ArcadeDB SQL):**

```sql
SELECT
  @rid, content, namespace, created_at, superseded_at, tags,
  vectorSimilarity(content_embedding, :query_embedding) AS vec_score,
  (1.0 / (1.0 + dateDiff('day', created_at, sysdate()) / 90.0)) AS recency_score
FROM Memory
WHERE
  namespace LIKE :ns_prefix + '%'
  AND (superseded_at IS NULL OR :include_superseded = true)
  AND vectorSimilarity(content_embedding, :query_embedding) > 0.70
ORDER BY
  (0.7 * vec_score + 0.3 * recency_score) DESC
LIMIT :top_k
```

Results where `superseded_at IS NOT NULL` are returned tagged `[HISTORICAL]`; results where it is null are tagged `[CURRENT]`.

---

### 4.2 MCP Server (`packages/mcp-server`)

Exposes all memory and orchestration operations as MCP tools. Claude Code connects to this server and gets memory/orchestration capabilities natively without any code changes in the Claude Code CLI.

**Transport modes:**

| Mode | Protocol | Use case |
|------|----------|----------|
| `stdio` | JSON-RPC over stdin/stdout | Local — Claude Code spawns engram as a subprocess |
| `sse` | HTTP + Server-Sent Events | Remote — Claude Code connects to `http://server:8765/sse` |

**MCP server implementation:**

Built on `mcp` Python SDK (official Anthropic MCP SDK). FastAPI handles the SSE transport.

```
packages/mcp-server/
  engram_mcp/
    server.py          # MCP tool registration and main server
    transports/
      stdio.py         # stdio transport handler
      sse.py           # SSE/HTTP transport handler
    tools/
      memory.py        # memory_search, memory_write, memory_delete
      graph.py         # graph_query, get_entity, get_related
      orchestrator.py  # spawn_task, get_task_result, list_tasks
    auth.py            # API key validation for remote mode
```

---

### 4.3 Orchestrator (`packages/orchestrator`)

The orchestrator decomposes complex tasks into subtasks, manages worker session lifecycles, collects results, and writes them back to memory.

**Decomposition flow:**

```
User task (string)
    │
    ▼
Planner (LLM call)
    │  produces: list[SubTask]
    ▼
Worker pool
    ├── spawn(subtask_1) → WorkerSession
    ├── spawn(subtask_2) → WorkerSession
    └── spawn(subtask_3) → WorkerSession
         │ all running concurrently
         ▼
    await all complete (asyncio.gather)
         │
         ▼
Synthesizer (LLM call)
    │  reads all subtask outputs from memory
    ▼
Final result → written to memory + returned to caller
    │
    ▼
teardown all workers
```

**Worker session — two implementations:**

```python
class ClaudeCodeWorker:
    """Spawns a claude CLI subprocess. Requires API key or --dangerously-skip-permissions."""

    async def run(self, task: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "--dangerously-skip-permissions",
            "--print",          # non-interactive, print response
            "-p", task,
            env={**os.environ, "ANTHROPIC_API_KEY": self.api_key}
        )
        stdout, _ = await proc.communicate()
        return stdout.decode()


class ApiWorker:
    """Runs a pure API tool-calling loop. No Claude Code CLI needed."""

    async def run(self, task: str) -> str:
        messages = [{"role": "user", "content": task}]
        while True:
            response = await self.client.messages.create(
                model=self.model,
                tools=ENGRAM_TOOLS,
                messages=messages
            )
            if response.stop_reason == "end_turn":
                return response.content[-1].text
            # handle tool_use blocks, append results, loop
            messages = self._handle_tools(messages, response)
```

**Runtime selector:**

```python
def make_worker(config: WorkerConfig) -> BaseWorker:
    match config.runtime:
        case "claude-code":
            return ClaudeCodeWorker(config)
        case "api":
            return ApiWorker(provider="anthropic", config=config)
        case "openrouter":
            return ApiWorker(provider="openrouter", config=config)
```

---

### 4.4 Gateway (`packages/gateway`)

The gateway translates messages between messaging platforms (Telegram, WhatsApp) and the engram orchestrator. A user sends a message from their phone; engram processes it and sends a reply.

**Telegram gateway:**

Uses `python-telegram-bot` v21+ (async). Official Bot API — no ToS issues.

```python
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = str(update.effective_user.id)
    text     = update.message.text
    ns       = f"personal:{user_id}"

    # Route to orchestrator
    result = await orchestrator.run(task=text, namespace=ns)

    await update.message.reply_text(result)
```

**WhatsApp gateway:**

Uses **Evolution API** — a Docker-ready REST wrapper around the Baileys Node.js library. engram sends/receives WhatsApp messages via Evolution API's webhook.

```
engram ──── HTTP ────→ Evolution API container (port 8080)
                            └── WhatsApp Web protocol (Baileys)
                                      └── WhatsApp servers
```

engram registers a webhook on Evolution API. Incoming WhatsApp messages hit the webhook endpoint in engram's gateway, which forwards them to the orchestrator.

**Gateway routing table:**

```python
@app.post("/webhook/whatsapp")
async def whatsapp_webhook(payload: dict):
    phone     = payload["data"]["key"]["remoteJid"]
    text      = payload["data"]["message"]["conversation"]
    ns        = f"personal:{phone}"
    result    = await orchestrator.run(task=text, namespace=ns)
    await whatsapp_client.send(phone, result)
```

---

### 4.5 REST API (`packages/api`)

FastAPI server exposing engram's memory and orchestration capabilities to non-MCP clients (curl, CI/CD scripts, other tools).

**Base URL:** `http://localhost:8766`

Full spec in Section 7.

---

## 5. Data Models

### 5.0 Temporal Properties — First-Class Design

Every node in engram carries two UTC timestamps. This is not optional — it is the foundation of the temporal knowledge model:

```
created_at:    datetime (UTC, immutable)  — when this fact was recorded
superseded_at: datetime (UTC) | None      — None = currently valid
                                            set = replaced by a newer fact
```

**Why UTC everywhere:** Team members in different timezones (India, US, Philippines, UK) all write to the same knowledge graph. UTC as the single canonical timezone means all temporal comparisons are unambiguous regardless of where the writer is located.

**Supersession pattern:** When a decision changes, the old memory is NOT deleted. Instead:
1. Write the new memory (`created_at = now(), superseded_at = null`)
2. Set `superseded_at = now()` on the old memory
3. Optionally create a `SUPERSEDED_BY` edge from old → new

This preserves full history while making "what do we currently believe?" a simple `WHERE superseded_at IS NULL` filter.

**Recency weighting in search:**
```
recency_score = 1 / (1 + days_since_created / 90)

A memory from today:      recency_score = 1.00
A memory from 90 days ago: recency_score = 0.50
A memory from 1 year ago:  recency_score = 0.22

combined_score = (0.7 × semantic_similarity) + (0.3 × recency_score)
```

Search results surface both `[CURRENT]` and `[HISTORICAL]` results to the user, ranked by combined score.

---

### 5.1 MemoryEntry

```python
@dataclass
class MemoryEntry:
    id: str                     # uuid4
    content: str                # raw text
    namespace: str              # org:acme:engineering / personal:alice / project:platform
    created_at: datetime        # UTC, immutable — set once on write
    superseded_at: datetime | None  # UTC — None = currently valid
    tags: list[str]
    source: str                 # "user" | "agent" | "file" | "api"
    metadata: dict              # arbitrary key-value
    is_current: bool            # computed: superseded_at is None
```

### 5.2 Entity

```python
@dataclass
class Entity:
    id: str
    name: str                   # normalized lowercase
    entity_type: str            # "PERSON" | "ORG" | "TECH" | "DECISION" | "CONCEPT"
    namespace: str
    attributes: dict
    created_at: datetime        # UTC
    superseded_at: datetime | None
```

### 5.3 Relation

```python
@dataclass
class Relation:
    id: str
    source_entity_id: str
    target_entity_id: str
    relation_type: str          # "USES" | "DECIDED" | "DEPENDS_ON" | "SUPERSEDES" | etc.
    namespace: str
    weight: float               # 0.0-1.0 confidence
    created_at: datetime        # UTC
    superseded_at: datetime | None
    attributes: dict
```

### 5.4 Fact

```python
@dataclass
class Fact:
    id: str
    subject: str                # entity name
    predicate: str              # verb phrase: "uses", "decided", "requires"
    object: str                 # entity name or literal value
    namespace: str
    created_at: datetime        # UTC — when this fact became true
    superseded_at: datetime | None  # UTC — when this fact was replaced
    source_memory_id: str | None
```

### 5.5 AssetReference

```python
@dataclass
class AssetReference:
    id: str
    path: str                   # local path or git URL to the binary file
    format: str                 # "drawio" | "pdf" | "png" | "docx" | "svg" | ...
    sha256: str                 # SHA-256 of file bytes — change detection
    extracted_content: str      # text extracted from binary (spaCy + format parser)
    namespace: str
    created_at: datetime        # UTC — when this version was registered
    superseded_at: datetime | None  # UTC — set when file hash changes (new Asset created)
    created_by: str             # user or agent that registered this asset
    related_memory_ids: list[str]  # memories this asset documents
```

See [Section 22](#22-binary-asset-handling) for the full binary asset model.

### 5.5 Task

```python
@dataclass
class Task:
    id: str                     # uuid4
    prompt: str                 # the task description
    namespace: str
    runtime: str                # "claude-code" | "api" | "openrouter"
    status: TaskStatus          # PENDING | RUNNING | COMPLETE | FAILED
    subtasks: list[SubTask]
    result: str | None
    created_at: datetime
    completed_at: datetime | None
    error: str | None
    parent_task_id: str | None  # for nested orchestration

@dataclass
class SubTask:
    id: str
    parent_task_id: str
    prompt: str
    worker_id: str | None
    status: TaskStatus
    result: str | None
```

### 5.6 Session

```python
@dataclass
class Session:
    id: str
    task_id: str
    runtime: str
    pid: int | None             # for claude-code subprocess workers
    status: str                 # ACTIVE | DONE | KILLED
    started_at: datetime
    ended_at: datetime | None
```

---

## 6. MCP Tool Definitions

These are the tools Claude Code sees when connected to engram. Each tool maps to an engram core operation.

### 6.1 `memory_search`

Search engram's memory (vector + graph hybrid) for content related to a query.

```json
{
  "name": "memory_search",
  "description": "Search persistent memory for content related to the query. Returns ranked results from both semantic vector search and knowledge graph traversal.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query":     { "type": "string", "description": "Natural language search query" },
      "namespace": { "type": "string", "description": "Namespace to search: personal:{id}, org:{name}, project:{name}" },
      "top_k":     { "type": "integer", "default": 10, "description": "Max results to return" },
      "mode":      { "type": "string", "enum": ["hybrid", "vector", "graph"], "default": "hybrid" }
    },
    "required": ["query", "namespace"]
  }
}
```

**Returns:**
```json
{
  "results": [
    {
      "id": "uuid",
      "content": "The SP design uses ABAC conditions to restrict...",
      "score": 0.92,
      "source": "agent",
      "created_at": "2026-05-10T14:23:00Z",
      "tags": ["azure", "iam", "pipeline"]
    }
  ],
  "total": 3
}
```

---

### 6.2 `memory_write`

Write a new memory entry.

```json
{
  "name": "memory_write",
  "description": "Persist a piece of information to engram's long-term memory. The content will be embedded and added to the knowledge graph.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "content":   { "type": "string", "description": "The information to store" },
      "namespace": { "type": "string" },
      "tags":      { "type": "array", "items": { "type": "string" } },
      "source":    { "type": "string", "default": "agent" },
      "metadata":  { "type": "object" }
    },
    "required": ["content", "namespace"]
  }
}
```

---

### 6.3 `memory_delete`

Delete a memory entry by ID.

```json
{
  "name": "memory_delete",
  "inputSchema": {
    "type": "object",
    "properties": {
      "memory_id": { "type": "string" },
      "namespace":  { "type": "string" }
    },
    "required": ["memory_id", "namespace"]
  }
}
```

---

### 6.4 `graph_query`

Run a raw Cypher query against the knowledge graph. For advanced graph traversal.

```json
{
  "name": "graph_query",
  "description": "Execute a Cypher query against the Neo4j knowledge graph. Use for relationship traversal, entity lookup, and temporal queries.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "cypher":    { "type": "string", "description": "Cypher query. Must be read-only (MATCH only)." },
      "namespace": { "type": "string" },
      "params":    { "type": "object", "description": "Query parameters" }
    },
    "required": ["cypher", "namespace"]
  }
}
```

---

### 6.5 `get_entity`

Look up a named entity and its relations.

```json
{
  "name": "get_entity",
  "description": "Look up a named entity in the knowledge graph and return its attributes and relationships.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "name":      { "type": "string" },
      "namespace": { "type": "string" },
      "depth":     { "type": "integer", "default": 2 }
    },
    "required": ["name", "namespace"]
  }
}
```

---

### 6.6 `spawn_task`

Fork a background task. Returns a task ID immediately; the task runs asynchronously.

```json
{
  "name": "spawn_task",
  "description": "Spawn a background worker to complete a subtask. The worker has access to engram memory. Returns task_id. Use get_task_result to retrieve the output.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "prompt":    { "type": "string", "description": "Task description for the worker" },
      "namespace": { "type": "string" },
      "runtime":   { "type": "string", "enum": ["claude-code", "api", "openrouter"], "default": "api" },
      "timeout_s": { "type": "integer", "default": 300 }
    },
    "required": ["prompt", "namespace"]
  }
}
```

**Returns:**
```json
{ "task_id": "uuid", "status": "PENDING" }
```

---

### 6.7 `get_task_result`

Poll or await the result of a spawned task.

```json
{
  "name": "get_task_result",
  "description": "Get the current status and result of a previously spawned task.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "task_id": { "type": "string" },
      "wait":    { "type": "boolean", "default": false, "description": "Block until complete (max 30s per call)" }
    },
    "required": ["task_id"]
  }
}
```

**Returns:**
```json
{
  "task_id": "uuid",
  "status": "COMPLETE",
  "result": "The analysis shows...",
  "completed_at": "2026-05-21T10:23:00Z"
}
```

---

### 6.8 `list_tasks`

List active or recent tasks.

```json
{
  "name": "list_tasks",
  "description": "List tasks in the orchestrator for a given namespace.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "namespace": { "type": "string" },
      "status":    { "type": "string", "enum": ["PENDING", "RUNNING", "COMPLETE", "FAILED", "ALL"], "default": "ALL" },
      "limit":     { "type": "integer", "default": 20 }
    },
    "required": ["namespace"]
  }
}
```

---

## 7. REST API Specification

**Base URL:** `http://localhost:8766/api/v1`

All endpoints require `Authorization: Bearer <ENGRAM_API_KEY>` header.

### Memory

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/memory` | Write a memory entry |
| `GET` | `/memory/search?q=&ns=&top_k=` | Search memories |
| `GET` | `/memory/{id}` | Get a single memory by ID |
| `DELETE` | `/memory/{id}` | Delete a memory |

### Graph

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/graph/query` | Execute Cypher query |
| `GET` | `/graph/entity/{name}?ns=&depth=` | Get entity + relations |
| `POST` | `/graph/fact` | Add a temporal fact |

### Orchestrator

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tasks` | Spawn a task |
| `GET` | `/tasks/{id}` | Get task status and result |
| `GET` | `/tasks?ns=&status=` | List tasks |
| `DELETE` | `/tasks/{id}` | Cancel a running task |

### Admin

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check — returns status of ArcadeDB, gateway, version |
| `GET` | `/namespaces` | List all namespaces |
| `POST` | `/namespaces` | Create a namespace |
| `DELETE` | `/namespaces/{ns}` | Delete namespace and all its data |

---

## 8. Runtime Modes

engram supports three worker runtimes. The runtime is configured globally in `engram.yaml` and can be overridden per `spawn_task` call.

### 8.1 `claude-code` Mode

**How it works:**  
Spawns `claude` CLI subprocesses. Each worker is an isolated Claude Code session with its own context window. Workers write results to engram memory via the MCP connection (the worker also connects to the same engram MCP server).

**Requirements:**
- `claude` CLI installed on the machine running engram
- `ANTHROPIC_API_KEY` set (OAuth tokens are not allowed for headless use per Anthropic terms)
- Or `--dangerously-skip-permissions` flag for fully non-interactive mode

**When to use:**  
Local desktop mode. Developer runs engram and Claude Code on the same laptop. Best for code editing tasks where Claude Code's file access is needed.

**Worker invocation:**
```bash
claude --dangerously-skip-permissions --print -p "TASK PROMPT" \
  --mcp-config /tmp/engram-worker-mcp.json
```

**MCP config injected per worker** (`/tmp/engram-worker-mcp.json`):
```json
{
  "mcpServers": {
    "engram": {
      "url": "http://localhost:8765/sse",
      "apiKey": "WORKER_SESSION_TOKEN"
    }
  }
}
```

---

### 8.2 `api` Mode

**How it works:**  
Runs a Python `asyncio` agent loop calling the Anthropic API directly. No Claude Code CLI required. Fully headless and server-safe.

**Requirements:**
- `ANTHROPIC_API_KEY` in environment or `engram.yaml`
- Python `anthropic` SDK

**When to use:**  
Server deployments. Autonomous background tasks. Scenarios where Claude Code CLI is not available or desired.

**Loop structure:**
```python
async def api_worker_loop(task: str, tools: list, model: str, api_key: str) -> str:
    client   = anthropic.AsyncAnthropic(api_key=api_key)
    messages = [{"role": "user", "content": task}]

    while True:
        response = await client.messages.create(
            model=model,
            max_tokens=8192,
            tools=tools,          # engram tools + any task-specific tools
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return extract_text(response)

        # Process tool_use blocks
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await dispatch_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user",      "content": tool_results}
        ]
```

---

### 8.3 `openrouter` Mode

**How it works:**  
Same agent loop as `api` mode but routes to OpenRouter, enabling any supported model (GPT-4o, Gemini, Llama 3, Mistral, etc.) as the worker.

**Requirements:**
- `OPENROUTER_API_KEY` in environment or `engram.yaml`

**When to use:**  
Multi-model routing. Cost optimization (use cheaper models for low-complexity subtasks). Fallback when Anthropic API is unavailable.

**Configuration example:**
```yaml
runtime:
  default: api
  workers:
    planner:
      runtime: api
      model: claude-opus-4-7-20250514
    summarizer:
      runtime: openrouter
      model: google/gemini-flash-1.5
    code:
      runtime: claude-code
```

---

## 9. Namespace and ACL Model

### 9.1 What a Namespace Is

A namespace is an **access boundary and search scope combined into one string**. It answers two questions simultaneously:

- **Who is allowed to see this memory?** (access boundary)
- **When I search, which pool of memories do I search in?** (search scope)

In a pure knowledge graph, everything is connected and visible. In enterprise reality, you need isolation: a PM's pricing strategy must not be visible to an engineer's agent; Customer A's data must be invisible when searching for Customer B. Namespaces enforce this without requiring separate database instances.

### 9.2 Namespace Hierarchy (Left to Right)

The colon is a hierarchy separator. The hierarchy reads left to right — broadest scope on the left, most specific on the right:

```
org : acme : engineering : backend
 │      │         │           │
 │      │         │           └── sub-team (most specific)
 │      │         └────────────── department
 │      └──────────────────────── organization name
 └─────────────────────────────── scope type (broadest)
```

**Prefix search inheritance:** Searching `org:acme:engineering` returns results from ALL child namespaces — `org:acme:engineering:backend`, `org:acme:engineering:frontend`, `org:acme:engineering:infra` — without naming each one explicitly. This is how an AI agent can say "give me all engineering knowledge" in one query.

### 9.3 Namespace Types

| Type | Format | Who sees it | Purpose |
|------|--------|-------------|---------|
| **Personal/local** | `personal:{username}` | One user only, never shared | Developer's local working notes, draft ideas, personal context |
| **Org-wide** | `org:{orgname}` | All members with org-level key | Shared company knowledge: values, high-level standards |
| **Team** | `org:{orgname}:{team}` | Members of that team | Engineering patterns, PM specs, QA standards |
| **Customer** | `org:{orgname}:customers:{name}` | Restricted keys only | Customer context, pricing, account notes |
| **Project** | `project:{projectname}` | Project team members | Sprint context, decisions for one specific project |

### 9.4 API Key → Namespace Mapping (The ACL Model)

A **single API key can cover multiple namespaces**. This is the enterprise governance primitive: you issue one key per developer and specify exactly which namespaces they can read and write.

```yaml
# engram.yaml — auth section
auth:
  api_keys:
    # Engineer Alice — can read all engineering, write only to her team + personal
    - key: "eng-alice-key-xxxx"
      user_id: "alice"
      namespaces:
        - namespace: "personal:alice"
          access: "read_write"
        - namespace: "org:acme:engineering:backend"
          access: "read_write"
        - namespace: "org:acme:engineering"
          access: "read_only"          # read ALL engineering, write only to backend
        - namespace: "org:acme:product"
          access: "read_only"          # can read PM specs, cannot write
        # no entry for org:acme:customers:* → no access at all

    # Product Manager Bob — can write to product, read engineering
    - key: "pm-bob-key-yyyy"
      user_id: "bob"
      namespaces:
        - namespace: "personal:bob"
          access: "read_write"
        - namespace: "org:acme:product"
          access: "read_write"
        - namespace: "org:acme:engineering"
          access: "read_only"

    # Admin key — full access
    - key: "admin-key-zzzz"
      user_id: "admin"
      namespaces:
        - namespace: "org:acme"
          access: "read_write"         # wildcard: covers all org:acme:* children
```

Access resolution is prefix-based: a key with `org:acme` `read_write` automatically covers `org:acme:engineering`, `org:acme:product`, `org:acme:customers:*`, etc.

### 9.5 Two Deployment Modes

**Personal / Local mode** — one user, no team:
```
personal:alice     ← private notes, working context
                   ← never shared, never leaves the local machine
```
No auth needed beyond a local API key. ArcadeDB runs in local Docker. This is the default for individual developers.

**Enterprise / Team mode** — multi-user, shared knowledge:
```
org:acme                        ← company-wide shared knowledge
org:acme:engineering            ← all engineering teams
org:acme:engineering:backend    ← backend team
org:acme:product                ← product management
org:acme:customers:centene      ← customer-specific (restricted)
personal:alice                  ← Alice's private notes (not in shared pool)
```
Each team member gets their own API key scoped to their allowed namespaces. Personal namespaces are local to each user and never merge with the shared pool.

### 9.6 Cross-Namespace Search

An agent with the right key can search across multiple namespaces in one call:

```python
# An engineering agent searching relevant context for a task
results = await client.search(
    query="how do we handle FHIR member-match",
    namespace="org:acme:engineering",    # searches all engineering/* children
    include_superseded=False             # only current facts
)

# A developer searching their own memory + shared engineering knowledge
results = await client.search(
    query="auth service JWT decision",
    namespaces=["personal:alice", "org:acme:engineering"]
)
```

### 9.7 Namespace Governance in the UI

The dashboard shows namespace hierarchy visually, with:
- **Key assignments**: which keys cover which namespaces
- **Content counts**: memories per namespace
- **Recent activity**: last write per namespace (helps detect stale namespaces)
- **Access matrix**: which user/key can read/write which namespace

This gives the operator transparency into who knows what — a core requirement for AI governance.

---

## 10. Orchestrator Design

### 10.1 Planner

The planner takes a user's task prompt and decomposes it into subtasks. It is itself an LLM call.

**Planner prompt template:**
```
You are an orchestrator planner. Break the following task into 1-5 independent subtasks.
Each subtask must be completable without needing the result of any other subtask (parallel).
If the task is simple enough for one worker, return a single subtask.

Task: {task}

Respond with a JSON array:
[
  { "id": "1", "prompt": "...", "rationale": "..." },
  ...
]
```

### 10.2 Worker Pool

```python
class WorkerPool:
    max_concurrent: int = 5

    async def run_parallel(self, subtasks: list[SubTask]) -> list[SubTaskResult]:
        semaphore = asyncio.Semaphore(self.max_concurrent)
        async with semaphore:
            results = await asyncio.gather(
                *[self.run_one(st) for st in subtasks],
                return_exceptions=True
            )
        return results
```

### 10.3 Synthesizer

After all subtasks complete, the synthesizer combines results.

**Synthesizer prompt template:**
```
You are synthesistartup-corp results from {n} parallel worker agents.
The original task was: {original_task}

Worker results:
{worker_results}

Synthesize a single coherent response to the original task.
Write any key learnings to memory via the memory_write tool before responding.
```

### 10.4 Task State Machine

```
PENDING ──► PLANNING ──► RUNNING ──► SYNTHESIZING ──► COMPLETE
                │                                         │
                └─────────────── FAILED ◄────────────────┘
                                    │
                               (error stored)
```

### 10.5 Teardown

After a task reaches COMPLETE or FAILED, engram:
1. Kills any running Claude Code subprocesses (SIGTERM, then SIGKILL after 5s)
2. Cleans up temp MCP config files
3. Writes task summary to memory
4. Marks sessions as KILLED

---

## 11. Gateway Design

### 11.1 Telegram

**Setup:**
1. Create a bot via `@BotFather` → get `TELEGRAM_BOT_TOKEN`
2. Set `TELEGRAM_ALLOWED_USERS` to a comma-separated list of Telegram user IDs (allowlist for security)
3. engram gateway starts polling or sets up webhook

**Conversation flow:**
```
User (phone) ──[sends message]──► Telegram servers ──► engram gateway
                                                              │
                                                      namespace = personal:{telegram_user_id}
                                                              │
                                                      orchestrator.run(message, namespace)
                                                              │
                                                      result (may take 30-120s)
                                                              │
                                                      reply back ──► Telegram servers ──► User
```

**Streaming replies:**  
For long tasks, the gateway sends a "working..." message first, then edits it with the result when ready. For tasks > 60s, it sends periodic "still thinking..." updates.

**Commands:**
```
/memory search <query>     - search memory without triggering full orchestration
/memory list               - list recent memories
/task status <id>          - check a spawned task
/ns <namespace>            - switch active namespace
/help                      - show available commands
```

### 11.2 WhatsApp (via Evolution API)

**Architecture:**
```
engram ─── HTTP ───► Evolution API (Docker, port 8080)
                           │
                    WhatsApp Web protocol
                           │
                    WhatsApp servers ◄──► User's phone
```

**Evolution API setup:**
```bash
docker run -d \
  --name evolution-api \
  -p 8080:8080 \
  -e AUTHENTICATION_API_KEY=your-evolution-api-key \
  -e WEBHOOK_GLOBAL_URL=http://engram:8766/webhook/whatsapp \
  atendai/evolution-api:latest
```

After startup, scan a QR code to connect a WhatsApp account. Then engram receives all messages via the webhook.

**Note:** WhatsApp via Evolution API / Baileys is unofficial (reverse-engineered WhatsApp Web protocol). It works reliably for personal/team use but violates WhatsApp ToS for bulk messaging. engram uses it only for interactive single-user communication, which is low risk in practice.

---

## 12. Configuration Reference

### `engram.yaml`

```yaml
# engram.yaml — main configuration file

server:
  host: 0.0.0.0
  mcp_port: 8765
  api_port: 8766
  log_level: INFO

auth:
  api_keys:
    - key: engram-key-CHANGEME
      user_id: default
      namespaces: ["*"]   # * = all namespaces

neo4j:
  uri: bolt://localhost:7687
  username: neo4j
  password: password-CHANGEME
  database: neo4j

qdrant:
  host: localhost
  port: 6333
  collection: engram_memories

embeddings:
  provider: openai               # openai | local
  model: text-embedding-3-small
  # For local embeddings (no OpenAI dependency):
  # provider: local
  # model: sentence-transformers/all-MiniLM-L6-v2

runtime:
  default: api                   # claude-code | api | openrouter
  max_concurrent_workers: 5
  worker_timeout_s: 300
  api:
    provider: anthropic
    model: claude-sonnet-4-6
    api_key: ${ANTHROPIC_API_KEY}   # env var reference
  openrouter:
    model: anthropic/claude-sonnet-4-6
    api_key: ${OPENROUTER_API_KEY}

namespaces:
  default: personal:default
  definitions:
    personal:default:
      owners: [default]

gateway:
  telegram:
    enabled: false
    bot_token: ${TELEGRAM_BOT_TOKEN}
    allowed_users: []              # list of telegram user IDs; empty = nobody
    default_namespace: personal:default

  whatsapp:
    enabled: false
    evolution_api_url: http://localhost:8080
    evolution_api_key: ${EVOLUTION_API_KEY}
    default_namespace: personal:default

memory:
  default_ttl_days: null           # null = never expire
  max_entries_per_namespace: 100000
  prune_schedule: "0 2 * * *"      # cron: daily at 2am
```

### Environment Variables

All secrets should be in environment variables, not in `engram.yaml`. The config supports `${VAR_NAME}` interpolation.

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | For `api` runtime + reflection | Anthropic API key |
| `OPENROUTER_API_KEY` | For `openrouter` runtime | OpenRouter API key |
| `OPENAI_API_KEY` | For OpenAI embeddings | OpenAI API key |
| `VOYAGE_API_KEY` | For Voyage AI embeddings | Voyage AI API key |
| `ENGRAM_API_KEY` | Yes | Master API key for MCP/REST auth |
| `ARCADEDB_PASSWORD` | Yes | ArcadeDB root password |
| `ENGRAM_VAULT_KEY` | Yes | 32-byte base64 key for vault AES-256-GCM encryption |
| `ENGRAM_VECTOR_BACKEND` | No | Set to `qdrant` to enable Qdrant HNSW backend |
| `QDRANT_URL` | When qdrant backend | URL of Qdrant instance (default: `http://localhost:6333`) |
| `QDRANT_API_KEY` | When qdrant cloud | Qdrant Cloud API key |
| `TELEGRAM_BOT_TOKEN` | For Telegram gateway | Telegram bot token |
| `EVOLUTION_API_KEY` | For WhatsApp gateway | Evolution API auth key |

---

## 13. Repository Structure

```
engram/
├── README.md
├── DESIGN.md                          ← this document
├── LICENSE                            (Apache 2.0)
├── pyproject.toml                     (workspace root — installs all sub-packages)
├── docker-compose.yml                 (two services: arcadedb + engram; qdrant optional)
├── engram.yaml.example                (copy to engram.yaml, fill secrets)
├── .env.example
│
├── packages/
│   │
│   ├── core/                          # Memory core library (engram-core)
│   │   ├── pyproject.toml
│   │   ├── engram/
│   │   │   ├── client.py              # EngramClient — main entry point
│   │   │   ├── config.py              # EngramConfig + all sub-configs
│   │   │   ├── models.py              # MemoryEntry, Entity, Fact, Provenance, enums
│   │   │   ├── namespace.py           # Namespace routing and ACL
│   │   │   ├── search.py              # SearchResult model
│   │   │   ├── contradiction/
│   │   │   │   └── detector.py        # check_contradictions(), direction detection
│   │   │   ├── decay/
│   │   │   │   └── job.py             # post-search decay (time/access weighted)
│   │   │   ├── extraction/
│   │   │   │   └── spacy_extractor.py # Entity extraction (spaCy NER + patterns)
│   │   │   ├── storage/
│   │   │   │   ├── arcadedb_client.py # All ArcadeDB operations
│   │   │   │   ├── vector_backend.py  # VectorBackend ABC + create_vector_backend()
│   │   │   │   └── qdrant_backend.py  # QdrantVectorBackend (optional)
│   │   │   ├── vector/
│   │   │   │   └── embedder.py        # Embedder factory (OpenAI / Voyage / local)
│   │   │   └── cli/
│   │   │       ├── git_hooks.py       # engram-git CLI
│   │   │       ├── decay_cli.py       # engram-decay CLI
│   │   │       └── import_cmd.py      # engram-import CLI
│   │
│   ├── mcp-server/                    # MCP server — engram-mcp-server
│   │   ├── engram_mcp/
│   │   │   ├── server.py              # MCP tool registration, dispatch
│   │   │   ├── skill_packs.py         # External skill pack loader
│   │   │   ├── tools/
│   │   │   │   ├── memory.py          # memory_write, memory_search, review_due
│   │   │   │   ├── graph.py           # graph_query, get_entity
│   │   │   │   ├── vault.py           # secret_write, secret_read
│   │   │   │   └── orchestrator_tools.py  # spawn_task, get_task_result
│   │   │   └── transports/
│   │   │       ├── stdio.py
│   │   │       └── sse.py             # FastAPI SSE transport
│   │
│   ├── orchestrator/                  # Multi-agent orchestrator
│   │   ├── engram_orchestrator/
│   │   │   ├── orchestrator.py        # Orchestrator (plan → run → synthesize)
│   │   │   ├── planner.py             # LLM-backed task decomposition
│   │   │   ├── synthesizer.py         # LLM-backed result merge
│   │   │   ├── workers/               # api / openrouter / claude-code workers
│   │   │   ├── pool.py                # WorkerPool (asyncio semaphore)
│   │   │   ├── router.py              # AgentRouter (semantic YAML matching)
│   │   │   └── task_store.py          # SQLite-backed task state
│   │
│   ├── api/                           # REST API — engram-api
│   │   ├── engram_api/
│   │   │   ├── main.py                # FastAPI app, lifespan, router registration
│   │   │   ├── schemas.py             # Request/response Pydantic models
│   │   │   ├── key_store.py           # RuntimeKeyStore (SQLite)
│   │   │   └── routers/
│   │   │       ├── memory.py          # /memory/*
│   │   │       ├── graph.py           # /graph/*
│   │   │       ├── tasks.py           # /tasks/*
│   │   │       ├── admin.py           # /admin/*
│   │   │       ├── vault.py           # /vault/*
│   │   │       ├── knowledge.py       # /knowledge/health
│   │   │       ├── subscriptions.py   # /subscriptions/*
│   │   │       ├── webhooks.py        # /webhooks/incident
│   │   │       ├── learning_admin.py  # /learning/*
│   │   │       └── viz.py             # /viz/*
│   │
│   ├── gateway/                       # Telegram + WhatsApp gateway
│   │   ├── engram_gateway/
│   │   │   ├── telegram/
│   │   │   │   └── bot.py             # TelegramGateway (polling, inline feedback)
│   │   │   └── whatsapp/
│   │   │       └── webhook.py         # FastAPI webhook receiver
│   │
│   └── learning/                      # Self-improvement — engram-learning
│       ├── engram_learning/
│       │   ├── episode_store.py       # Task episode persistence
│       │   ├── heuristic_store.py     # Heuristic (rule) persistence
│       │   ├── quality_store.py       # Per-agent quality scores
│       │   ├── reflection.py          # ReflectionService (LLM → heuristics)
│       │   ├── decay.py               # HeuristicDecayService
│       │   └── scheduler.py           # APScheduler-based learning scheduler
│
├── docker/
│   └── Dockerfile                     # engram server image (EMBED_MODE build arg)
│
├── docs/
│   ├── quickstart.md
│   ├── claude-code-setup.md
│   ├── remote-deployment.md
│   ├── gateway.md
│   └── obsidian-migration.md
│
└── tools/                             # Integration tests and CLI utilities
    ├── test_arcadedb.py               # 30-test ArcadeDB integration suite
    ├── test_qdrant_backend.py         # Qdrant VectorBackend + client sync tests
    ├── test_knowledge_health.py       # Knowledge health scoring tests
    ├── test_skill_packs.py            # Skill pack loader tests
    ├── test_learning_admin.py         # Learning admin API tests
    ├── backup.sh                      # stop/rsync/restart backup (keeps last 7)
    └── reembed.py                     # Re-embed all Memory records (dim migration)
```

---

## 14. Docker Deployment

engram ships as a **two-service Docker Compose stack**: ArcadeDB + engram. This replaces the previous three-service setup (Neo4j + Qdrant + engram).

For developers who want the absolute minimum, a **single-container option** bundles both services using supervisord.

### Option A — Docker Compose (recommended)

Two containers, one command:

```yaml
# docker-compose.yml
version: "3.9"

services:

  arcadedb:
    image: arcadedata/arcadedb:latest
    container_name: engram-arcadedb
    ports:
      - "2480:2480"   # HTTP API + dashboard
      - "2424:2424"   # binary protocol
    environment:
      ARCADEDB_SERVER_ROOT_PASSWORD: ${ARCADEDB_PASSWORD}
      ARCADEDB_SERVER_PLUGINS: "GremlinServer,Cypher,GraphQL"
    volumes:
      - arcadedb_data:/home/arcadedb/databases
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:2480/api/v1/ready"]
      interval: 10s
      timeout: 5s
      retries: 10

  engram:
    build:
      context: .
      dockerfile: docker/Dockerfile
    container_name: engram
    ports:
      - "8765:8765"   # MCP SSE
      - "8766:8766"   # REST API + dashboard
    environment:
      ARCADEDB_HOST: arcadedb
      ARCADEDB_PORT: 2480
      ARCADEDB_PASSWORD: ${ARCADEDB_PASSWORD}
      ENGRAM_API_KEY: ${ENGRAM_API_KEY}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}   # optional — only for orchestrator
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY:-}
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN:-}
      EVOLUTION_API_KEY: ${EVOLUTION_API_KEY:-}
    volumes:
      - ./engram.yaml:/app/engram.yaml:ro
      - engram_assets:/app/assets              # asset sync staging area
    depends_on:
      arcadedb: { condition: service_healthy }
    restart: unless-stopped

  evolution-api:                       # optional WhatsApp bridge
    image: atendai/evolution-api:latest
    container_name: engram-evolution
    profiles: ["whatsapp"]             # only starts if --profile whatsapp
    ports:
      - "8080:8080"
    environment:
      AUTHENTICATION_API_KEY: ${EVOLUTION_API_KEY}
      WEBHOOK_GLOBAL_URL: http://engram:8766/webhook/whatsapp
      WEBHOOK_GLOBAL_ENABLED: "true"
    volumes:
      - evolution_data:/evolution/instances
    restart: unless-stopped

volumes:
  arcadedb_data:
  engram_assets:
  evolution_data:
```

### Option B — Single Container

For developers who want zero Docker networking overhead or a portable single binary:

```dockerfile
# docker/Dockerfile.single
FROM ubuntu:24.04

# Install ArcadeDB
RUN apt-get install -y openjdk-21-jre-headless curl
RUN curl -fsSL https://github.com/ArcadeData/arcadedb/releases/latest/download/arcadedb-latest.tar.gz \
    | tar -xz -C /opt/arcadedb

# Install Python + engram
RUN apt-get install -y python3.12 python3-pip
COPY . /app
RUN pip install -e /app/packages/core /app/packages/mcp-server /app/packages/api

# supervisord to run both processes
RUN apt-get install -y supervisor
COPY docker/supervisord.conf /etc/supervisor/conf.d/engram.conf

EXPOSE 2480 8765 8766
CMD ["/usr/bin/supervisord", "-n"]
```

```ini
# docker/supervisord.conf
[program:arcadedb]
command=/opt/arcadedb/bin/server.sh
autostart=true
autorestart=true

[program:engram]
command=python -m engram_api.main
directory=/app
autostart=true
autorestart=true
startsecs=15          # wait for ArcadeDB to be ready
```

```bash
# Build and run single container
docker build -f docker/Dockerfile.single -t engram:latest .
docker run -d \
  -p 8765:8765 -p 8766:8766 \
  -e ENGRAM_API_KEY=your-key \
  -e ARCADEDB_PASSWORD=your-db-password \
  -v engram_data:/home/arcadedb/databases \
  engram:latest
```

### `.env.example`

```bash
# Copy to .env and fill in your values
ARCADEDB_PASSWORD=change-me-strong-password
ENGRAM_API_KEY=engram-change-me

# LLM providers — OPTIONAL
# Only needed for the orchestrator (task spawning). Not needed for memory.
# Embeddings use local models (nomic-embed-text) — no API key needed.
ANTHROPIC_API_KEY=sk-ant-...   # optional: for orchestrator workers
OPENROUTER_API_KEY=sk-or-...   # optional: for multi-model routing

# Gateway — optional
TELEGRAM_BOT_TOKEN=             # get from @BotFather
EVOLUTION_API_KEY=              # only needed if WhatsApp profile enabled
```

> **Note:** engram no longer requires an OpenAI API key. Embeddings are generated locally using `nomic-embed-text` (via `sentence-transformers`). Entity extraction uses spaCy (local NLP). An LLM API key is only needed if you use the orchestrator to spawn background task workers.

### Start commands

```bash
# Full stack (no WhatsApp)
docker compose up -d

# Full stack + WhatsApp gateway
docker compose --profile whatsapp up -d

# Single container
docker run -d -p 8765:8765 -p 8766:8766 --env-file .env engram:latest

# View logs
docker compose logs -f engram

# Stop everything
docker compose down
```

---

## 15. Remote Deployment

### Minimal requirements

- Linux VPS (1 vCPU, 2GB RAM minimum; 4GB recommended for Neo4j)
- Docker + Docker Compose
- Ports 8765 and 8766 accessible (or behind reverse proxy)
- Domain name + TLS recommended (required for Telegram webhook mode)

### Nginx reverse proxy (recommended)

```nginx
server {
    listen 443 ssl;
    server_name engram.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/engram.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/engram.yourdomain.com/privkey.pem;

    location /sse {
        proxy_pass         http://localhost:8765;
        proxy_http_version 1.1;
        proxy_set_header   Connection "";
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;   # SSE keeps connection open
    }

    location /api {
        proxy_pass http://localhost:8766;
    }

    location /webhook {
        proxy_pass http://localhost:8766;
    }
}
```

### Claude Code configuration for remote engram

On the developer's laptop, edit `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "engram": {
      "url": "https://engram.yourdomain.com/sse",
      "apiKey": "your-ENGRAM_API_KEY"
    }
  }
}
```

After saving, restart Claude Code. Type `/mcp` to verify the connection. All engram tools will appear in the tool list.

### Local mode (stdio)

For developers who run engram locally and want zero network overhead:

```json
{
  "mcpServers": {
    "engram": {
      "command": "python",
      "args": ["-m", "engram_mcp.transports.stdio"],
      "env": {
        "ENGRAM_API_KEY": "your-key",
        "ENGRAM_CONFIG": "/path/to/engram.yaml"
      }
    }
  }
}
```

---

## 16. Security and Terms Compliance

### Anthropic Terms Compliance

| Requirement | engram's position |
|-------------|-------------------|
| Claude Code CLI requires API key for headless/server use | engram passes `ANTHROPIC_API_KEY` to worker subprocesses. OAuth tokens are never used. |
| Cannot resell Anthropic API access | engram never holds or proxies API keys. Each user provides their own key in `.env`. |
| Cannot build a competing product to Claude Code | engram is a memory and orchestration layer. It depends on Claude Code as a component. It does not replicate or replace the Claude Code feature set. |
| SDK use in CI/CD is permitted | engram orchestrator uses the `anthropic` Python SDK for `api` mode workers. |

### Security Model

**Authentication:**
- All engram API and MCP access requires a bearer token (`ENGRAM_API_KEY`)
- Tokens are associated with a user ID and namespace ACL in config
- No default tokens — operator must set `ENGRAM_API_KEY` on first run

**Secrets:**
- API keys (Anthropic, OpenAI, etc.) are environment variables, never stored in Neo4j or Qdrant
- `engram.yaml` supports `${VAR}` interpolation so secrets never appear in config files

**Network:**
- By default engram binds to `0.0.0.0` inside Docker but ports are only exposed on localhost
- For remote access, always put behind Nginx + TLS
- Telegram webhook URL must be HTTPS (Telegram requirement)

**Neo4j:**
- Cypher queries from `graph_query` tool are validated to be read-only (MATCH only, no MERGE/CREATE/DELETE)
- Write operations go through the typed Graphiti API, not raw Cypher

**WhatsApp allowlist:**
- No default allowlist = WhatsApp gateway rejects all messages until `allowed_phones` is configured
- Same for Telegram: `allowed_users` must be populated

### Privacy

- All memory is stored on infrastructure the user operates
- No data is sent to Anthropic except the actual LLM inference calls (prompts/responses)
- engram does not log prompt or response content by default (log level INFO shows task IDs only)
- Debug logging (`log_level: DEBUG`) logs prompts — should never be used in production

---

## 17. Developer Quickstart

### Prerequisites

```bash
# Install Docker and Docker Compose
# Install Python 3.11+
# Install uv (recommended) or pip

# Clone the repo
git clone https://github.com/yourorg/engram.git
cd engram

# Copy and fill config
cp .env.example .env
# Edit .env — set NEO4J_PASSWORD, ENGRAM_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY
cp engram.yaml.example engram.yaml
# Edit engram.yaml if needed (defaults work for local dev)
```

### Start the stack

```bash
docker compose up -d
docker compose logs -f engram   # watch until "MCP server ready on :8765"
```

### Connect Claude Code

```bash
# Add to ~/.claude/settings.json
cat >> ~/.claude/settings.json <<'EOF'
{
  "mcpServers": {
    "engram": {
      "url": "http://localhost:8765/sse",
      "apiKey": "your-ENGRAM_API_KEY"
    }
  }
}
EOF

# Start Claude Code and verify
claude
/mcp   # should show engram tools
```

### Write and read a memory

```
# Inside a Claude Code session:
Use the memory_write tool to save this:
  content: "The hc-env-provisioner SP uses ABAC conditions to restrict role assignments"
  namespace: "personal:default"
  tags: ["azure", "iam"]

Now use memory_search to find it:
  query: "azure service principal"
  namespace: "personal:default"
```

### Spawn a background task

```
# Inside a Claude Code session:
Use spawn_task to:
  prompt: "Search memory for all Azure IAM decisions and write a one-page summary"
  namespace: "personal:default"
  runtime: "api"

# Get the task ID, then:
Use get_task_result with the task_id and wait: true
```

---

## 18. Implementation Roadmap

### Phase 1 — Core (MVP)

**Goal:** Working memory with MCP integration. No orchestrator, no gateway.

| Item | Package | Effort |
|------|---------|--------|
| `EngramClient` with Neo4j + Qdrant | `core` | L |
| Graphiti integration | `core` | M |
| Qdrant integration + OpenAI embeddings | `core` | M |
| Namespace routing + basic ACL | `core` | S |
| MCP server — stdio transport | `mcp-server` | M |
| MCP tools: memory_search, memory_write, memory_delete | `mcp-server` | S |
| Docker Compose (neo4j + qdrant + engram) | infra | S |
| Unit tests | core + mcp | M |
| README + Claude Code setup guide | docs | S |

**Deliverable:** Developer can connect Claude Code to engram and persist/search memories across sessions.

---

### Phase 2 — Remote Access + REST API

**Goal:** engram runs on a remote server; developer connects from laptop.

| Item | Package | Effort |
|------|---------|--------|
| MCP server — SSE transport | `mcp-server` | M |
| API key authentication middleware | `mcp-server` | S |
| REST API (memory + graph + admin) | `api` | M |
| Nginx config + TLS setup docs | docs | S |
| MCP graph tools (graph_query, get_entity) | `mcp-server` | S |
| Integration tests | all | M |

**Deliverable:** engram runs on a VPS; Claude Code on laptop connects via SSE.

---

### Phase 3 — Orchestrator

**Goal:** Multi-agent task forking from a single Claude Code session.

| Item | Package | Effort |
|------|---------|--------|
| Task and SubTask models + SQLite store | `orchestrator` | M |
| Planner (LLM decomposition) | `orchestrator` | M |
| `ApiWorker` (Anthropic tool-calling loop) | `orchestrator` | L |
| `ClaudeCodeWorker` (subprocess) | `orchestrator` | M |
| `WorkerPool` (asyncio semaphore) | `orchestrator` | S |
| Synthesizer | `orchestrator` | M |
| MCP tools: spawn_task, get_task_result, list_tasks | `mcp-server` | S |
| REST API: tasks endpoints | `api` | S |
| `OpenRouterWorker` | `orchestrator` | M |

**Deliverable:** Developer can type a complex task in Claude Code and get it parallelized across N worker agents.

---

### Phase 4 — Mobile Gateway

**Goal:** Interact with engram from phone via Telegram.

| Item | Package | Effort |
|------|---------|--------|
| Telegram bot setup + polling | `gateway` | M |
| Message → orchestrator → reply flow | `gateway` | M |
| /memory commands | `gateway` | S |
| Streaming "still thinking" updates | `gateway` | S |
| WhatsApp (Evolution API webhook) | `gateway` | L |
| User identity → namespace mapping | `gateway` | S |

**Deliverable:** Developer sends a message from Telegram; engram runs the task and replies.

---

### Phase 5 — Team / Org Namespaces

**Goal:** Multiple developers share org-level memory.

| Item | Package | Effort |
|------|---------|--------|
| Multi-user API key table | `api` | M |
| Namespace ACL enforcement | `core` | M |
| Namespace admin endpoints | `api` | S |
| Cross-namespace search | `core` | M |
| Team onboarding guide | docs | S |

---

### Effort key

| Label | Estimate |
|-------|----------|
| S | 0.5 – 1 day |
| M | 1 – 3 days |
| L | 3 – 5 days |

**Total Phase 1-3 (solo developer):** ~3-4 weeks  
**Total Phase 1-5 (solo developer):** ~7-8 weeks

---

## 19. Agents and Skills

### 19.1 Design Philosophy

engram does not require users to write agents. The system is designed in three tiers so that progressively more sophisticated use requires progressively more user effort — but the zero-effort tier is fully functional for most workflows.

| Tier | What the user does | Code required |
|------|--------------------|---------------|
| 1 — Dynamic | Just send tasks to the orchestrator | None |
| 2 — Declarative | Write a YAML agent definition | None |
| 3 — Custom tools | Write one Python function per skill | Minimal |
| 4 — Custom agent | Extend `BaseWorker` in Python | Developer |

Tiers 1 and 2 cover ~80% of real-world usage.

---

### 19.2 Tier 1 — Dynamic Agents (zero-code)

The orchestrator creates workers on demand. A worker is a generic agent that receives a task prompt plus the full set of available tools. It reasons about what to do without any pre-specified behaviour. No user configuration is needed.

```
User prompt: "Review the platform-infra Terraform and flag any IAM misconfigurations"
                  │
                  ▼
            Orchestrator
            ├── Planner decomposes → 3 subtasks
            ├── Worker A: spawned with subtask 1 + all tools
            ├── Worker B: spawned with subtask 2 + all tools
            └── Worker C: spawned with subtask 3 + all tools
                              ↓
                    Workers reason freely — no prescribed steps
```

Workers have access to:
- All engram MCP tools (memory, graph, spawn_task)
- All registered skills (see §19.5)
- Any external MCP servers configured in engram.yaml

---

### 19.3 Tier 2 — Declarative Agent Definitions (YAML)

A **named agent** is a YAML file in the `agents/` directory. It pins a system prompt, model, tool subset, and behaviour parameters. The orchestrator matches tasks to agents using the `description` field (semantic similarity search against the task prompt).

#### Agent definition schema

```yaml
# agents/code-reviewer.yaml

name: code-reviewer
version: "1.0"
description: >
  Reviews source code for security vulnerabilities, correctness, and
  adherence to coding standards. Specialises in Java Spring Boot and Python.

model: claude-sonnet-4-6          # override global default per agent
temperature: 0.2
max_tokens: 8192

system_prompt: |
  You are a senior security-focused code reviewer. When reviewing code:
  1. Check for OWASP Top 10 vulnerabilities (injection, XSS, IDOR, etc.)
  2. Flag incorrect or missing error handling
  3. Note performance anti-patterns
  4. Identify missing input validation at API boundaries
  Always cite the file path and line number. Explain WHY something is a
  problem, not just that it is. End with a severity-ranked finding list.

tools:
  - memory_search         # look up past decisions and known patterns
  - memory_write          # save findings for future reference
  - filesystem_read       # read source files (requires filesystem MCP)
  - graph_query           # check entity relationships

use_critic: true          # run a critic pass on the draft output
critic_model: claude-haiku-4-5   # cheaper model for the critic pass
critic_prompt: |
  You are reviewing a code review report. Check:
  1. Are the line numbers accurate?
  2. Are any obvious issues missed?
  3. Is the severity ranking justified?
  Reply with a list of corrections only. If the report is accurate, reply "LGTM".

namespace_scope:
  - personal              # can read/write personal namespace
  - project               # can read/write project namespace (opt-in)

timeout_s: 180
retry_on_failure: 2
```

#### Field reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique identifier. Used in spawn_task `agent:` override. |
| `version` | string | No | Semantic version for tracking changes. |
| `description` | string | Yes | Used for semantic matching to incoming tasks. |
| `model` | string | No | Overrides `runtime.api.model` from engram.yaml. |
| `temperature` | float | No | Default 0.5. Lower = more deterministic. |
| `max_tokens` | int | No | Default 8192. |
| `system_prompt` | string | Yes | The agent's persona and instructions. |
| `tools` | list | No | Restrict to a subset of available tools. Default: all. |
| `use_critic` | bool | No | Default false. Enable critic-worker loop. |
| `critic_model` | string | No | Model for the critic pass. Default: same as `model`. |
| `critic_prompt` | string | No | Critic evaluation instructions. |
| `namespace_scope` | list | No | Namespaces this agent may read/write. |
| `timeout_s` | int | No | Max seconds before worker is killed. Default: 300. |
| `retry_on_failure` | int | No | Number of retries on error. Default: 0. |

#### Agent discovery and matching

At startup, engram scans `agents/` and embeds each agent's `description` into Qdrant under the `agents` collection. When the orchestrator receives a task, it searches this collection:

```python
async def select_agent(task: str) -> AgentDefinition | None:
    results = await qdrant.search(
        collection="agents",
        query_vector=await embed(task),
        limit=1,
        score_threshold=0.82    # below this, fall back to generic worker
    )
    if results:
        return load_agent(results[0].payload["name"])
    return None  # use generic worker
```

If no agent matches with high confidence, the orchestrator uses a generic worker. If the user wants to force a specific agent, they can override in the task prompt:

```
spawn_task(
  prompt="Review auth/SessionManager.java",
  agent="code-reviewer",          # explicit override
  namespace="project:myplatform"
)
```

---

### 19.4 Community Agent Library

engram ships a library of pre-built agent definitions in `agents/builtin/`. Users can use them as-is, override them locally, or contribute new ones.

| Agent | File | Purpose |
|-------|------|---------|
| `planner` | `builtin/planner.yaml` | Task decomposition (used internally by orchestrator) |
| `synthesizer` | `builtin/synthesizer.yaml` | Result synthesis (used internally) |
| `critic` | `builtin/critic.yaml` | Generic output critic |
| `researcher` | `builtin/researcher.yaml` | Web search + memory synthesis |
| `summarizer` | `builtin/summarizer.yaml` | Condense long content |
| `code-reviewer` | `builtin/code-reviewer.yaml` | Security and correctness review |
| `doc-writer` | `builtin/doc-writer.yaml` | Structured documentation generation |
| `data-analyst` | `builtin/data-analyst.yaml` | Query, chart, and insight extraction |
| `test-writer` | `builtin/test-writer.yaml` | Generate unit and integration tests |
| `refactor` | `builtin/refactor.yaml` | Safe code refactoring with rationale |

Users add custom agents alongside builtins in the `agents/` directory. Local agents with the same name as a builtin take precedence.

---

### 19.5 Skills — Tool Registration

A **skill** is a Python async function registered as an MCP tool. It extends what agents can do beyond the built-in engram tools.

#### How skills are registered

```python
# skills/azure_cli.py
from engram.skills import skill

@skill(
    name="az_role_list",
    description="List Azure role assignments at a given scope",
    parameters={
        "scope": {
            "type": "string",
            "description": "Azure resource scope (e.g. /subscriptions/xxx)"
        },
        "assignee": {
            "type": "string",
            "description": "Optional: filter by assignee object ID"
        }
    }
)
async def az_role_list(scope: str, assignee: str = None) -> list[dict]:
    cmd = ["az", "role", "assignment", "list", "--scope", scope, "--output", "json"]
    if assignee:
        cmd += ["--assignee", assignee]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return json.loads(out)
```

Drop this file in `skills/`. On startup, engram imports all `skills/*.py` files and registers decorated functions as MCP tools. No other wiring required.

#### Skill auto-discovery

```python
# engram/skills/loader.py
def load_skills(skills_dir: Path) -> list[SkillDefinition]:
    skills = []
    for path in skills_dir.glob("*.py"):
        module = importlib.import_module(f"skills.{path.stem}")
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if hasattr(fn, "_engram_skill"):
                skills.append(fn._engram_skill)
    return skills
```

#### Built-in skills

engram ships with these skills pre-registered:

| Skill | Description |
|-------|-------------|
| `memory_search` | Search vector + graph memory |
| `memory_write` | Persist a memory entry |
| `memory_delete` | Delete a memory entry |
| `graph_query` | Execute a Cypher query (read-only) |
| `get_entity` | Look up an entity and its relations |
| `spawn_task` | Fork a background worker |
| `get_task_result` | Retrieve a spawned task's output |
| `list_tasks` | List tasks for a namespace |
| `web_search` | Search the web (requires Brave/Serper API key) |
| `fetch_url` | Fetch and parse a URL |

#### External MCP skill packs

engram can load any external MCP server as a skill pack. Configure in `engram.yaml`:

```yaml
skill_packs:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/projects"]

  - name: github
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: ${GITHUB_TOKEN}

  - name: brave-search
    command: npx
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
    env:
      BRAVE_API_KEY: ${BRAVE_API_KEY}
```

All tools from these MCP servers are automatically available to every agent.

---

### 19.6 Repository Structure — Agents and Skills

```
engram/
├── agents/
│   ├── builtin/
│   │   ├── planner.yaml
│   │   ├── synthesizer.yaml
│   │   ├── critic.yaml
│   │   ├── researcher.yaml
│   │   ├── summarizer.yaml
│   │   ├── code-reviewer.yaml
│   │   ├── doc-writer.yaml
│   │   ├── data-analyst.yaml
│   │   ├── test-writer.yaml
│   │   └── refactor.yaml
│   └── (user-defined agents go here)
│
├── skills/
│   ├── __init__.py             # @skill decorator
│   ├── loader.py               # auto-discovery
│   ├── builtin/
│   │   ├── memory.py           # memory_search, memory_write, memory_delete
│   │   ├── graph.py            # graph_query, get_entity
│   │   ├── orchestrator.py     # spawn_task, get_task_result, list_tasks
│   │   ├── web.py              # web_search, fetch_url
│   └── (user-defined skills go here as .py files)
```

---

### 19.7 MCP Tool Additions for Agent Management

Two new MCP tools exposed by the MCP server:

#### `list_agents`

```json
{
  "name": "list_agents",
  "description": "List all registered agent definitions, including builtins and user-defined.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "filter": { "type": "string", "description": "Optional keyword to filter by name or description" }
    }
  }
}
```

#### `spawn_task` — extended with `agent` field

```json
{
  "name": "spawn_task",
  "inputSchema": {
    "type": "object",
    "properties": {
      "prompt":    { "type": "string" },
      "namespace": { "type": "string" },
      "runtime":   { "type": "string", "enum": ["claude-code", "api", "openrouter"] },
      "agent":     { "type": "string", "description": "Optional: force a specific named agent (e.g. 'code-reviewer')" },
      "timeout_s": { "type": "integer", "default": 300 }
    },
    "required": ["prompt", "namespace"]
  }
}
```

---

### 19.8 Phase 6 — Agents and Skills (Roadmap Addition)

| Item | Package | Effort |
|------|---------|--------|
| `@skill` decorator + auto-discovery loader | `skills` | S |
| Built-in skills (memory, graph, orchestrator, web) | `skills/builtin` | M |
| YAML agent definition schema + validator | `core` | M |
| Agent embedding + semantic matching | `core` | M |
| Community agent library (10 builtins) | `agents/builtin` | L |
| Critic-worker loop in orchestrator | `orchestrator` | M |
| `list_agents` MCP tool | `mcp-server` | S |
| External MCP skill pack loader | `core` | M |
| Agent management REST endpoints | `api` | S |
| Agent authoring guide | `docs` | S |

---

## 20. Self-Learning Architecture

### 20.1 Design Philosophy

Self-learning in engram means **the system improves its future behaviour based on past outcomes**. This is not model fine-tuning — we cannot retrain Claude. Instead, learning happens through four evidence stores in memory:

| Store | What it holds | Used when |
|-------|--------------|-----------|
| **Episodic store** | Raw task history: prompt, approach, result, outcome | Planner searches for similar past tasks |
| **Heuristic store** | Distilled rules derived from failures and corrections | Injected into planner context on every task |
| **Skill template store** | Step-by-step approaches extracted from successful tasks | Planner reuses when pattern matches |
| **Quality store** | Per-agent, per-task-type quality scores | Orchestrator routes to best-performing agents |

All four stores live in Neo4j and Qdrant — no separate database needed.

---

### 20.2 Data Models — Learning

#### EpisodicRecord

```python
@dataclass
class EpisodicRecord:
    id: str
    task_id: str
    namespace: str
    original_prompt: str
    decomposition: list[str]          # list of subtask prompts
    agent_used: str | None
    runtime: str
    outcome: Outcome                  # SUCCESS | FAILURE | CORRECTED
    user_feedback: str | None         # raw correction text if CORRECTED
    quality_score: float | None       # 0.0-1.0, set by critic or user feedback
    duration_s: float
    token_cost: int
    created_at: datetime
    tags: list[str]                   # auto-extracted topic tags
```

#### Heuristic

```python
@dataclass
class Heuristic:
    id: str
    namespace: str
    rule: str                         # plain-language rule sentence
    rationale: str                    # why this rule exists (from the failure)
    source_episode_id: str            # which EpisodicRecord it was derived from
    applies_to_tags: list[str]        # topic tags this rule is relevant for
    confidence: float                 # 0.0-1.0, decays over time if rule not triggered
    triggered_count: int              # how many times this rule was loaded
    overridden_count: int             # how many times the agent ignored it
    created_at: datetime
    last_triggered_at: datetime | None
```

#### SkillTemplate

```python
@dataclass
class SkillTemplate:
    id: str
    name: str                         # auto-generated slug
    namespace: str
    description: str                  # plain-language description of what this solves
    trigger_patterns: list[str]       # phrases that indicate this template applies
    steps: list[str]                  # ordered list of approach steps
    tools_used: list[str]
    avg_duration_s: float
    success_rate: float               # updated after each use
    source_episode_id: str
    created_at: datetime
    last_used_at: datetime | None
    use_count: int
```

#### QualityRecord

```python
@dataclass
class QualityRecord:
    agent_name: str
    task_tag: str                     # topic area (e.g. "azure-iam", "fhir", "code-review")
    namespace: str
    sample_count: int
    avg_quality_score: float
    avg_duration_s: float
    failure_rate: float
    last_updated: datetime
```

---

### 20.3 Mechanism 1 — Episodic Memory (Passive)

Every task stores an `EpisodicRecord` automatically after completion. This requires no user action.

```python
# orchestrator/orchestrator.py

async def run(self, task: str, namespace: str) -> str:
    episode = EpisodicRecord(
        id=uuid4(),
        task_id=task_id,
        namespace=namespace,
        original_prompt=task,
        decomposition=[st.prompt for st in subtasks],
        agent_used=selected_agent,
        runtime=config.runtime,
        outcome=Outcome.SUCCESS,
        duration_s=elapsed,
        token_cost=token_count,
        tags=await self.tag_extractor.extract(task)
    )
    await self.memory.add_episode(episode)
    return result
```

The planner queries past episodes before decomposing a new task:

```python
# orchestrator/planner.py

async def plan(self, task: str, namespace: str) -> list[SubTask]:
    # Find similar past tasks that succeeded
    past = await memory.search_episodes(
        query=task,
        namespace=namespace,
        outcome=Outcome.SUCCESS,
        top_k=3
    )
    past_context = format_episodes(past)

    # Load relevant heuristics
    heuristics = await memory.search_heuristics(query=task, namespace=namespace)
    heuristic_context = format_heuristics(heuristics)

    # Load matching skill templates
    template = await memory.match_skill_template(task, namespace)
    template_context = format_template(template) if template else ""

    prompt = PLANNER_PROMPT.format(
        task=task,
        past_context=past_context,
        heuristics=heuristic_context,
        template=template_context
    )
    return await llm.decompose(prompt)
```

**Planner system prompt template:**

```
You are a task decomposition planner.

TASK: {task}

SIMILAR PAST TASKS (use these as guidance, not prescription):
{past_context}

HEURISTICS (rules derived from past failures — follow these):
{heuristics}

APPROACH TEMPLATE (if applicable):
{template}

Break the task into 1-5 parallel subtasks. Return JSON array:
[{"id": "1", "prompt": "...", "agent": "optional-agent-name"}]
```

---

### 20.4 Mechanism 2 — Feedback Loop

User feedback is captured via three channels:

#### Channel A — Explicit (Telegram/WhatsApp)

```
User sends 👍 or 👎 after a task response
    │
    ▼
Gateway maps to: FeedbackSignal(task_id, signal="positive"|"negative")
    │
    ▼
LearningService.record_feedback(episode_id, signal)
    ├── positive → quality_score = 1.0, outcome = SUCCESS
    └── negative → quality_score = 0.0, outcome = CORRECTED, trigger reflection
```

#### Channel B — Correction detection

```python
# learning/feedback_detector.py

CORRECTION_PATTERNS = [
    r"\b(no|wrong|incorrect|that's not right|actually|wait)\b",
    r"\b(the correct .+ is)\b",
    r"\b(you missed|you forgot|you got that wrong)\b"
]

async def detect_correction(user_message: str, prior_task_id: str) -> bool:
    for pattern in CORRECTION_PATTERNS:
        if re.search(pattern, user_message, re.IGNORECASE):
            return True
    return False
```

When a correction is detected, the correction text itself is stored alongside the episode:

```python
episode.outcome        = Outcome.CORRECTED
episode.user_feedback  = user_message
episode.quality_score  = 0.2
await memory.update_episode(episode)
await reflection_service.reflect_on_correction(episode)
```

#### Channel C — REST API (for CI/CD / programmatic feedback)

```
POST /api/v1/feedback
{
  "task_id": "uuid",
  "signal": "positive" | "negative",
  "comment": "optional free-text explanation"
}
```

---

### 20.5 Mechanism 3 — Reflection Loop

The reflection loop is the core of autonomous self-improvement. It runs on a schedule and after detected corrections.

#### Trigger conditions

```yaml
# engram.yaml
learning:
  reflection:
    schedule: "0 2 * * *"          # nightly at 2am (cron)
    trigger_on_correction: true    # also runs immediately after any correction
    min_episodes_per_run: 5        # skip if fewer than 5 new episodes since last run
    lookback_days: 7
```

#### ReflectionAgent

```python
# learning/reflection.py

REFLECTION_PROMPT = """
You are a self-improvement agent for an AI orchestration system.

RECENT TASK OUTCOMES (last {lookback_days} days):
{episodes}

EXISTING HEURISTICS:
{existing_heuristics}

Analyse the failures and corrections. For each pattern you identify:
1. State the rule in one sentence (plain English, future-tense instruction)
2. State the rationale (which specific failure led to this rule)
3. List the topic tags this rule applies to

Also identify any existing heuristics that should be:
- Strengthened (pattern confirmed by multiple episodes)
- Weakened (pattern contradicted by successful counter-examples)
- Deleted (no longer relevant)

Respond in JSON:
{
  "new_heuristics": [
    {
      "rule": "...",
      "rationale": "...",
      "applies_to_tags": [...],
      "confidence": 0.0-1.0
    }
  ],
  "update_heuristics": [
    { "id": "...", "confidence_delta": +/-0.1, "reason": "..." }
  ],
  "delete_heuristic_ids": [...]
}
"""

class ReflectionAgent:

    async def run(self, namespace: str):
        episodes  = await memory.get_recent_episodes(namespace, days=self.lookback_days)
        heuristics = await memory.get_all_heuristics(namespace)

        response = await llm.generate(
            REFLECTION_PROMPT.format(
                lookback_days=self.lookback_days,
                episodes=format_episodes(episodes),
                existing_heuristics=format_heuristics(heuristics)
            )
        )

        updates = json.loads(response)
        await self._apply_updates(namespace, updates)

    async def _apply_updates(self, namespace: str, updates: dict):
        for h in updates["new_heuristics"]:
            await memory.add_heuristic(Heuristic(namespace=namespace, **h))

        for u in updates["update_heuristics"]:
            await memory.update_heuristic_confidence(u["id"], u["confidence_delta"])

        for hid in updates["delete_heuristic_ids"]:
            await memory.delete_heuristic(hid)
```

#### Heuristic confidence decay

Heuristics that are never triggered decay over time. This prevents the heuristic store from accumulating stale rules that no longer apply.

```python
# learning/decay.py  — runs weekly

async def decay_heuristics(namespace: str):
    heuristics = await memory.get_all_heuristics(namespace)
    for h in heuristics:
        days_since_trigger = (now() - (h.last_triggered_at or h.created_at)).days
        if days_since_trigger > 30:
            new_confidence = h.confidence * 0.9   # 10% decay per week after 30 days
            if new_confidence < 0.1:
                await memory.delete_heuristic(h.id)
            else:
                await memory.update_heuristic(h.id, confidence=new_confidence)
```

---

### 20.6 Mechanism 4 — Skill Template Extraction

After a task succeeds with high quality (score > 0.8), the SkillExtractor analyses whether the approach is worth capturing as a reusable template.

```python
# learning/skill_extractor.py

EXTRACTION_PROMPT = """
A task was completed successfully. Decide if it represents a reusable approach pattern.

TASK: {task}
APPROACH TAKEN: {decomposition}
TOOLS USED: {tools}
OUTCOME: success (quality score {score})

If this approach would be useful as a template for similar future tasks:
1. Write a one-sentence description of what problem type this solves
2. List 3-5 phrases that would indicate a future task matches this pattern
3. List the steps as a numbered approach guide

If the task is too specific or one-off, respond with: {"extract": false}

Respond in JSON:
{
  "extract": true,
  "description": "...",
  "trigger_patterns": [...],
  "steps": [...]
}
"""

class SkillExtractor:

    async def maybe_extract(self, episode: EpisodicRecord):
        if episode.quality_score < 0.8 or episode.outcome != Outcome.SUCCESS:
            return

        existing = await memory.match_skill_template(
            episode.original_prompt,
            episode.namespace,
            threshold=0.92   # only extract if no very similar template already exists
        )
        if existing:
            await self._update_template_stats(existing, episode)
            return

        response = await llm.generate(
            EXTRACTION_PROMPT.format(
                task=episode.original_prompt,
                decomposition="\n".join(episode.decomposition),
                tools=", ".join(episode.tools_used),
                score=episode.quality_score
            )
        )
        result = json.loads(response)
        if result.get("extract"):
            await memory.add_skill_template(SkillTemplate(
                namespace=episode.namespace,
                source_episode_id=episode.id,
                **{k: result[k] for k in ["description", "trigger_patterns", "steps"]}
            ))
```

---

### 20.7 Mechanism 5 — Critic-Worker Loop (Self-Refine)

The critic runs inline as part of task execution when `use_critic: true` in the agent definition.

```python
# orchestrator/critic.py

CRITIC_PROMPT = """
You are evaluating a draft response produced by an AI agent.

ORIGINAL TASK: {task}
DRAFT RESPONSE: {draft}
AGENT INSTRUCTIONS: {system_prompt}

{critic_instructions}

Respond with:
- "LGTM" if the response is accurate and complete.
- Otherwise, a numbered list of specific corrections needed.
Do not rewrite the response yourself. Only identify what needs fixing.
"""

class CriticWorker:

    async def evaluate(self,
                       task: str,
                       draft: str,
                       agent_def: AgentDefinition) -> CriticResult:

        response = await llm.generate(
            model=agent_def.critic_model,
            prompt=CRITIC_PROMPT.format(
                task=task,
                draft=draft,
                system_prompt=agent_def.system_prompt,
                critic_instructions=agent_def.critic_prompt
            )
        )

        if response.strip().upper() == "LGTM":
            return CriticResult(passed=True, corrections=None)
        return CriticResult(passed=False, corrections=response)


# In the orchestrator worker loop:
async def run_with_critic(self, task: str, agent_def: AgentDefinition) -> str:
    draft = await self.worker.run(task, agent_def.system_prompt)

    if not agent_def.use_critic:
        return draft

    critique = await self.critic.evaluate(task, draft, agent_def)
    if critique.passed:
        return draft

    # One revision pass
    revised = await self.worker.run(
        f"Revise your previous response based on this critique:\n{critique.corrections}\n\nOriginal task: {task}",
        agent_def.system_prompt
    )
    return revised
```

The critic always uses a cheaper/faster model (default: `claude-haiku-4-5`) to keep cost low. The revision adds one more worker call but significantly improves output quality for high-stakes tasks.

---

### 20.8 Quality Routing

Over time, the `QualityRecord` store accumulates per-agent, per-topic performance data. The orchestrator uses this to route tasks to the best-performing agent for a given topic.

```python
# orchestrator/router.py

async def select_best_agent(task: str, namespace: str) -> str:
    task_tags = await tag_extractor.extract(task)

    candidates = []
    for tag in task_tags:
        records = await memory.get_quality_records(tag, namespace)
        for rec in records:
            candidates.append((rec.agent_name, rec.avg_quality_score, rec.failure_rate))

    if not candidates:
        return None  # fall back to generic worker

    # Score: quality - (2 * failure_rate) — penalise failures heavily
    best = max(candidates, key=lambda c: c[1] - 2 * c[2])
    return best[0] if best[1] > 0.6 else None
```

---

### 20.9 Learning Configuration

```yaml
# engram.yaml — learning section

learning:
  enabled: true

  episodic:
    enabled: true                  # always store task history
    retention_days: 365            # delete episodes older than this

  feedback:
    correction_detection: true     # auto-detect corrections in user messages
    feedback_endpoint: true        # expose POST /api/v1/feedback

  reflection:
    enabled: true
    schedule: "0 2 * * *"         # cron: nightly at 2am
    trigger_on_correction: true    # also run after every detected correction
    min_episodes_per_run: 5
    lookback_days: 7
    model: claude-haiku-4-5        # use cheap model for reflection

  skill_extraction:
    enabled: true
    quality_threshold: 0.8         # only extract from high-quality outcomes
    similarity_threshold: 0.92     # don't extract if similar template already exists

  heuristic_decay:
    enabled: true
    schedule: "0 3 * * 0"         # weekly, Sunday 3am
    inactive_days_before_decay: 30
    decay_rate: 0.9                # multiply confidence by this each week

  quality_routing:
    enabled: true
    min_samples: 10                # need at least 10 episodes before routing kicks in
    quality_threshold: 0.6         # agent must exceed this score to be preferred
```

---

### 20.10 New MCP Tools for Learning

#### `get_heuristics`

```json
{
  "name": "get_heuristics",
  "description": "Retrieve the current heuristics engram has learned for a namespace.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "namespace": { "type": "string" },
      "query":     { "type": "string", "description": "Optional: filter by relevance to a topic" },
      "limit":     { "type": "integer", "default": 20 }
    },
    "required": ["namespace"]
  }
}
```

#### `add_heuristic`

```json
{
  "name": "add_heuristic",
  "description": "Manually add a heuristic rule to engram's learning store.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "rule":             { "type": "string" },
      "rationale":        { "type": "string" },
      "applies_to_tags":  { "type": "array", "items": { "type": "string" } },
      "namespace":        { "type": "string" }
    },
    "required": ["rule", "namespace"]
  }
}
```

#### `trigger_reflection`

```json
{
  "name": "trigger_reflection",
  "description": "Manually trigger the reflection loop for a namespace. Useful after a batch of corrections.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "namespace":    { "type": "string" },
      "lookback_days": { "type": "integer", "default": 7 }
    },
    "required": ["namespace"]
  }
}
```

---

### 20.11 Learning Data in Neo4j

```
// Episodic record node
(:Episode {id, namespace, prompt, outcome, quality_score, duration_s, created_at})

// Heuristic node
(:Heuristic {id, namespace, rule, rationale, confidence, triggered_count, created_at})

// Skill template node
(:SkillTemplate {id, namespace, description, steps[], success_rate, use_count})

// Relationships
(:Episode)-[:DERIVED_HEURISTIC]->(:Heuristic)
(:Episode)-[:GENERATED_TEMPLATE]->(:SkillTemplate)
(:Heuristic)-[:APPLIES_TO_TAG {tag}]->(:Tag)
(:SkillTemplate)-[:USED_TOOL {tool_name}]->(:Tool)
(:Episode)-[:USED_TEMPLATE]->(:SkillTemplate)
```

---

### 20.12 Phase 7 — Self-Learning (Roadmap Addition)

| Item | Package | Effort |
|------|---------|--------|
| `EpisodicRecord` model + storage | `core` | M |
| Episode write on every task completion | `orchestrator` | S |
| Planner — episode + heuristic context injection | `orchestrator` | M |
| Correction detection in gateway | `gateway` | M |
| Feedback endpoint (REST + Telegram 👍/👎) | `api` + `gateway` | M |
| `ReflectionAgent` + scheduled run | `learning` | L |
| Heuristic CRUD in memory core | `core` | M |
| Heuristic confidence decay job | `learning` | S |
| `SkillExtractor` + template CRUD | `learning` | L |
| Critic-worker loop in orchestrator | `orchestrator` | M |
| `QualityRecord` store + quality routing | `orchestrator` | M |
| New MCP tools (get_heuristics, add_heuristic, trigger_reflection) | `mcp-server` | S |
| Neo4j schema additions (Episode, Heuristic, SkillTemplate) | `core` | S |
| Learning config section in engram.yaml | `core` | S |
| Learning admin dashboard (optional) | `api` | L |

---

## Updated Repository Structure (Sections 19–20 additions)

```
engram/
├── agents/
│   ├── builtin/             (10 pre-built agent YAML definitions)
│   └── (user agents here)
│
├── skills/
│   ├── __init__.py          (@skill decorator)
│   ├── loader.py
│   └── builtin/
│       ├── memory.py
│       ├── graph.py
│       ├── orchestrator.py
│       └── web.py
│
├── packages/
│   ├── core/
│   │   └── engram/
│   │       ├── agents/
│   │       │   ├── registry.py        agent discovery + embedding
│   │       │   └── matcher.py         semantic task→agent matching
│   │       └── learning/
│   │           ├── models.py          EpisodicRecord, Heuristic, SkillTemplate, QualityRecord
│   │           ├── episode_store.py   CRUD for episodic memory
│   │           ├── heuristic_store.py CRUD for heuristics
│   │           ├── skill_store.py     CRUD for skill templates
│   │           └── quality_store.py   CRUD for quality records
│   │
│   ├── orchestrator/
│   │   └── engram_orchestrator/
│   │       ├── critic.py              CriticWorker
│   │       ├── router.py              quality-based agent routing
│   │       └── learning/
│   │           ├── reflection.py      ReflectionAgent
│   │           ├── extractor.py       SkillExtractor
│   │           ├── feedback.py        FeedbackDetector
│   │           └── decay.py           heuristic decay job
│   │
│   └── gateway/
│       └── engram_gateway/
│           └── feedback_handler.py    👍/👎 + correction detection
```

---

**Total Phase 1-7 (solo developer):** ~12-14 weeks  
**Total Phase 1-3 only (working orchestrator, no learning):** ~3-4 weeks  
**Suggested order:** Build Phases 1-3 first (working system), then add Phase 6 (agents/skills), then Phase 7 (self-learning) — each phase ships independently useful functionality.

---

## 22. Architectural Improvements — Build Status

> Sourced from architecture review 2026-05-23. These are additions to the core design that make
> engram genuinely useful for the "new engineer touches PaymentService on week one" scenario.

### 22.1 Tier 1 — Foundation (Highest Leverage)

#### 22.1.1 Decision Record Memory Type ✅ COMPLETE

`MemoryEntry` carries `memory_type` (fact/decision/constraint/incident/adr/skill), `status`
(active/superseded/deprecated/proposed), `author`, `affects: list[str]`, and `rationale`.

On every write with `affects`, AFFECTS edges are created from the Memory vertex to Entity vertices.
`write_decision()`, `write_constraint()`, `write_incident()` are convenience methods on `EngramClient`.

#### 22.1.2 Constraint Memory Injection ✅ COMPLETE

All active `memory_type=constraint` records for a namespace (and its ancestors) are fetched via
`get_constraints()` and prepended to every `memory_search` result under the `⚠ ACTIVE CONSTRAINTS`
header — before the scored vector results, never subject to top_k competition.

#### 22.1.3 Decision Pinning ✅ COMPLETE (2026-05-23, commit c901015)

When `search()` is called, entity names are extracted from the query via:
- CamelCase pattern (`PaymentService`)
- ALLCAPS (`JWT`, `API`)
- snake_case (`payment_service`)
- kebab-case (`payment-service`)
- spaCy NER

`get_decisions_for_entities(entity_names, namespace)` in `arcadedb_client.py` fetches all active
decision/constraint/ADR memories whose `affects` list overlaps, checking the full namespace ancestry.
These are prepended as `SearchResult(source="pinned", score=2.0)` — above all scored results.

The MCP formatter renders them under `📌 PINNED` with type, affects, author, rationale, and ID.

#### 22.1.4 Git Integration ✅ COMPLETE

`engram-git` CLI (entry point: `engram.cli.git_hooks:main`):
- `engram-git install` — installs a `post-commit` hook into `.git/hooks/`
- `engram-git post-commit` — writes commit SHA, author, message, files to engram
- `engram-git pre-review` — given a PR diff, retrieves relevant memories as context
- `engram-git post-incident-merge` — parses `INCIDENT_ID:`, `ROOT_CAUSE:`, `RESOLUTION:`, `AFFECTED_SERVICES:` fields from incident branch merge commits and writes a typed `memory_type=incident` memory

---

### 22.2 Tier 2 — Team Collaboration

#### 22.2.1 Namespace Subscriptions ✅ COMPLETE

Subscription CRUD, cursor-based feed polling, `filter_types` in feed query, and cross-namespace fan-out are all built and tested.

- `Subscription` vertex with composite index in ArcadeDB
- `POST /subscriptions/` — subscribe; `GET /subscriptions/{ns}/feed` — cursor poll; `DELETE /subscriptions/{ns}` — unsubscribe
- `filter_types` list applied in `get_feed()` SQL query (not just stored)
- Fan-out: `delivery_namespace` on `Subscription` → fire-and-forget copy on `insert_memory()`
- MCP tools: `namespace_subscribe`, `namespace_feed`, `unsubscribe`

**Remaining gap:** delivery modes `webhook` and `immediate` — currently only polling exists.

#### 22.2.2 Memory Provenance ✅ COMPLETE

`Provenance(agent_id, user_id, tool, session_id, git_commit, jira_ticket)` on every `MemoryEntry`.
Stored as MAP in ArcadeDB, reconstructed on read. Wired through MCP write tools.

#### 22.2.3 Contradiction Detection ✅ COMPLETE

Fires after every write via `check_contradictions()` (cosine similarity ≥ 0.88 against top-5 similar
memories). Returns `contradiction_warnings` in MCP response. Non-blocking.

Directional/negation logic added: `_negated_phrases()` and `_affirmed_phrases()` extract stance polarity from both the new memory and candidates. When stances conflict (`affirmed` vs. `negated` for the same concept), the warning is tagged with `direction="contradiction"` rather than `direction="similar"`.

---

### 22.3 Tier 3 — Intelligence Layer

#### 22.3.1 Incident Intelligence ✅ COMPLETE

`memory_type=incident` and `write_incident()` exist. Incidents are searchable via standard vector search.

- **Webhook receiver** (`POST /api/v1/webhooks/incident`) accepts PagerDuty and AlertManager payloads, normalises them, and writes a typed incident memory
- **`SIMILAR_TO` edges** connecting incidents with cosine similarity ≥ 0.75 (written at webhook receipt time)
- **`RESOLVED_BY` edges** linking an incident memory to the config-change or file memory that resolved it (written when a `resolution_memory_id` is supplied in the webhook payload or via MCP)

**Remaining gap:** automatic past-incident retrieval on alert trigger (proactive context injection when a new alert matches a known incident pattern).

#### 22.3.2 Knowledge Health Metrics ✅ COMPLETE

`GET /api/v1/knowledge/health?namespace=<ns>` returns a composite 0–100 health score:

| Metric | Deduction | Cap |
|--------|-----------|-----|
| Unused active constraints (never retrieved) | 3 pts each | 15 |
| Namespaces with no writes in 90+ days | 5 pts each | 20 |
| Overdue review memories (`review_by < now`) | 2 pts each | 20 |
| Memories approaching expiry (< 7 days) | 1 pt each | 10 |

Also returns raw counts for dashboard display. Tests: `tools/test_knowledge_health.py`.

#### 22.3.3 Memory Expiry and Decay Contracts ✅ COMPLETE

- `expires_at` — hard expiry, excluded from search
- `review_by` + `memory_review_due` MCP tool — returns stale decisions past their review date
- `decay_policy` field (`none` / `time_weighted` / `access_weighted`) on `MemoryEntry`
- Post-search decay: scores multiplied by time/access decay factor before ranking; memories with `decay_policy=none` are unaffected
- Weekly CLI job: `engram-decay run` (`engram.cli.decay_cli:main`) — processes all active memories with non-none decay policy

---

### 22.4 Infrastructure Additions ✅ COMPLETE

#### 22.4.1 External MCP Skill Pack Loader

`packages/mcp-server/engram_mcp/skill_packs.py`:
- Drop a `.yaml` or `.json` file in `ENGRAM_SKILL_PACKS_DIR`
- Server loads all packs at startup and registers their tools alongside built-in MCP tools
- Only `webhook` handler type supported: incoming MCP calls are forwarded as `POST {"tool": name, "arguments": args}` to the configured URL
- Name collisions with built-ins are skipped with a warning

#### 22.4.2 Qdrant Vector Backend

`packages/core/engram/storage/qdrant_backend.py` implements the `VectorBackend` ABC:
- Activated via `ENGRAM_VECTOR_BACKEND=qdrant` in `.env`
- `create_vector_backend()` factory in `vector_backend.py` returns `None` (ArcadeDB default) or `QdrantVectorBackend`
- Install: `pip install 'engram-core[qdrant]'`
- Docker: uncomment the `qdrant` profile service in `docker-compose.yml`
- Recommended when namespace exceeds ~100K memories; below that the numpy cosine similarity path (<15ms) is sufficient

#### 22.4.3 Learning Admin Dashboard

`packages/api/engram_api/routers/learning_admin.py`:
- `GET /api/v1/learning/dashboard` — inline HTML dashboard
- `GET /api/v1/learning/stats?ns=<ns>` — heuristic count, episode count (7d), avg quality, success rate, top agents
- `GET /api/v1/learning/heuristics?ns=<ns>` — sorted by confidence descending
- `DELETE /api/v1/learning/heuristics/{id}` — delete a heuristic
- `GET /api/v1/learning/episodes/recent?ns=<ns>` — newest-first, long prompts truncated to 300 chars
- `POST /api/v1/learning/reflect` — trigger reflection run
- Graceful 503 degradation if `engram_learning` is not installed

---

### 22.5 Remaining Gaps

| Item | Notes |
|------|-------|
| Subscription delivery modes `webhook` / `immediate` | Currently only cursor polling exists |
| Automatic past-incident retrieval on alert | Proactive context injection on new alert matching known pattern |

---

---

## 21. AI Governance Positioning

### 21.1 The Problem engram Solves for Enterprise Engineering Teams

Traditional engineering teams manage knowledge through disconnected silos:

```
PM writes spec in Confluence ────────────────────────────────────┐
Architect makes decision in Slack ───────────────────────────┐   │
Developer builds what they remember ─────────────────────┐   │   │
AI agent generates code from 2024 training data ─────┐   │   │   │
                                                      ↓   ↓   ↓   ↓
                                             Four different realities.
                                             No single source of truth.
```

When AI agents enter the picture, this problem compounds: the agent has no knowledge of the organization's actual decisions, patterns, constraints, or current state. It either hallucinates or produces code that contradicts established choices.

### 21.2 engram as Governance Infrastructure

engram becomes the **single queryable source of organizational truth** that all AI agents must consult before acting:

```
All decisions, specs, patterns, standards → engram (timestamped, namespace-scoped)
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         PM agent    Dev agent   Arch agent
              │          │          │
    "What's the      "What       "What's the
    current auth    JWT config   approved
    spec?"          do we use?"  DB pattern?"
              │          │          │
              └──────────┴──────────┘
                         │
                 engram memory_search
                         │
              [CURRENT] JWT with 24h expiry,
              RS256 signing, refresh token
              rotation. Decided 2026-04-15.
              (Supersedes 2025-12 HS256 decision)
```

The developer's AI agent cannot go off-specification because the specification is available in real time, versioned, and the agent is instructed to query it before generating code.

### 21.3 What AI Governance Requires

| Governance Layer | What it does | How engram provides it |
|---|---|---|
| **Knowledge accuracy** | 85%+ correct org facts in the graph | spaCy entity extraction + human writes + quality scoring |
| **Knowledge freshness** | Agents use current decisions, not stale ones | UTC timestamps + recency weighting + superseded_at |
| **Decision attribution** | Who decided X, when, why | `created_by` + `created_at` on every memory |
| **Namespace scoping** | PM knowledge ≠ engineering ≠ compliance | Hierarchical namespaces with ACL keys |
| **Audit trail** | Every agent query logged: who asked, what retrieved | Query log per API key |
| **Conflict detection** | Two memories contradict → surface for human | Semantic similarity on new writes; flag near-duplicates with different content |
| **Knowledge gap flags** | Search returned nothing → human should write it | Empty search result logging → gap report |
| **Citation enforcement** | Agent must cite which knowledge informed output | memory_search returns IDs; agent cites them in output |

### 21.4 Governance Workflow for a Developer Team

```
1. PM writes product spec → memory_write(namespace="org:acme:product")
2. Architect writes decision → memory_write(namespace="org:acme:engineering")
3. Developer asks AI agent to build feature
   AI agent calls memory_search before generating code:
     → finds: "Use HAPI FHIR 7.x, JWT 24h expiry, pagination via cursor"
     → generates code that follows these constraints
     → cites: "Based on decisions: [id-abc, id-def, id-ghi]"
4. If decision changes:
     → write new memory with updated decision
     → set superseded_at on old memory
     → next agent query returns [CURRENT] new decision + [HISTORICAL] old one
5. Audit: "What knowledge did the AI use when building this feature on 2026-05-20?"
     → query audit log by date + namespace → full trace
```

### 21.5 Knowledge Quality Target: 85%+

To be useful for governance, the knowledge graph must be accurate. The path to 85%+ accuracy:

1. **Human-written memories are the ground truth** — agents write what humans tell them; humans correct errors
2. **Conflict detection** — when a new write is semantically similar to an existing memory but contradicts it, surface both to the human for resolution before writing
3. **Supersession logging** — every time a fact is superseded, log: what changed, who changed it, why (from the new memory content)
4. **Staleness warnings** — memories older than a configurable threshold (default: 180 days) without any update or query hit get flagged: "This knowledge has not been confirmed recently"
5. **Query hit tracking** — memories that are frequently retrieved and acted on are more likely to be accurate; low-hit memories get lower confidence over time

---

## 22. Binary Asset Handling

### 22.1 Design Principle

Binaries (diagrams, PDFs, Word documents, images) are **never stored in the knowledge graph**. The graph stores:
1. A **reference node** pointing to where the binary lives (path or URL)
2. The **content hash** (SHA-256) for change detection
3. **Extracted text content** from the binary — this IS vector-embedded and searchable

The binary itself stays in its natural location: a git repository, a shared drive, a local file system.

### 22.2 The Asset Reference Node

```
(Decision: "Microservices architecture for auth layer")
       │
       └─[DOCUMENTED_IN]──▶ (Asset {
             id:                "asset-auth-arch-v3"
             path:              "~/vaults/hc/diagrams/auth-service-v3.drawio"
             git_url:           "gitlab.com/acme/docs/-/blob/main/arch/auth-v3.drawio"
             format:            "drawio"
             sha256:            "a3f9c2d8..."
             extracted_content: "Auth Service → Token Validator → JWT Store
                                  Auth Service → Rate Limiter → Redis Cache
                                  External Client → [HTTPS/443] → Auth Service
                                  Token Validator → [reads] → Public Key Store"
             namespace:         "org:acme:engineering"
             created_at:        2026-05-20T14:32:00Z
             created_by:        "alice"
             superseded_at:     null
         })
```

The `extracted_content` field is what makes the asset searchable. A developer asking "show me the auth architecture" → vector search on extracted content → returns the Asset node → includes the file path → agent can open or display the file.

### 22.3 Format-Specific Extraction

| Format | Extraction method | What is extracted |
|---|---|---|
| `.drawio` | XML parse (draw.io is XML) | All node labels, edge labels, group names |
| `.pdf` | PyMuPDF text extraction | Full text content, headings |
| `.png`, `.jpg`, `.svg` | Vision model (optional) or OCR | Alt-text, OCR text, or vision description |
| `.docx`, `.xlsx` | python-docx / openpyxl | Text content, sheet names, headings |
| `.md`, `.txt` | Direct read | Full text |

Extraction runs **without an LLM** for all structured formats. Vision-based extraction for images is optional and only needed if you want semantic understanding of diagrams (e.g., "this screenshot shows the login flow").

### 22.4 Change Detection via Hash

When a binary changes, the knowledge graph must stay current:

```python
async def sync_asset(path: str, namespace: str) -> AssetReference:
    current_hash = sha256_file(path)
    existing = await client.get_asset_by_path(path, namespace)

    if existing and existing.sha256 == current_hash:
        return existing  # no change — nothing to do

    # File changed — create new asset version
    content = extract_content(path)
    new_asset = await client.add_asset(
        path=path,
        format=detect_format(path),
        sha256=current_hash,
        extracted_content=content,
        namespace=namespace,
    )

    # Supersede old version (preserves history)
    if existing:
        await client.supersede(existing.id, namespace)
        # Migrate DOCUMENTED_IN edges to new asset
        await client.migrate_edges(existing.id, new_asset.id)

    return new_asset
```

The sync job can run:
- As a **pre-commit git hook** (fast, triggered on every commit)
- As a **scheduled background job** (periodic, configurable)
- **On-demand** via the REST API (`POST /api/v1/assets/sync`)

### 22.5 Asset Registration via MCP

AI agents can register assets directly during a session:

```
# In a Claude Code session after generating a diagram:
Use memory_write to record:
  content: "Created auth service architecture diagram showing microservices breakdown.
            Architect: alice. Decision date: 2026-05-20."
  namespace: "org:acme:engineering"
  tags: ["architecture", "auth", "diagram"]

Use asset_register:
  path: "~/vaults/hc/diagrams/auth-service-v3.drawio"
  namespace: "org:acme:engineering"
  related_memory_id: <the id from memory_write above>
```

The `asset_register` MCP tool extracts content, hashes the file, creates the Asset node, and creates the `DOCUMENTED_IN` edge from the memory to the asset — all in one call.

### 22.6 Asset MCP Tools

Two additional MCP tools for asset management:

**`asset_register`**
```json
{
  "name": "asset_register",
  "description": "Register a binary file (diagram, PDF, image) in the knowledge graph. Extracts and indexes the content. Creates a reference node — the binary is NOT copied.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path":              { "type": "string", "description": "File path or git URL" },
      "namespace":         { "type": "string" },
      "related_memory_id": { "type": "string", "description": "Optional: memory this asset documents" },
      "description":       { "type": "string", "description": "Optional: human-readable description" }
    },
    "required": ["path", "namespace"]
  }
}
```

**`asset_search`**
```json
{
  "name": "asset_search",
  "description": "Search registered assets by content. Returns file paths and extracted content summaries.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query":     { "type": "string" },
      "namespace": { "type": "string" },
      "format":    { "type": "string", "description": "Optional: filter by format (drawio, pdf, png)" }
    },
    "required": ["query", "namespace"]
  }
}
```

---

*End of engram Design Document v0.2*
