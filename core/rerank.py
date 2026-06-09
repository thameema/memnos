"""Configurable cross-encoder reranker (B3).

SmartSearch's biggest single lever was reranker QUALITY (+6pp). An earlier build used a
22M ms-marco-MiniLM; this lets us swap to a stronger model (bge-reranker-base/large,
mxbai-rerank-large) with one config. A reranker is NOT a generative LLM, so the
'no LLM at query time' moat is preserved. Lazily loaded + cached per model.
"""
from __future__ import annotations

import functools

# Default upgrade over the 22M MiniLM. Override per deployment.
DEFAULT_RERANKER = "BAAI/bge-reranker-base"      # ~278M; bge-reranker-large for max accuracy


@functools.lru_cache(maxsize=2)
def _model(name: str):
    from sentence_transformers import CrossEncoder
    return CrossEncoder(name)


def rerank(query: str, candidates: list[str], model: str = DEFAULT_RERANKER) -> list[tuple[int, float]]:
    """Return [(orig_index, score)] sorted best-first. Score is the raw relevance logit."""
    if not candidates:
        return []
    scores = _model(model).predict([(query, c) for c in candidates])
    return sorted([(i, float(s)) for i, s in enumerate(scores)], key=lambda x: x[1], reverse=True)
