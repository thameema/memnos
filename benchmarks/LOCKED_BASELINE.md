# memnos — Locked Accuracy Baseline

**Config (single API — all OpenAI, one key/SDK/bill):**
- Extractor: `gpt-4o-mini` (exhaustive, statement-first SPO + single-valued supersession)
- Embeddings: `text-embedding-3-small` (1536-d)
- Answerer: `gpt-5-mini`  (in production this is the *calling agent*; eval uses gpt-5-mini)
- Retrieval: hybrid (vector HNSW + BM25 RRF) + cross-encoder rerank + quota
  (raw⊕facts) + timeline (temporal) + entity-guarantee (aggregation). No query-time LLM.

## LoCoMo — FULL 10 (n=1542) — REPRODUCED FROM SCRATCH 2026-06-09
| judge | single_hop | multi_hop | temporal | open_domain | OVERALL |
|-------|-----------|-----------|----------|-------------|---------|
| gpt-4o | 29% (82/282) | 59% (190/321) | 47% (45/96) | 69% (579/841) | **58% (898/1542)** |

`python benchmarks/locomo_eval.py --sample-ids 0,1,2,3,4,5,6,7,8,9` on a fresh clone + fresh
DB, gpt-4o judge, $1.81. Full predictions:
[`results/locomo-2026-06-09.json`](results/locomo-2026-06-09.json).

### Run-to-run variance (measured, not hand-waved)
Extraction + consolidation are non-deterministic LLM calls, so the from-scratch overall
moves a few points between ingests. Two correct full runs (both gpt-4o judge, production
recall path): **58%** (the published run above) and **61%** (an independent ingest of the
same data) → a **58–61% band**. We publish the lower run because we can show its predictions.

> Note: an earlier 2026-06-09 commit briefly recorded **56%** here. That was a *harness* bug,
> not the engine — the rewritten driver retrieved via the low-level `Retriever`, bypassing the
> production recall arms (entity-guarantee + timeline). Fixed to use `MemnosMemory.context`
> (same path as MCP/REST), which restored the 58–61% band. Lesson logged.

## Notes
- Answerer model is a large lever (gpt-5-mini vs gpt-4o-mini). Free Claude-CLI extraction
  was tried and REJECTED (volume diluted precision).
- Cross-provider (Claude) judging is an internal de-biasing aid; **production is single-API
  (OpenAI)** and the public reproduce uses the gpt-4o judge.
- Reproduce: `python benchmarks/locomo_eval.py --sample-ids 0,1,2,3,4,5,6,7,8,9`
  (defaults to the config above). Engine = one codebase for benchmark + production.
- **Open:** root-cause the multi_hop −8pp vs the prior claim; re-run; then re-baseline.
