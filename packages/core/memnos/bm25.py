"""
memnos.bm25 — Lexical scoring + Reciprocal Rank Fusion (Feature 3).

Vector search retrieves on meaning; BM25 retrieves on the exact tokens. The
two are complementary — a hybrid that fuses both ranks (RRF) consistently
beats either alone on enterprise corpora where queries mix natural language
with code identifiers, error codes, ticket IDs, and product names.

This module is intentionally dependency-free (no rank_bm25, no scikit-learn)
so the build stays slim and we can later swap to ArcadeDB native Lucene FTS
or Qdrant sparse vectors without touching callers.

Public surface:
    BM25Index(docs).score(query) -> list[(doc_id, score)]
    reciprocal_rank_fusion([ranking_a, ranking_b], k=60) -> dict[doc_id, score]
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 1]


class BM25Index:
    """Okapi BM25 over an in-memory document set."""

    __slots__ = ("k1", "b", "ids", "tfs", "doc_lens", "avg_doc_len", "df", "N")

    def __init__(
        self,
        docs: Iterable[tuple[str, str]],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.k1 = k1
        self.b = b
        ids: list[str] = []
        tfs: list[dict[str, int]] = []
        doc_lens: list[int] = []
        df: dict[str, int] = {}
        for doc_id, text in docs:
            tokens = _tokenize(text)
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            ids.append(doc_id)
            tfs.append(tf)
            doc_lens.append(len(tokens))
            for t in tf:
                df[t] = df.get(t, 0) + 1
        self.ids = ids
        self.tfs = tfs
        self.doc_lens = doc_lens
        self.N = len(ids)
        self.avg_doc_len = (sum(doc_lens) / self.N) if self.N else 0.0
        self.df = df

    def score(self, query: str) -> list[tuple[str, float]]:
        """Return [(doc_id, bm25_score)] ordered by score desc; only positives."""
        if self.N == 0:
            return []
        q_terms = _tokenize(query)
        if not q_terms:
            return []
        scores = [0.0] * self.N
        avg = self.avg_doc_len or 1.0
        for q in q_terms:
            n_q = self.df.get(q, 0)
            if n_q == 0:
                continue
            idf = math.log((self.N - n_q + 0.5) / (n_q + 0.5) + 1.0)
            for i, tf in enumerate(self.tfs):
                f = tf.get(q, 0)
                if f == 0:
                    continue
                denom = f + self.k1 * (1.0 - self.b + self.b * self.doc_lens[i] / avg)
                scores[i] += idf * (f * (self.k1 + 1.0)) / denom
        ranked = [(self.ids[i], scores[i]) for i in range(self.N) if scores[i] > 0]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked


def reciprocal_rank_fusion(
    rankings: list[list[str]],
    k: int = 60,
) -> dict[str, float]:
    """RRF fusion. ``k=60`` is the Microsoft / Elastic default.

    Each ranking is a list of doc_ids ordered best→worst. Returns the fused
    score per doc_id: ``Σ 1 / (k + rank)`` where rank starts at 1.
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused
