"""Local embedding + cross-encoder rerank (no API, no cost) — free 384-d mode.

- embedding: BAAI/bge-small-en-v1.5 (384-d, L2-normalized) — small, fast, strong retrieval
- rerank: ms-marco-MiniLM-L-6-v2 — final-stage precision

Both run locally on ONNX Runtime via `fastembed` (no torch), so the install stays light.
This is the free path: no OpenAI key, no cost, no LLM at query time.
"""
from __future__ import annotations

import functools
import math
import os

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"   # ONNX build of the MiniLM cross-encoder


def _arena_enabled() -> bool:
    # issue #15: ONNX CPU memory arena holds a never-returned high-water block. Off by
    # default so the embed/rerank path releases memory back to the OS. MEMNOS_RERANK_ARENA=1
    # reverts. Arena/threads are allocation knobs only — embeddings + logits are unchanged.
    return os.environ.get("MEMNOS_RERANK_ARENA", "0").strip().lower() in ("1", "true", "yes", "on")


def _threads() -> int | None:
    try:
        n = int(os.environ.get("MEMNOS_RERANK_THREADS", "1"))
    except (TypeError, ValueError):
        n = 1
    return None if n <= 0 else n


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


@functools.lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=EMBED_MODEL,
                         threads=_threads(),
                         enable_cpu_mem_arena=_arena_enabled())


@functools.lru_cache(maxsize=1)
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(model_name=RERANK_MODEL,
                            threads=_threads(),
                            enable_cpu_mem_arena=_arena_enabled())


def embed(text: str) -> list[float]:
    return next(iter(_embedder().embed([text]))).tolist()


def rerank(query: str, candidates: list[str]) -> list[tuple[int, float]]:
    """Return [(orig_index, score)] sorted best-first."""
    if not candidates:
        return []
    logits = list(_reranker().rerank(query, candidates))
    scores = [_sigmoid(float(s)) for s in logits]
    return sorted(((i, scores[i]) for i in range(len(candidates))), key=lambda x: x[1], reverse=True)
