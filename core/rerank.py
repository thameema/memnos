"""Configurable cross-encoder reranker (B3).

The reranker is the biggest single retrieval-quality lever, and it is NOT a generative LLM,
so the 'no LLM at query time' moat is preserved. Runs on ONNX Runtime via `fastembed` —
no torch — so the install stays light. Lazily loaded + cached per model.

Output parity: `fastembed` returns the model's raw relevance logit; we apply a sigmoid so
scores match the previous sentence-transformers `CrossEncoder.predict()` output (0-1) exactly
— a drop-in for everything downstream.
"""
from __future__ import annotations

import functools
import math
import os

# Default reranker. Override per deployment via MEMNOS_RERANKER (e.g.
# BAAI/bge-reranker-large for max accuracy, or Xenova/ms-marco-MiniLM-L-6-v2 for a
# small-RAM host: bge-reranker-base is a 1.0 GB fp32 ONNX whose working set is ~1.9 GB
# resident while hot — the RSS "sawtooth" of issue #8 is this model paging in on recall
# bursts and being reclaimed by the OS at idle). Changing the model changes ranking
# quality — re-run the LoCoMo benchmark before switching a production deployment.
# Default chosen by measured LoCoMo A/B on the IDENTICAL full-10 corpus (2026-06-10,
# n=1542, same answer+judge): MiniLM-L-6 65% vs bge-reranker-base 59% — the 80MB model
# BEAT the 1.04GB one by +6pp while being 8.4x faster (118ms vs 986ms / 80 candidates),
# ~660MB lighter resident, 0.23s vs 1.5s cold start. Override: MEMNOS_RERANKER.
DEFAULT_RERANKER = os.environ.get("MEMNOS_RERANKER", "Xenova/ms-marco-MiniLM-L-6-v2")


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


@functools.lru_cache(maxsize=2)
def _model(name: str):
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(model_name=name)


def rerank(query: str, candidates: list[str], model: str = DEFAULT_RERANKER) -> list[tuple[int, float]]:
    """Return [(orig_index, score)] sorted best-first. Score = sigmoid(relevance logit), 0-1."""
    if not candidates:
        return []
    logits = list(_model(model).rerank(query, candidates))   # raw logits, candidate order
    scores = [_sigmoid(float(s)) for s in logits]
    return sorted(((i, scores[i]) for i in range(len(candidates))), key=lambda x: x[1], reverse=True)
