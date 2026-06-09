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
| gpt-4o | 32% (91/282) | 56% (180/321) | 45% (43/96) | 66% (553/841) | **56% (869/1542)** |

This is the **current reproducible number** — `python benchmarks/locomo_eval.py
--sample-ids 0,1,2,3,4,5,6,7,8,9` on a fresh clone + fresh DB, gpt-4o judge, $1.45.
Full predictions: [`results/locomo-2026-06-09.json`](results/locomo-2026-06-09.json).

### ⚠ Discrepancy with the prior "locked" figures (kept for honesty)
Earlier this file claimed **61% (gpt-4o) / 58% (independent Claude)**. That **did not
reproduce** from scratch on the current `core` engine — every category came in lower, with
multi_hop the biggest drop (**64% → 56%, −8pp**), at the identical config (gpt-4o-mini
extract, gpt-5-mini answer, gpt-4o judge, k=40/top_k=14). The 56% above is the real number.
The −8pp on multi_hop points at consolidation/dossier quality — a likely regression from the
ArcadeDB→PG / package refactor that is worth investigating before any re-claim. Until that's
root-caused, **56% (gpt-4o judge) is the figure we stand behind.** The prior 58% used an
independent Claude-CLI judge (more conservative than gpt-4o), so it is almost certainly not
recoverable on these answers either.

## Notes
- Answerer model is a large lever (gpt-5-mini vs gpt-4o-mini). Free Claude-CLI extraction
  was tried and REJECTED (volume diluted precision).
- Cross-provider (Claude) judging is an internal de-biasing aid; **production is single-API
  (OpenAI)** and the public reproduce uses the gpt-4o judge.
- Reproduce: `python benchmarks/locomo_eval.py --sample-ids 0,1,2,3,4,5,6,7,8,9`
  (defaults to the config above). Engine = one codebase for benchmark + production.
- **Open:** root-cause the multi_hop −8pp vs the prior claim; re-run; then re-baseline.
