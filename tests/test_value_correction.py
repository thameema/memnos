"""Bi-temporal correctness regression: value-correction + recall filtering + fact rerank.

Three defects found in the field (v0.1.12 test-laptop check, 2026-06-23):

  1. Value-correction over-supersession: a "now Y, not X" correction extracts two facts
     (B: the new value, C: the negation). C's reversal close-out fired against B (same
     observed_at — they come from the same source turn), closing the replacement value.
     Fix: nearest_live_facts uses strict < on observed_at, excluding same-turn co-siblings.

  2. Recall returns valid_to-closed (superseded) facts: search_semantic defaulted to
     current_only=False, so closed facts leaked into results. Fix: recall_fetch passes
     current_only=True on the non-temporal arm.

  3. Entity-path facts score=None: entity-dump rows bypassed rr() and were assembled
     without a score field, always ranking below turns. Fix: entity-dump + semantic facts
     are combined for a single rr() pass in recall_rank.

Engine-level (no LLM, no embedding API). Bugs 1+2 use write_facts with a bag-of-words
embedder (cosine-similar for texts sharing content words — required for the reversal
distance guard). Bug 3 calls recall_rank directly with a constructed bundle.
"""
import math
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.store import BrainStore
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
NS = "test:valuecorrect"
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

# Vocabulary for bag-of-words embedding (covers all test sentences)
_VOCAB = ["veldoria", "capital", "vantaria", "mornhaven", "longer", "correction", "now", "city"]


def _bow_embed(text, dim):
    """Bag-of-words embedding over _VOCAB: semantically related texts share dimensions →
    low cosine distance. Required for the reversal close-out distance guard to fire."""
    vec = [0.0] * dim
    words = re.findall(r'\b\w+\b', text.lower())
    for w in words:
        if w in _VOCAB:
            vec[_VOCAB.index(w)] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        return [x / norm for x in vec]
    # fallback: deterministic non-zero unit vector for out-of-vocab text
    h = abs(hash(text))
    fb = [((h >> (i % 32)) & 1) * 0.1 + 0.001 for i in range(dim)]
    n = math.sqrt(sum(x * x for x in fb))
    return [x / n for x in fb]


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def main():
    store = BrainStore(DSN)
    with store.conn.cursor() as c:
        c.execute(f"SELECT atttypmod AS d FROM pg_attribute "
                  f"WHERE attrelid='{SCHEMA}.semantic'::regclass AND attname='embedding'")
        dim = c.fetchone()["d"] or 384

    embed = lambda text: _bow_embed(text, dim)
    mem = MemnosMemory(store, embed, dim=dim, llm=None)

    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))

    print("=== Bug 1: value-correction over-supersession ===")
    d1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    d2 = datetime(2025, 6, 1, tzinfo=timezone.utc)

    # Write 1: initial capital fact (A)
    tid1 = store.insert_raw_turn(SCHEMA, NS, None, "user",
                                 "The capital of Veldoria is Vantaria.", d1, embed("turn1"))
    fact_A = {"subject": "Veldoria", "predicate": "capital", "object": "Vantaria",
              "statement": "The capital of Veldoria is Vantaria."}
    mem.write_facts(NS, [fact_A], d1, turn_id=tid1)

    with store.conn.cursor() as c:
        c.execute(f"SELECT id, object, valid_to FROM {SCHEMA}.semantic "
                  f"WHERE namespace=%s AND predicate='capital' ORDER BY id", (NS,))
        rows = c.fetchall()
    check("initial fact A stored and current (valid_to IS NULL)",
          len(rows) == 1 and rows[0]["object"] == "Vantaria" and rows[0]["valid_to"] is None)

    # Write 2: correction turn — both B (new value) and C (negation) extracted from SAME
    # turn → same observed_at=d2, same source turn id. C has reversal cue "no longer".
    # BOW vectors: A and C share {veldoria, capital, vantaria} → low cosine distance.
    #              B and C share {veldoria, capital} only → still low distance (< 0.40).
    # Without fix: C's reversal close-out finds BOTH A and B (observed_at <= d2).
    # With fix:    C only finds A (observed_at < d2 strict); B excluded (same timestamp).
    tid2 = store.insert_raw_turn(SCHEMA, NS, None, "user",
                                 "Correction: the capital is now Mornhaven, not Vantaria.", d2,
                                 embed("Correction capital Mornhaven not Vantaria"))
    fact_B = {"subject": "Veldoria", "predicate": "capital", "object": "Mornhaven",
              "statement": "The capital of Veldoria is now Mornhaven."}
    fact_C = {"subject": "Veldoria", "predicate": "capital", "object": None,
              "statement": "Veldoria capital is no longer Vantaria."}
    # same d2 observed_at for both (co-extracted from same turn)
    mem.write_facts(NS, [fact_B, fact_C], d2, turn_id=tid2)

    with store.conn.cursor() as c:
        c.execute(f"SELECT id, object, statement, valid_to FROM {SCHEMA}.semantic "
                  f"WHERE namespace=%s AND predicate='capital' ORDER BY id", (NS,))
        rows = c.fetchall()

    A = next((r for r in rows if r["object"] == "Vantaria"), None)
    B = next((r for r in rows if r["object"] == "Mornhaven"), None)
    check("A (Vantaria) got valid_to — correctly superseded",
          A is not None and A["valid_to"] is not None)
    check("B (Mornhaven) remains current (valid_to IS NULL) — NOT closed by co-sibling C",
          B is not None and B["valid_to"] is None)

    print("=== Bug 2: recall excludes valid_to-closed (superseded) facts ===")
    # Fact A content is the precise "is Vantaria" positive claim; fact C ("no longer Vantaria")
    # is also current (so "Vantaria" alone is ambiguous). Check the specific closed sentence.
    A_content = fact_A["statement"]   # "The capital of Veldoria is Vantaria."
    B_content = fact_B["statement"]   # "The capital of Veldoria is now Mornhaven."
    qv = embed("what is the capital of Veldoria")
    results_current = store.search_semantic(SCHEMA, NS, qv, "capital of Veldoria", k=20,
                                            current_only=True)
    results_all = store.search_semantic(SCHEMA, NS, qv, "capital of Veldoria", k=20,
                                        current_only=False)

    current_contents = {r.get("content") for r in results_current}
    all_contents = {r.get("content") for r in results_all}
    check("current_only=True: closed fact A (is Vantaria) absent",
          A_content not in current_contents)
    check("current_only=True: current fact B (is Mornhaven) present",
          B_content in current_contents)
    check("current_only=False: closed fact A IS returned (regression guard)",
          A_content in all_contents)

    print("=== Bug 3: entity-path fact scores are not None ===")
    # Construct a bundle that triggers the entity path (b["ents"] non-empty) with dump rows.
    # Previously dump rows bypassed rr() → score=None. Fixed: all fact candidates go through rr().
    from core.temporal import TemporalIntent
    intent = TemporalIntent()   # temporal=False, current=False — non-temporal query
    dump_row = {"content": "The capital of Veldoria is Mornhaven.",
                "valid_from": d2, "author": "system", "memory_type": None,
                "restatements": 0, "salience": 0.5, "subject_entity": "Veldoria"}
    sem_row = {"content": "Veldoria is a major city.",
               "valid_from": d1, "author": "system", "memory_type": None,
               "restatements": 0, "salience": 0.3, "subject_entity": "Veldoria"}
    b = {
        "intent": intent,
        "ents": ["Veldoria"],
        "raw": [],
        "sem": [sem_row],
        "dump": [dump_row],
        "tl": [],
    }
    results = mem.recall_rank("what is the capital of Veldoria", b)
    fact_rows = [r for r in results if r.get("kind") == "fact"]
    check("at least one fact row in entity-path results", len(fact_rows) > 0)
    no_score = [r for r in fact_rows if r.get("score") is None]
    check("no fact row has score=None (all passed through reranker)",
          len(no_score) == 0)
    check("Mornhaven dump fact is present in results",
          any("Mornhaven" in r.get("content", "") for r in fact_rows))

    # cleanup
    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
