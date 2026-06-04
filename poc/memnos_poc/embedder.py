"""POC stand-in embedder.

Deterministic, dependency-free, local. Hashes tokens into a 1536-dim signed
bag-of-words and L2-normalizes — so texts sharing words get closer vectors.
This is ONLY to exercise the retrieval plumbing + measure latency at the
production dimension without spending tokens or downloading a model.

Production uses `text-embedding-3-small` (1536-d). Semantic ranking *quality*
is the embedding model's job and is validated separately; this POC validates
the engine path (HNSW + FTS + 1-hop -> RRF) and its latency.
"""
from __future__ import annotations

import hashlib
import math

DIM = 1536


def embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)
    for tok in cleaned.split():
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % DIM] += 1.0 if (h >> 8) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
