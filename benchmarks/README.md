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

Full 10 conversations (n = 1,542), reproduced **from scratch** with
`python benchmarks/locomo_eval.py --sample-ids 0,1,2,3,4,5,6,7,8,9` on the config below
(gpt-4o-mini extract · text-embedding-3-small 1536-d · gpt-5-mini answer · gpt-4o judge).
Run date 2026-06-09, $1.81 — raw per-question predictions in
[`results/locomo-2026-06-09.json`](results/locomo-2026-06-09.json):

| judge | single-hop | multi-hop | temporal | open-domain | **overall** |
|-------|:---------:|:--------:|:--------:|:-----------:|:-----------:|
| gpt-4o | 29% (82/282) | 59% (190/321) | 47% (45/96) | 69% (579/841) | **58% (898/1542)** |

This is what the **one-command public reproduce actually produces** end-to-end, judged by
gpt-4o, via the same recall path production uses.

> **On variance, honestly.** Fact extraction + consolidation are LLM calls (gpt-4o-mini), and
> they're not deterministic — a second independent from-scratch ingest of the same data scored
> **61%** under the same gpt-4o judge. So the reproducible figure is a **58–61% band**, not a
> single point; we publish the lower run we can show you the predictions for. The score is
> also judge-sensitive: on the *same answers*, a strict → lenient judge prompt sweeps roughly
> **44% → 58% → 85%**, which is why we always name the judge. We'd rather report a 58% you can
> rerun than a headline you can't.

### How it runs (no LLM at query time)

- **Write:** each conversation is ingested into a throwaway namespace — verbatim raw turns
  **plus** bi-temporal SPO facts extracted by `gpt-4o-mini`, with single-valued
  supersession; embeddings are `text-embedding-3-small` (1536-d).
- **Read:** hybrid retrieval (pgvector HNSW + BM25 via tsvector, fused with RRF) → a local
  cross-encoder rerank → quota (raw ⊕ facts) + a guaranteed timeline arm (temporal) + an
  entity-guarantee arm (aggregation). **No LLM is used at retrieval time.**
- **Answer:** the retrieved context block is handed to the answerer model (the *calling
  agent* in production; `gpt-5-mini` in this run).
- **Judge:** an LLM (gpt-4o) grades each answer against the gold answer (YES/NO same key
  information).

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
