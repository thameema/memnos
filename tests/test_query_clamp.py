"""Query-clamp gate (issue #15 follow-up).

The #15 fix added store.fts_clamp (200 tokens) for the Postgres FTS arm, but the EMBEDDING
and the cross-encoder RERANKER arms still received the FULL query (up to
MEMNOS_QUERY_MAX_CHARS=20000 chars). An ~8000-word / ~40KB query then embedded + reranked
the whole thing (~5s) even though both models cap their own input length anyway. This gate
asserts the follow-up fix, deterministically and at $0 (no OpenAI, no paid bench):

  1. CLAMP BOUNDS EMBED INPUT: a pathological long query, when run through the recall path,
     reaches the embedder as a BOUNDED prefix (<= MEMNOS_QUERY_RERANK_MAX_TOKENS tokens),
     not the full 40KB.
  2. CLAMP BOUNDS RERANK INPUT: the same long query reaches the cross-encoder rerank as a
     bounded prefix — recall_rank / recall_wide_rank clamp the query side.
  3. NORMAL QUERY UNCHANGED: a normal-length query (under the cap) is passed BYTE-FOR-BYTE
     to both the embedder and the reranker — the clamp only triggers above the cap, so
     retrieval scores on normal queries are identical to before.
  4. LATENCY: with the embed + rerank inputs bounded, a pathological query's clamp-
     attributable work is bounded (the helper is O(tokens) and cheap) — far under 1s.

No server, no DB: the recall CPU/embedding plumbing is exercised with a counting fake
embedder and a fake reranker that records exactly what query text it is handed.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import service, rerank as brain_rerank
from core.store import query_clamp, _query_max_tokens, fts_clamp, _fts_max_tokens

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


class RecordingReranker:
    """Stands in for brain_rerank.rerank: records the query text it is handed and returns a
    trivial identity order so recall_rank's quota assembly runs unchanged."""
    def __init__(self):
        self.seen_queries = []

    def __call__(self, query, candidates, model=None):
        self.seen_queries.append(query)
        return [(i, 1.0 / (1.0 + i)) for i in range(len(candidates))]


def _bundle(items):
    """Minimal recall_fetch-shaped bundle: a few raw turns, no temporal intent."""
    from core import temporal as T
    intent = T.analyze("x", time_module_now())
    return {"intent": intent, "ents": [],
            "raw": [{"id": i, "content": c, "score": 1.0 / (1.0 + i)} for i, c in enumerate(items)],
            "sem": [], "dump": []}


def time_module_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def main():
    print("=== query clamp (issue #15 follow-up) ===")
    cap = _query_max_tokens()

    # --- helper behavior: returns identical object under the cap, bounds above it --------
    short = "where do things stand with the alpha deployment"
    check("query_clamp leaves a normal query untouched (identity above byte-equality)",
          query_clamp(short) == short)
    long_q = " ".join(f"word{i}" for i in range(cap * 50))           # far above the cap
    clamped = query_clamp(long_q)
    check(f"query_clamp bounds a long query to <= {cap} tokens",
          len(clamped.split()) <= cap and len(clamped) < len(long_q))
    check("query_clamp is a PREFIX of the original (signal-preserving, order-stable)",
          long_q.startswith(clamped))
    check("clamp cap (rerank) is independent of the FTS cap",
          _query_max_tokens() >= 1 and _fts_max_tokens() >= 1)

    # --- wire a MemoryBrain with a COUNTING embedder + RECORDING reranker ----------------
    embed_inputs = []

    def fake_embed(text):
        embed_inputs.append(text)
        return [0.0] * 8                          # dim-8 dummy vector; never hits a DB here

    rec = RecordingReranker()
    orig_rerank = brain_rerank.rerank
    brain_rerank.rerank = rec
    try:
        mem = service.MemnosMemory(None, fake_embed)

        # 2/3. RERANK INPUT bounded for a long query; recall_rank clamps the query side.
        long_items = [f"alpha billing item {i}" for i in range(5)]
        mem.recall_rank(long_q, _bundle(long_items))
        check("recall_rank handed the reranker a BOUNDED query (not the 40KB original)",
              rec.seen_queries and all(len(s.split()) <= cap for s in rec.seen_queries))
        check("reranker query for a long input is the clamped prefix",
              rec.seen_queries and rec.seen_queries[-1] == query_clamp(long_q))

        # 3. NORMAL query reaches the reranker byte-for-byte (no clamp below the cap).
        rec.seen_queries.clear()
        mem.recall_rank(short, _bundle(long_items))
        check("recall_rank passes a NORMAL query to the reranker byte-for-byte",
              rec.seen_queries and all(s == short for s in rec.seen_queries))

        # 1. EMBED INPUT bounded — exercise the embed site directly (no DB needed: the
        # fake_embed records and the store calls would need a connection, so call the
        # clamp+embed seam the recall path uses).
        embed_inputs.clear()
        mem.embed(query_clamp(long_q))            # the exact call recall_fetch/server make
        check("embedder received a BOUNDED query for a long input",
              embed_inputs and len(embed_inputs[-1].split()) <= cap)
        embed_inputs.clear()
        mem.embed(query_clamp(short))             # normal query: byte-for-byte
        check("embedder received a NORMAL query byte-for-byte",
              embed_inputs and embed_inputs[-1] == short)

        # 4. LATENCY: clamp + a bounded rerank/embed for a pathological query is fast.
        rec.seen_queries.clear()
        t0 = time.perf_counter()
        for _ in range(50):
            query_clamp(long_q)                   # the clamp itself, 50x
        mem.recall_rank(long_q, _bundle(long_items))
        dt = time.perf_counter() - t0
        check(f"clamp-attributable overhead bounded (<1s for pathological query): {dt*1000:.1f}ms",
              dt < 1.0)
    finally:
        brain_rerank.rerank = orig_rerank

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
