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
- **[Omnigent](integrations/omnigent.md)** — server-wide, deterministic, write-only capture
  of assistant responses via Omnigent's native function-policy mechanism.

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

## What makes memnos different
- **One engine** — a single PostgreSQL + pgvector; no second store, no graph DB.
- **Deterministic memory** — conflicts resolved by rule (bi-temporal supersession), not by an LLM at write time.
- **No LLM at query time** — hybrid search + a local cross-encoder rerank.
- **Governed by default** — token auth, namespace ACL, audit, usage/cost ledger, encrypted secret vault.

A detailed, version-pinned comparison with other memory systems is at [memnos.net/compare](https://memnos.net/compare.html).

## Operate
The `memnos` CLI — `setup · principal · token · grant · stats · health · usage · audit · whoami`.
Health turns metrics into actionable CRITICAL/WARN findings; usage ledger tracks per-op cost.

## Architecture
See the [main README](../README.md#how-it-works) for the write/read pipeline, and
[`benchmarks/`](../benchmarks/README.md) for the accuracy methodology and reproduce steps.
