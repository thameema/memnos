"""Local embedding + cross-encoder rerank (no API, no cost).

- embedding: BAAI/bge-small-en-v1.5 (384-d) — small, fast, strong retrieval quality
- rerank: cross-encoder/ms-marco-MiniLM-L-6-v2 — final-stage precision

Both run locally via sentence-transformers (already installed). This proves the
'no LLM at query, local cross-encoder rerank' rule end-to-end and measures the
latency the rerank stage adds on top of the base retrieval number.
"""
from __future__ import annotations

import functools

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


@functools.lru_cache(maxsize=1)
def _embedder():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBED_MODEL)


@functools.lru_cache(maxsize=1)
def _reranker():
    from sentence_transformers import CrossEncoder
    return CrossEncoder(RERANK_MODEL)


def embed(text: str) -> list[float]:
    return _embedder().encode(text, normalize_embeddings=True).tolist()


def rerank(query: str, candidates: list[str]) -> list[tuple[int, float]]:
    """Return [(orig_index, score)] sorted best-first."""
    scores = _reranker().predict([(query, c) for c in candidates])
    return sorted([(i, float(s)) for i, s in enumerate(scores)], key=lambda x: x[1], reverse=True)
