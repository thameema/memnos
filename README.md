# memnos

**Self-hostable, governed, vendor-neutral memory for AI agents — on one PostgreSQL.**

memnos gives AI agents long-term memory that persists across sessions, with **governance
built in** (token auth, namespace ACL, audit, an encrypted secret vault) and **no vendor
lock-in**. It runs on a single **PostgreSQL + pgvector** database — no second vector store,
no graph database — and uses **no LLM at query time** (retrieval is hybrid search + a local
cross-encoder reranker). Works with **Claude Code, Cursor, Windsurf** and any MCP client,
plus a REST API and a cross-platform CLI.

> **Status:** POC / preview. Single-org self-host, local-first. Apache-2.0.

```
Claude Code ─┐
Cursor       ├─ MCP (stdio) ─┐
Windsurf     ─┘              │
hooks (auto) ───────────────┼─► memnos server ──► PostgreSQL + pgvector  (ONE engine)
REST / CLI ─────────────────┘     ├─ hybrid retrieve: pgvector (HNSW) + BM25 (tsvector) → RRF
                                   │   → cross-encoder rerank → quota + timeline + entity arms
                                   │   (NO LLM at query time)
                                   ├─ bi-temporal facts + belief-change supersession
                                   ├─ governance: token auth · namespace ACL · audit · usage
                                   └─ encrypted secret vault (AES-256-GCM) + ingest redaction
```

---

## Why memnos (vs Graphiti / Mem0)

| | **memnos** | Graphiti (Zep) | Mem0 |
|---|---|---|---|
| Store | **one** Postgres + pgvector | Neo4j graph DB | vector DB (+ optional graph) |
| Conflict handling | **deterministic** bi-temporal supersession | LLM edge-invalidation | LLM ADD/UPDATE/DELETE |
| Query-time LLM | **none** | none | none |
| Reranking | **cross-encoder** | RRF / node distance | vector score |
| Governance | **token + namespace ACL + audit + usage ledger** | — | — |
| Secrets | **encrypted vault + ingest redaction** | — | — |
| Deploy | **one container / pip install**, runs anywhere | graph DB required | cloud or self-host |
| License | **Apache-2.0** | — | — |

**The essence:** memnos is a *governed memory engine*, not an agent runtime. Conflicts are
resolved deterministically (no LLM guessing at write time), retrieval is hybrid + reranked,
everything is bi-temporal and audited, and it all runs on one Postgres you already know how
to operate.

---

## Quickstart (local)

**Prerequisite:** a PostgreSQL with the `pgvector` extension available. memnos does **not**
install Postgres — it connects to yours. (For local dev: `docker compose -f
poc/docker-compose.poc.yml up -d`.)

```bash
cd poc
./install.sh          # macOS/Linux   (Windows: .\install.ps1)  → installs the `memnos` command
memnos setup          # enter your Postgres connection → creates schema + an admin token
memnos serve          # start the server → open http://127.0.0.1:8900/admin
```

`memnos --help` covers everything: `setup serve token grant principal namespace secret
stats health whoami ns remember recall`. Config (DSN, vault key, port) lives in
`~/.memnos/config.json`. Full walkthrough: [`poc/QUICKSTART.md`](poc/QUICKSTART.md).

An OpenAI key (in `.env` or `memnos secret set openai` → `OPENAI_API_KEY=secret://openai`)
enables 1536-d embeddings + fact extraction. Without it, memnos runs in free **local 384-d**
mode (embeddings only, no extraction). memnos **never holds your LLM key in plaintext** —
it stays in `.env` or the encrypted vault.

---

## Integrations

One command wires memnos into your agent — no manual config editing:

```bash
memnos claude-setup            # Claude Code: MCP + hooks (auto recall/save) + /memnos + CLAUDE.md
memnos agent-setup codex       # Codex CLI (MCP via ~/.codex/config.toml + AGENTS.md)
memnos agent-setup cursor      # Cursor
memnos agent-setup windsurf    # Windsurf
memnos agent-setup claude-desktop
```

Each mints a scoped token, is idempotent, and backs up edited files; `memnos setup` runs
`claude-setup` automatically when it detects Claude Code. **Claude Code** is the only agent
with lifecycle **hooks** (auto-recall before each prompt, auto-save after); every other agent
gets the memnos MCP **tools** (`recall`, `recall_wide`, `remember`, `reconcile_claim`, …).

- **REST** — `POST /remember`, `POST /recall` (Bearer token, namespace-scoped).
- **CLI / SDK** — `memnos remember/recall`, or `pip install memnos-sdk` (LangChain / LangGraph
  / LlamaIndex adapters).
- Full client guides: [`docs/guides/clients/`](docs/guides/clients/README.md).

REST, MCP, hooks and the benchmark all run the **same engine** (`MemnosMemory`) — there is
one codebase, not a benchmarked copy and a shipped copy.

---

## Management console + governance

A zero-build web console ships in the open-source build at **`/admin`** (create namespaces,
mint/revoke tokens, manage grants, view the dashboard, store secrets). Every call is
token-authenticated, namespace-ACL'd, and audited. (SSO/OIDC, advanced RBAC, multi-tenant
control plane, and the richer enterprise UI are the commercial layer.)

```bash
memnos admin          # bootstrap an admin token → paste into /admin
```

---

## Benchmarks — LoCoMo (and how we report it)

**58% under an independent cross-provider judge / 61% under the gpt-4o judge** on the full
LoCoMo benchmark (10 conversations, 1,542 QA).

We care more about *credibility* than a big headline:

- **Setup:** full 10 conversations. Ingest → bi-temporal SPO fact extraction (gpt-4o-mini) +
  consolidation; retrieve via hybrid (pgvector + BM25, RRF) + cross-encoder rerank +
  timeline / entity-guarantee arms — **no LLM at query time**; answer with the calling agent
  (GPT-5-mini in our run); judge with an LLM.
- **Independent judging:** most published numbers are *self-judged* (the same vendor's model
  grades its own answers). We additionally score under an **independent provider's judge**
  (Claude grading GPT answers) to remove self-preference bias.
- **Judge transparency:** the score is judge-sensitive. On the *same answers* we measure a
  **strict ~44% / standard 58-61% / lenient 85-88%** band — so you can see how much the
  judge prompt moves any number.
- **On comparisons:** headlines elsewhere (~66% Mem0, ~73% Mnemory, 90%+ others) are
  typically self-judged and sometimes on a *different* benchmark (e.g. DMR, not LoCoMo). We
  don't claim parity — we publish a reproducible harness.

**Reproduce:** `cd poc && python cross_provider_eval.py --sample-ids 0,1,2,3,4,5,6,7,8,9`
(see [`poc/LOCKED_BASELINE.md`](poc/LOCKED_BASELINE.md)).

*We'd rather report a credible 58% under an independent judge than an inflated 85% under a
lenient one.*

---

## How it works

**Write** (LLM at ingest only): a message becomes a verbatim raw turn **and** structured
bi-temporal SPO facts. Single-valued attributes (`lives_in`, `works_at`) supersede on
change; multi-valued ones (`did`, `visited`) accumulate. Secrets are redacted before
storage. An offline "sleep" pass consolidates facts into entity dossiers (multi-hop
pre-join).

**Read** (no LLM): the query runs hybrid retrieval (pgvector HNSW + BM25 via tsvector, fused
with RRF), a cross-encoder reranks, and quota retrieval guarantees raw-turn + fact coverage.
Temporal questions add a guaranteed entity **timeline**; entity questions add an
**entity-guarantee** arm so list/aggregation answers are complete.

---

## Security & operations

- **Auth:** opaque bearer tokens (SHA-256 hashed at rest; instantly revocable — not JWTs).
- **ACL:** every read/write is clamped to the principal's namespace grants.
- **Audit + usage ledger:** who/what/when + per-op LLM cost.
- **Secret vault:** AES-256-GCM, value-refs (`secret://name`), key rotation.
- **Redaction:** secret-shaped text is stripped from remembered messages before storage.
- **Health heuristic:** `memnos health` turns metrics into actionable findings.

> Local-first: the server binds `127.0.0.1`. Put a TLS reverse proxy in front for remote use.

---

## License

Apache-2.0. The open-source build is the engine + single-org self-host + the basic
management console. SSO/advanced RBAC, encrypted-vault key management (KMS/HSM, rotation
policies), the multi-tenant control plane, the richer enterprise UI, and managed cloud are
the commercial layer.
