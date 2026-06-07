# memnos — Locked Accuracy Baseline

**Config (single API — all OpenAI, one key/SDK/bill):**
- Extractor: `gpt-4o-mini` (exhaustive, statement-first SPO + single-valued supersession)
- Embeddings: `text-embedding-3-small` (1536-d)
- Answerer: `gpt-5-mini`  (in production this is the *calling agent*; eval uses gpt-5-mini)
- Retrieval: hybrid (vector HNSW + BM25 RRF) + cross-encoder rerank + quota
  (raw⊕facts) + timeline (temporal) + entity-guarantee (aggregation). No query-time LLM.

## LoCoMo — FULL 10 samples (n=1542) — PUBLISHABLE
| judge | single_hop | multi_hop | temporal | open_domain | OVERALL |
|-------|-----------|-----------|----------|-------------|---------|
| gpt-4o (self-provider)         | 34% | 64% | 47% | 70% | **61%** |
| claude (independent, de-biased)| 33% | 58% | 45% | 68% | **58%** |

Two strong, independent judges → **58–61%** depending on judge. The cross-provider
(Claude) judge de-biases GPT-grading-GPT and is the more conservative, defensible figure.

### 3-sample subset (2,3,4, n=529) — for historical comparison
gpt-4o judge 59% / claude judge 58%. (Subset had unusually hard temporal Qs: 18–24% vs
45–47% on full-10.)

## Notes
- Answerer model was the biggest single lever (+6pp vs gpt-4o-mini). Free Claude-CLI
  extraction was tried and REJECTED (-4pp — volume diluted precision).
- Cross-provider is an internal measurement aid only; **production is single-API (OpenAI)**.
- Reproduce: `python cross_provider_eval.py --sample-ids 0,1,2,3,4,5,6,7,8,9`
  (defaults to the locked config). Engine = one codebase for benchmark + production.
