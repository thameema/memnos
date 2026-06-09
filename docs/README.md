# memnos Documentation

Self-hostable, governed, vendor-neutral **memory for AI agents**. One PostgreSQL +
pgvector engine — ACID correctness, namespace ACL + audit, bi-temporal facts, and
provenance-stamped portability. No second database; no LLM at query time.

## Start here
- **[Quickstart (local, ~5 min)](../QUICKSTART.md)** — run the server + store/recall your first memory.
- **[Accuracy baseline + LoCoMo numbers](../benchmarks/README.md)** — the locked config and scores.

## Integrations
- **[Claude Code](integrations/claude-code.md)** — MCP tools and/or automatic hooks.
- **[Any MCP client](integrations/mcp.md)** — Cursor, Windsurf, Zed, Claude Desktop, REST/SDK.

## How it works (one screen)
```
write:  message ──► raw turn (verbatim, embedded)
                └─► LLM extraction ──► bi-temporal SPO facts ──► supersession (single-valued)
                                                            └─► entity graph
        (offline "sleep") consolidation ──► entity dossiers (multi-hop pre-join)

read (NO LLM):  query ──► hybrid retrieve (pgvector HNSW + BM25/tsvector, RRF)
                          ⊕ timeline arm (temporal)  ⊕ entity-guarantee arm (aggregation)
                          ──► cross-encoder rerank ──► quota'd context block
```

## Why memnos (vs Graphiti / Mem0)
| | memnos | Graphiti | Mem0 |
|---|---|---|---|
| Store | one Postgres+pgvector | Neo4j graph | vector DB (+opt graph) |
| Conflict handling | **deterministic** bi-temporal supersession | LLM edge-invalidation | LLM ADD/UPDATE/DELETE |
| Query-time LLM | none | none | none |
| Rerank | **cross-encoder** | RRF / node-distance | vector score |
| Governance | **token + namespace ACL + audit + usage ledger** | — | — |
| Deploy | one container, runs anywhere | graph DB required | cloud or self-host |

## Operate
`memnos_admin.py` — `init · principal · token · grant · stats · health · usage · audit · quality`.
Health turns metrics into actionable CRITICAL/WARN findings; usage ledger tracks per-op cost.

## Architecture deep-dives
The full design narrative + decisions live in the dev vault:
`~/Documents/ai/personal/projects/memnos/` (`DESIGN-memory-architecture.md`, `research/`,
and dated `handoffs/`).
