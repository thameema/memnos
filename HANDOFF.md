# memnos — dev handoff / context

Snapshot of the architecture decisions + build state so this work can resume on
another machine (or by a fresh Claude Code session). Read this + `git log` and
you have the full picture.

## What memnos is
A **self-hostable, governed, vendor-neutral memory platform** for AI agents —
positioned against Graphiti (engine-only library) and Mem0 (cloud lock-in).
Moat = ACID correctness + governance/ACL + provenance-stamped portability +
runs anywhere. Target buyer: B2B regulated/SMB (law/dental/clinic) + eng teams.

## Locked architecture decisions (this is the important context)
1. **Single engine: PostgreSQL + pgvector.** ACID across vector+graph+relational. No second store.
2. **No AGE / no native graph engine.** Relationships = typed indexed PG tables (entity/relation/mentions); 1-hop = indexed joins. Runs on any managed PG. (AGE rejected: only Azure managed supports it; memnos's graph is shallow.)
3. **No LLM at query time.** Query = pgvector(HNSW) + tsvector(BM25) + 1-hop graph → RRF → **local cross-encoder rerank**. Target <200ms (POC measured ~122ms e2e).
4. **LLM at ingest only.** **OpenAI default for BOTH embedding (`text-embedding-3-small`, 1536-d) and extraction (`gpt-4o-mini`)** — embedding cost negligible (~$4/M memories). Pluggable to any OpenAI-compatible endpoint (Ollama/vLLM) by config — local is a 3-env-var swap, no code change.
5. **Bi-temporal facts** (valid_at/invalid_at + system axis). Accuracy via temporal filtering + provenance, not deep traversal.
6. **Multi-tenant: schema-per-tenant + namespace(user/team) column inside.** Schema = natural shard boundary → app-level tenant→instance sharding (shared-nothing, no Citus). Scales B2B to thousands of firms; NOT for millions of consumer tenants (that'd need shared-table+tenant_id tier).
7. **Identity server-side** (never client-trusted). Token auth (`memnos login`); OIDC/SSO for enterprise. **Groups** are the ACL unit (users→groups→namespace grants). Namespace-level ACL.
8. **Namespace routing:** explicit → session-binding → user keyword-map → default (all clamped to authorized). Keyword map server-side, deterministic, visible+correctable.
9. **UI:** admin console (users/groups/namespaces/ACL/KB/audit/tokens) + end-user UI (set default ns, manage keyword map, browse own memory). MVP component.
10. **Cost tracking:** every extraction writes a `usage` ledger row (tenant, model, tokens, cost_usd); UI surfaces per-tenant spend + priciest extractions. CostMeter enforces a hard budget cap.

## Build state (branch `poc/pg-pgvector`)
POC + first implementation, all committed (`git log master..HEAD`):
- **POC 1–5**: pg+pgvector verified (HNSW+tsvector one engine); schema-per-tenant DDL; StorageBackend + **RRF hybrid search in ONE SQL round-trip**; Acme/Jane/Bob demo (graph arm finds what vector+FTS miss); **latency p95 77ms @ 50k rows**; e2e with local embed+rerank **~122ms**.
- **Extraction eval**: Qwen-7B vs gpt-4o-mini (entities tie ~0.84; facts gpt-4o-mini wins 0.76 vs 0.51). 14B **untested** — won't fit 16GB M1 Pro (needs ≥24GB/GPU).
- **IMPL 1–4**: CostMeter+budget cap; ingest pipeline (episode→gate→extract→embed→memory/facts/mentions); retrieve (hybrid RRF→rerank→context); `locomo_pg.py` driver.
- **OpenAI-embed wiring**: embedders pluggable, OpenAI default (1536), `--local-embed` opt-in.
- **First LoCoMo run on PG engine** (sample 0, 20 Q, no-extract, local embed): **40% overall** — a FLOOR (weakest config: no facts/graph, 384-d local embed, untuned). Cost meter + cap worked.

## Open / next steps
1. **Run LoCoMo with the new OpenAI key** (user moving to a different key): `OPENAI_API_KEY=… python locomo_pg.py --sample-ids 0 --max-qa 20 --budget 1.00` (OpenAI embed+extract now default). Then scale up. Full-run cost est: <$1 all-mini, ~$7-8 if gpt-4o judge; ingest-once to keep tuning cheap.
2. **Test 14B/32B extraction on a GPU box** (M1 Pro 16GB can't) to decide fully-local viability.
3. **Build out MVP backlog**: identity/groups/ACL, admin+user UI, tenant-shard router, vault/audit, MCP server, portable export (P5 from the other branch).
4. Consider porting P1/P3/P5 work from branch `overnight/p0-p5-graphiti-port` (bi-temporal Fact model, contradiction→invalidation, portable export) onto the PG engine.

## Environment to recreate on the new machine
- **Docker** + `cd poc && docker compose -f docker-compose.poc.yml up -d` (postgres:pgvector, port 5433).
- **Schema**: `psql … < poc/sql/01_schema.sql` then `SELECT create_tenant_schema('demo', 1536);`
- **Python venv** (do NOT copy the old one — it has a stale `engram` shebang): `python -m venv .venv && .venv/bin/pip install "psycopg[binary]" openai sentence-transformers httpx`
- **Ollama** (only if testing local models): `ollama pull qwen2.5:14b` (needs ≥24GB RAM).
- **Secrets**: create `poc/../.env` with `OPENAI_API_KEY=…` (new key) + `ARCADEDB_PASSWORD=…`. `.env` is gitignored — set it fresh, never commit.
- POC data (tenant_demo/locomo*) is throwaway — re-run `poc/ingest_demo.py` / `poc/locomo_pg.py` to repopulate.

## Note
ArcadeDB-based code on `master` is the OLD engine being retired. The new design lives on `poc/pg-pgvector`.
