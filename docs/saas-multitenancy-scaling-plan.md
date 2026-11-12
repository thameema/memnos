# memnos as hosted SaaS: multi-tenancy & horizontal scaling plan

Status: proposal / not started. Captures the architecture direction discussed for turning
memnos into a hosted "memory as a service" offering with multi-tenancy and autoscaling.
Not a commitment to a timeline — staged so each step ships value on its own.

## Context

memnos today runs as a single Postgres+pgvector instance, with namespace-scoped ACLs already
in the data model, and a gateway process that mixes a stateless API surface with in-process
background threads (consolidation, decay, corpus-ingest, the update-check loop) and a
lease-based concurrency mechanism (`lease_acquire`/`lease_heartbeat`/`lease_release`).

Goal: offer memnos as a multi-tenant hosted service with autoscaling and horizontal
scalability, without losing the relational/graph/governance model (entities, relationships,
provenance, namespace ACLs, constraints) that's core to memnos's positioning
("AI forgets you, memnos remembers").

**Decision already made and out of scope here:** do not move off Postgres+pgvector to
parquet files + a separate open-source vector DB. Postgres is the right engine for memnos's
actual workload (relational joins across entity/episode/provenance/namespace, transactional
multi-writer mutation, RLS-based access control) — see rationale below. The scaling problem
is tenancy and horizontal capacity, not the storage engine.

### Why not parquet + a dedicated vector DB

- memnos is a knowledge graph plus vector search, not pure vector search. Entities,
  relationships, provenance, and namespace ACLs need relational joins living next to the
  vector index — splitting them means reconciling two sources of truth on every write.
- memnos is live and mutable (consolidation, decay, contradiction-checking, leased
  concurrent writers), not a write-once analytical dataset. Parquet is a columnar format for
  immutable/batch data; the row-level updates, transactions, and locks memnos needs would
  mean bolting a real database on top of it anyway.
- Namespace ACLs are a relational access-control concern (RLS, foreign keys, transactions) —
  trivial in Postgres, awkward to recreate consistently across two stores.
- Dedicated vector DBs (Qdrant/Weaviate/Milvus/LanceDB) earn their keep at scales and
  ANN-tuning needs (100M+ vectors, GPU indexes, aggressive quantization) memnos isn't near.
  The known perf issues (see Known bugs below) are query/index-tuning bugs in the current
  Postgres setup, not evidence Postgres is the wrong engine.
- If memnos ever does outgrow pgvector, the right move is Postgres-as-system-of-record with
  a dedicated vector index as a sidecar synced from Postgres — one source of truth, add a
  specialized index only where profiling proves it's needed.

## Architecture direction

### 1. Tenant isolation — shared schema + Row-Level Security

Extend the existing namespace-scoped ACL model into Postgres RLS keyed on
`tenant_id`/namespace, applied to every table (episodes, entities, provenance, vectors).
This scales to thousands of tenants in one cluster at low marginal cost per tenant.

Reserve dedicated-schema or dedicated-cluster isolation as a paid "enterprise/dedicated"
tier for tenants needing hard isolation or their own compliance boundary — not the default;
it doesn't scale operationally as the base tier.

### 2. Split stateless API from stateful background work

Running N replicas of the current gateway duplicates its in-process background threads
(consolidation, decay, update-check loop) across every replica. Required split:

- **API/MCP layer**: made fully stateless, horizontally autoscaled (e.g. k8s HPA on request
  concurrency).
- **Worker pool**: consolidation/decay/corpus-ingest moved to a separate deployment that
  claims work via the existing lease mechanism (Postgres advisory locks or a jobs table), so
  exactly one worker processes a given tenant/namespace's background job at a time, scaled
  independently by queue depth.

### 3. Horizontal DB scale — Citus, sharded by tenant

Postgres alone doesn't horizontally scale writes. Citus (a Postgres extension, pgvector-
compatible) shards by `tenant_id` so each tenant's rows — including their vectors —
co-locate on one shard: joins and RLS stay cheap within a shard, and capacity is added by
adding worker nodes.

Alternative with less operational burden: a managed autoscaling Postgres (Neon, Aurora
Serverless) gives compute elasticity without re-architecting, but doesn't provide true
write-sharding — viable until a single tenant or overall write volume outgrows one primary.

### 4. Read replicas before sharding

Recall/search is almost certainly the dominant traffic pattern. Before Citus, a primary +
read replicas (route `recall`/`graph_query` to replicas, writes to primary) buys significant
headroom for much less change than sharding.

### 5. Per-tenant quotas and noisy-neighbor protection

Extend the existing `BudgetExceeded` mechanism into per-tenant rate limits / resource caps
enforced at the API layer. Ensure the Citus shard key (`tenant_id`) prevents one large
tenant's data from skewing a shard shared with others.

### 6. Embeddings as an independent scaling axis

Embedding generation is an external/expensive call, not a DB concern. Give it its own
stateless, independently-autoscaled worker tier, with `QueryEmbedCache` made tenant-aware
and shared where safe.

## Supporting billions of facts in PostgreSQL (`1B–5B+` scale specification)

PostgreSQL can support billions of facts in a multi-tenant SaaS provided the schema and
indexes are designed around the primary physical constraint of vector search: **never put
1 billion `float32` vectors into a single monolithic in-memory HNSW index.**

### 1. Storage and RAM math for 1 billion facts (`1536-d`)

| Metric | Unpartitioned `vector(1536)` (`float32`, Single Index) | Multi-Tenant PG (`halfvec(1536)` / `bit(1536)` + Partitioned Partial HNSW) |
|---|---|---|
| **Raw Vector Size per Fact** | `6,144 bytes` (`1536 × 4B`) | `3,072 bytes` (`halfvec` `fp16`) or `192 bytes` (`bit` binary) |
| **1B Facts Raw Vector Storage** | `~6.14 TB` | `~3.07 TB` (`halfvec`) / `~192 GB` (binary index) |
| **HNSW Graph + B-Tree + Heap (1B rows)** | `~9–11 TB` total disk | `~2.2–3.8 TB` total disk (with active-only partial index) |
| **RAM Required if 1 Monolithic Index** | **`~7–8 TB RAM`** (infeasible on 1 node) | **Only active tenant partitions paged in (`32–128 GB RAM` per worker)** |

In `memnos`, no query searches all 1 billion facts globally. Every `/recall`, `/reconcile`,
and `/graph` query is strictly scoped to **one tenant's `namespace` (plus its explicitly
linked namespaces)**. Even across `1B–5B` facts spanning thousands of tenants, only active
tenants' partitions are resident in `shared_buffers` / OS page cache during any given window.

### 2. Five index & schema requirements for billion-fact scale

1. **Partition-Local HNSW Indexes (`PARTITION BY HASH (tenant_id)`):**
   - Partition `semantic`, `episodic`, `raw_turns`, `entities`, and `edges` by `tenant_id`
     (e.g., 128–512 hash partitions per Citus shard, or dedicated sub-partitions for
     enterprise tenants).
   - Postgres builds a **separate, bounded HNSW index per partition**. Active tenants'
     HNSW partitions stay hot in RAM; idle tenants' partitions remain on NVMe SSD at zero
     RAM cost.

2. **Partial HNSW Indexing on Live Facts Only (`WHERE valid_to IS NULL`):**
   - Because `memnos` is bi-temporal, superseded rows (`valid_to IS NOT NULL`,
     `superseded_by IS NOT NULL`) remain in `semantic` for provenance and audit. Over time,
     60–80% of rows are historical.
   - Define the primary ANN index as a partial index over live facts only:
     ```sql
     CREATE INDEX ON semantic USING hnsw ((embedding::halfvec(1536)) halfvec_cosine_ops)
     WHERE valid_to IS NULL;
     ```
   - This reduces live HNSW index footprint by **3×–5×** and accelerates standard `/recall`.

3. **Vector Compression (`halfvec(1536)` `fp16` and Binary Quantization `bit(1536)`):**
   - **`halfvec(1536)` (`pgvector 0.7+`):** Cuts vector index memory and disk footprint by
     **50%** with **99.9%+ recall parity** against `float32`.
   - **Binary Quantization (`bit(1536)`) + Re-scoring:** Compresses each 1536-d vector in
     the HNSW index from `6,144 bytes` to **`192 bytes` (32× smaller)** and re-scores top
     candidates using exact `halfvec` from the heap (`100M` vectors fit in `~25 GB RAM`).
   - **NVMe-Resident DiskANN (`pgvectorscale`):** For tenants with `50M–100M+` active facts
     in a single namespace, `StreamingDiskANN` keeps the graph on NVMe SSD with PQ vectors
     in RAM, serving `<20ms` p95 queries at a fraction of HNSW RAM cost.

4. **Co-Located Citus Sharding by `tenant_id`:**
   - Co-locates each tenant's `semantic`, `episodic`, `entities`, `edges`, `mentions`, and
     `raw_turns` on the same physical worker node so 1–2 hop graph joins, bi-temporal
     supersession (`close_out`), and RLS checks execute locally within a single worker
     without cross-node network hops.

5. **Filtered HNSW Iterative Scan (`pgvector 0.8.0+`):**
   - Enable `SET LOCAL hnsw.iterative_scan = relaxed_order` on filtered vector queries
     (`WHERE namespace = ANY(...) ORDER BY embedding <=> $1 LIMIT k`) so shared partitions
     never under-return rows or fall back to sequential scans.

### 3. Scale tier summary

| Scale Tier | Target Architecture on PostgreSQL |
|---|---|
| **Up to ~50M facts** (Single instance) | Single Postgres 16/17 + `pgvector 0.8` (`halfvec(1536)` + partial HNSW `WHERE valid_to IS NULL` + `PARTITION BY HASH(tenant_id)`) + 1–2 Read Replicas on a 128–256 GB RAM node |
| **100M – 5B+ facts** (Multi-tenant SaaS across 1k–100k tenants) | Citus sharded by `tenant_id` (4–16 workers) + Partition-local `halfvec` / `bit` HNSW + PgBouncer + Stateless API / Leased Workers |
| **100M+ active facts in a SINGLE namespace** | Postgres as transactional system of record + `pgvectorscale` (DiskANN on NVMe) or binary-quantized HNSW (`bit(1536)`) with `halfvec` re-scoring |

## Staged rollout

1. **RLS multi-tenancy + connection pooling (pgbouncer) + read replicas.** Biggest win per
   effort; ship first.
2. **Split API (stateless, autoscaled) from background workers (leased, independently
   scaled).**
3. **Citus sharding by tenant** — only once write/vector-index load on the primary is the
   *proven* bottleneck (profile first, don't pre-shard).
4. **Dedicated-tenant isolation tier** — once there are paying enterprise customers who
   need it.

## Known bugs to fix before scaling horizontally

These get worse under multi-tenant concurrent load and will bite before sharding would even
help — sharding a pathological query pattern just produces N copies of the same slow query:

- `community_search` expands the entire connected component (~55s, alphabetical junk on
  live data).
- `recall`/namespace reconcile does a sequential scan instead of using the HNSW index.
- Namespace reconcile holds one giant transaction's locks for the whole run instead of
  batching.

## Open questions / not yet decided

- Exact jobs-table vs. advisory-lock design for the worker pool's distributed leasing.
- Billing/metering granularity per tenant (requests, storage, embedding calls).
- Whether the dedicated-tenant tier is schema-per-tenant (same Citus cluster) or a fully
  separate cluster per tenant.
- Target scale (tenant count, vectors/tenant) to use for capacity planning before deciding
  when to introduce Citus.
