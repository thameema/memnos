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
| gpt-4o | 26% (72/282) | 57% (182/321) | 46% (44/96) | 68% (574/841) | **57% (874/1542)** |

`python benchmarks/locomo_eval.py --sample-ids 0,1,2,3,4,5,6,7,8,9` on a fresh clone + fresh
DB, gpt-4o judge, $1.81, **ONNX reranker** (fastembed; no torch — bit-identical ranking to the
prior torch backend). Full predictions: [`results/locomo-2026-06-09.json`](results/locomo-2026-06-09.json).

### Run-to-run variance (measured, not hand-waved)
Extraction + consolidation are non-deterministic LLM calls, so the from-scratch overall moves
a few points between ingests. Three correct full runs (gpt-4o judge, production recall path):
**57%** (the ONNX run above), **58%** and **61%** (torch backend, independent ingests) → a
**57–61% band**. The reranker swap (torch → ONNX) does NOT move the band — ordering is
bit-identical; the spread is extraction variance. We publish the run whose predictions we show.

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
