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

# Default reranker. Override per deployment (e.g. BAAI/bge-reranker-large for max accuracy).
DEFAULT_RERANKER = "BAAI/bge-reranker-base"      # ~278M params, ONNX


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
