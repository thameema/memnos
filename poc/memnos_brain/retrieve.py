"""B3 — recency-gated DUAL retrieval (no LLM at query time).

The owner's refinement + ACT-R/CLS: recall RECENT info sharply from EPISODIC, fall
back to consolidated SEMANTIC for OLD info. The forgetting curve does the routing —
no hard switch:

  episodic_weight = recency_floor + (1-recency_floor)·exp(-λ·age)   # fresh→~1, old→floor
  semantic_weight = w_semantic (flat, durable)
  final_score     = relevance(cross-encoder) × weight

A recent relevant episodic memory outranks semantic; once it decays below
w_semantic, the durable semantic fact wins. Tunables: λ (decay/day), recency_floor,
w_semantic. 'now' = latest memory in the namespace (the conversation's present) or
real wall-clock, whichever is later.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .store import BrainStore
from . import rerank as _rr


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class Retriever:
    def __init__(self, store: BrainStore, schema: str, ns: str, embed_fn, *,
                 reranker_model: str = _rr.DEFAULT_RERANKER, rerank_lock=None,
                 decay_per_day: float = 0.03, recency_floor: float = 0.3,
                 w_semantic: float = 0.6, current_only: bool = False):
        self.store, self.schema, self.ns, self.embed = store, schema, ns, embed_fn
        self.reranker_model, self.rlock = reranker_model, rerank_lock
        self.decay, self.floor, self.w_sem = decay_per_day, recency_floor, w_semantic
        self.current_only = current_only

    def _recency(self, now, observed_at) -> float:
        if observed_at is None or now is None:
            return 1.0
        age_days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
        return math.exp(-self.decay * age_days)

    def retrieve(self, query: str, k: int = 40, top_k: int = 12) -> list[dict]:
        qv = self.embed(query)
        epi = self.store.search_episodic(self.schema, self.ns, qv, query, k)
        sem = self.store.search_semantic(self.schema, self.ns, qv, query, k, current_only=self.current_only)
        now = self.store.max_observed_at(self.schema, self.ns)
        wall = datetime.now(timezone.utc)
        if now is None or (wall > now):
            now = wall

        cands = []
        for r in epi:
            rec = self._recency(now, r.get("observed_at"))
            cands.append({"content": r["content"], "kind": "episodic", "id": r["id"],
                          "recency": rec, "weight": self.floor + (1 - self.floor) * rec})
        for r in sem:
            # semantic is durable; give a mild recency nudge by valid_from so a freshly
            # consolidated fact edges out a stale one, but floored at w_semantic.
            rec = self._recency(now, r.get("valid_from"))
            cands.append({"content": r["content"], "kind": "semantic", "id": r["id"],
                          "recency": rec, "weight": self.w_sem * (0.85 + 0.15 * rec)})
        if not cands:
            return []

        # relevance via cross-encoder over the merged pool (no LLM)
        texts = [c["content"] for c in cands]
        if self.rlock is not None:
            with self.rlock:
                order = _rr.rerank(query, texts, self.reranker_model)
        else:
            order = _rr.rerank(query, texts, self.reranker_model)
        for idx, score in order:
            cands[idx]["relevance"] = _sigmoid(score)
        for c in cands:
            c["final"] = c.get("relevance", 0.0) * c["weight"]

        cands.sort(key=lambda c: c["final"], reverse=True)
        return cands[:top_k]


def context_block(rows, max_chars: int = 6000) -> str:
    out, used = [], 0
    for r in rows:
        tag = "fact" if r["kind"] == "semantic" else "event"
        line = f"- ({tag}) {r['content']}"
        if used + len(line) > max_chars:
            break
        out.append(line); used += len(line)
    return "\n".join(out)
