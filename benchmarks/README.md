# memnos benchmarks

## What a benchmark is (and what it isn't)

A **benchmark** is a fixed, public dataset of questions with known answers, run through a
memory system end-to-end, and scored by a judge. It measures one thing: **given a long
conversation, how often can the system recall the right fact to answer a question about it.**

A benchmark number is only meaningful with its **setup attached** — which dataset, which
models did the extraction / answering, and *who judged the answers*. The same answers can
score 44% or 88% depending only on how strict the judge's prompt is. So we publish the
**full configuration and the judge**, not just a headline. We deliberately do **not** put a
competitor comparison here: published numbers elsewhere use different datasets, different
judges, and are usually self-judged, so a side-by-side table would mislead more than inform.
We report *our* result, reproducibly, and let you run it yourself.

## The dataset — LoCoMo

[LoCoMo](https://github.com/snap-research/locomo) is a long-conversation memory benchmark:
10 multi-session conversations and **1,542** QA pairs across four scored question types —
**single-hop** (one fact), **multi-hop** (connect several facts), **temporal** (reason about
dates/order) and **open-domain** (world knowledge grounded in the conversation). A 5th
"adversarial / unanswerable" category is excluded from scoring.

## Latest result

Full 10 conversations (n = 1,542), under the locked configuration in
[`LOCKED_BASELINE.md`](LOCKED_BASELINE.md):

| judge | single-hop | multi-hop | temporal | open-domain | **overall** |
|-------|:---------:|:--------:|:--------:|:-----------:|:-----------:|
| gpt-4o (same-provider) | 34% | 64% | 47% | 70% | **61%** |
| claude (independent) | 33% | 58% | 45% | 68% | **58%** |

We lead with the **independent (cross-provider) judge — 58%** because it removes the
self-preference bias of a model grading its own provider's answers. The same-provider judge
reads 61%. On the *same answers*, sweeping the judge prompt from strict → lenient moves the
number across a **~44% / 58–61% / 85–88%** band — which is exactly why we always name the
judge.

### How it runs (no LLM at query time)

- **Write:** each conversation is ingested into a throwaway namespace — verbatim raw turns
  **plus** bi-temporal SPO facts extracted by `gpt-4o-mini`, with single-valued
  supersession; embeddings are `text-embedding-3-small` (1536-d).
- **Read:** hybrid retrieval (pgvector HNSW + BM25 via tsvector, fused with RRF) → a local
  cross-encoder rerank → quota (raw ⊕ facts) + a guaranteed timeline arm (temporal) + an
  entity-guarantee arm (aggregation). **No LLM is used at retrieval time.**
- **Answer:** the retrieved context block is handed to the answerer model (the *calling
  agent* in production; `gpt-5-mini` in this run).
- **Judge:** an LLM grades each answer against the gold answer; we score under two
  independent judges.

The benchmark and the shipping product run the **same engine** (`core` / `MemnosMemory`) —
there is no separate "benchmark build."

## Reproduce

Prerequisite: a running Postgres+pgvector and an `OPENAI_API_KEY` (the run calls OpenAI for
extraction, embeddings and answering). From the repo root:

```bash
export MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos
export OPENAI_API_KEY=sk-...
python benchmarks/locomo_eval.py --sample-ids 0,1,2,3,4,5,6,7,8,9
```

Defaults match the locked config (`gpt-4o-mini` extract, `gpt-5-mini` answer, gpt-4o + claude
judges). It downloads LoCoMo, ingests into `locomo:<id>` namespaces, answers every QA pair,
and prints per-category and overall accuracy under each judge. See
[`LOCKED_BASELINE.md`](LOCKED_BASELINE.md) for the exact pinned configuration and notes.
