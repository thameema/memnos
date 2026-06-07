# memnos — Locked Accuracy Baseline

**Config (single API — all OpenAI, one key/SDK/bill):**
- Extractor: `gpt-4o-mini` (exhaustive, statement-first SPO + single-valued supersession)
- Embeddings: `text-embedding-3-small` (1536-d)
- Answerer: `gpt-5-mini`  (in production this is the *calling agent*; eval uses gpt-5-mini)
- Retrieval: hybrid (vector HNSW + BM25 RRF) + cross-encoder rerank + quota
  (raw⊕facts) + timeline (temporal) + entity-guarantee (aggregation). No query-time LLM.

**LoCoMo (samples 2,3,4, n=529):**
| judge | single_hop | multi_hop | temporal | open_domain | OVERALL |
|-------|-----------|-----------|----------|-------------|---------|
| gpt-4o (self-provider)        | 31% | 71% | 24% | 68% | **59%** |
| claude (independent, de-biased)| 31% | 70% | 18% | 68% | **58%** |

Judges agree within 1pp on these answers → robust, not a judge artifact. Beats the prior
honest peak (58%, gpt-4o-judge). Reproduce: `python cross_provider_eval.py` (defaults to
this locked config).

**Notes:** answerer model was the biggest single lever (+6pp vs gpt-4o-mini). Free Claude-CLI
extraction was tried and REJECTED (-4pp despite more facts — volume diluted precision).
Cross-provider is an internal measurement aid only; production is single-API.
