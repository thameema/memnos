# LoCoMo Benchmark for memnos

## What is LoCoMo?

LoCoMo (**Lo**ng **Co**nversation **Mo**mory) is a QA benchmark introduced at ACL 2024 by Maharana et al. (Snap Research):

> **"Evaluating Very Long-Term Conversational Memory of LLM Agents"**
> Adyasha Maharana, Dong-Ho Lee, Sergey Tulyakov, Mohit Bansal, Francesco Barbieri, Yuwei Fang
> ACL 2024 — https://aclanthology.org/2024.acl-long.356
> Code/data: https://github.com/snap-research/locomo

The benchmark contains 10 long multi-session conversations (`locomo10.json`), each spanning months of simulated dialogue. QA pairs are categorized into four types:

| Category | Description |
|----------|-------------|
| `single_hop` | Answered from a single conversation turn |
| `multi_hop` | Requires connecting information across turns |
| `temporal` | Questions about timing, sequence, or recency |
| `open_domain` | World-knowledge combined with conversation context |

## How to Run

### Prerequisites

```bash
pip install httpx anthropic   # or openai
export ANTHROPIC_API_KEY=sk-...   # or OPENAI_API_KEY
```

memnos must be running locally (default: `http://localhost:8766`).

### Run the benchmark

```bash
# Run all 10 samples (downloads dataset automatically)
python3 benchmarks/locomo_runner.py \
  --url http://localhost:8766 \
  --key memnos-local-dev-key \
  --model claude-haiku-4-5-20251001 \
  --sample-ids all \
  --output benchmarks/results.json

# Run a subset (samples 0, 1, 2)
python3 benchmarks/locomo_runner.py --sample-ids 0,1,2

# Use a local dataset file
python3 benchmarks/locomo_runner.py --dataset /path/to/locomo10.json

# Skip re-ingestion if memories are already loaded
python3 benchmarks/locomo_runner.py --skip-ingest

# Verbose output
python3 benchmarks/locomo_runner.py -v
```

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | `http://localhost:8766` | memnos API base URL |
| `--key` | `memnos-local-dev-key` | memnos API key |
| `--model` | `claude-haiku-4-5-20251001` | LLM for answering and judging |
| `--dataset` | GitHub raw URL | Path or URL to `locomo10.json` |
| `--sample-ids` | `all` | Comma-separated 0-based indices or `all` |
| `--output` | `benchmarks/results.json` | Output JSON path |
| `--skip-ingest` | off | Skip memory ingestion |
| `--top-k` | 5 | Number of memories to retrieve per question |
| `--verbose` / `-v` | off | Print per-question detail |

## What the Scores Mean

Each QA pair is scored 0 or 1 by an LLM judge comparing the predicted answer to the ground truth. The judge prompt is:

```
Question: {q}
Expected: {expected}
Predicted: {predicted}
Is the predicted answer correct? Reply with just YES or NO.
```

Scores are reported as percentage correct within each category. Higher is better; chance is near 0 for open-ended QA.

## Results

All runs use conv-26 (sample 0), 30 QA pairs, claude-haiku-4-5-20251001.

### Progression — v1 → v2 → v3

| Category | v1 Raw only | v2 + Extraction pass | v3 All fixes | mnemory (full dataset) |
|----------|-------------|---------------------|--------------|------------------------|
| single_hop | 20.0% | 40.0% | **70.0%** | 63.1% |
| multi_hop | 6.2% | 12.5% | **18.8%** | 53.1% |
| temporal | 75.0% | 100.0% | **100.0%** | 74.8% |
| open_domain | — | — | — | 78.2% |
| **OVERALL** | **20.0%** | **33.3%** | **46.7%** | **73.2%** |

> mnemory scores are from their published benchmark (full 10-sample dataset, gpt-4o-mini).
> memnos scores are single-sample (conv-26), 30 QA pairs.

**v3 changes** (vs v2):
- `top_k` raised 10→20, score floor lowered 0.45→0.35 (more recall)
- Date-anchored extraction prompt (force "On [Month Day, Year], [event]" format)
- Grounded answering prompt (no hallucination — answer only from retrieved memories)
- Auto-extract on every ingest (background task, no manual pass needed)
- Unified atomic extraction (1 LLM call instead of N+1)

**Key findings:**
- `single_hop` jumps to **70.0%** — now beats mnemory on this category
- `temporal` holds at **100.0%** — perfect across all runs
- `multi_hop` still the main gap (18.8% vs 53.1%) — dates in text but not in top-k retrieved memories
- OVERALL: 20% → 33% → **46.7%** across three versions

**Next — path to 70%+:**
- BM25 hybrid search (just shipped, needs fastembed in container)
- Two-layer consolidation: `POST /api/v1/memory/consolidate` then re-run
- Multi-query retrieval for multi_hop (reformulate question, merge results)

### Output file (`results.json`)

```json
{
  "model": "claude-haiku-4-5-20251001",
  "memnos_url": "http://localhost:8766",
  "sample_count": 10,
  "scores": {
    "single_hop": {"correct": 0, "total": 0, "pct": 0.0},
    ...
    "OVERALL": {"correct": 0, "total": 0, "pct": 0.0}
  },
  "results": [
    {
      "sample_id": "...",
      "category": "single_hop",
      "question": "...",
      "expected": "...",
      "predicted": "...",
      "score": 1,
      "search_hits": 5
    }
  ]
}
```

## How It Works

1. **Ingest**: Each dialog turn is written to memnos as a memory with namespace `locomo:{sample_id}` and session tags.
2. **Retrieve**: For each question, the top-k most relevant memories are retrieved via semantic search.
3. **Answer**: An LLM generates an answer given the retrieved context.
4. **Judge**: A second LLM call scores correctness against the ground truth.
