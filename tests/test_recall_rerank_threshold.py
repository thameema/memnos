"""Configurable reranker score threshold — precision-controlled recall (issue #22).

MEMNOS_RERANK_MIN_SCORE (default 0.0): candidates whose RAW cross-encoder score
(rerank()'s sigmoid output, before recall_rank's own length/salience/fact/entity rank
heuristics) falls below this floor are dropped entirely, before quotas are applied.
Default 0.0 preserves today's behavior — rerank() scores are always > 0, so a >=0.0
floor never filters anything.

ENGINE-LEVEL, real Postgres: rows are seeded into a live namespace with a crafted-vector
stub embedder (the same trick test_recall_ranking_quality.py uses) and fetched through
the REAL recall_fetch (live SQL). The reranker itself is stubbed to return FIXED,
content-keyed scores (the same technique test_fact_boost_gate uses) so which candidates
clear the threshold is deterministic and independent of the real cross-encoder's
model-version float drift — the filter's CONTRACT (score >= threshold survives, score <
threshold is dropped, boundary is inclusive) is what's under test, not the model.
"""
import os
import sys
import math
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.store import BrainStore
from core import service as svc
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
NS = "test:rerankthreshold"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


# --- seed: 3 raw turns + 3 facts, spanning low/mid/high "reranker confidence" --------
# Plain lowercase content (no capitalized proper nouns, no date/temporal language) so
# the query stays on the plain hybrid path — no entity-guarantee or temporal arm, which
# would route candidates through a different rr() call than the one under test.
T_LOW = "bot: routine heartbeat ping, nothing notable happened, filler chatter only."
T_MID = "bot: someone mentioned the pipeline briefly during standup today."
T_HIGH = "bot: the deployment pipeline failed at the build stage this morning."
F_LOW = "the office printer is on the third floor."
F_MID = "the pipeline status was discussed briefly at standup."
F_HIGH = "the pipeline failure was caused by a missing dependency in the build stage."

# fixed content->score map the stubbed reranker returns — the RAW cross-encoder score
# the threshold filters on. 0.5 is used TWICE (turn + fact) to cover the inclusive
# boundary (score == threshold must be RETAINED, not dropped) for both candidate kinds.
SCORE_BY_CONTENT = {
    T_LOW: 0.10, T_MID: 0.50, T_HIGH: 0.90,
    F_LOW: 0.05, F_MID: 0.50, F_HIGH: 0.85,
}
QUERY = "what is going on with the pipeline"


def _fake_rerank(query, candidates, model=None):
    return [(i, SCORE_BY_CONTENT[c]) for i, c in enumerate(candidates)]


def seed(store, embed):
    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
    d = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for t in (T_LOW, T_MID, T_HIGH):
        store.insert_raw_turn(SCHEMA, NS, None, "bot", t, d, embed(t))
    for f in (F_LOW, F_MID, F_HIGH):
        store.insert_semantic(SCHEMA, NS, "fact", f, valid_from=d, salience=0.5, vec=embed(f))


def _contents(rows):
    return {r["content"] for r in rows}


def _with_env(name, value, fn):
    saved = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        return fn()
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


# --- 1. default (env unset): today's behavior — no filtering ------------------------
def test_default_no_filtering(mem):
    print("default (MEMNOS_RERANK_MIN_SCORE unset): every seeded candidate survives")
    rows = _with_env("MEMNOS_RERANK_MIN_SCORE", None, lambda: mem.recall(NS, QUERY))
    got = _contents(rows)
    for c in SCORE_BY_CONTENT:
        check(f"unset threshold retains low-score candidate: {c[:40]!r}", c in got)


# --- 2. explicit threshold: below excluded, at/above retained (inclusive boundary) --
def test_threshold_boundary(mem):
    print("MEMNOS_RERANK_MIN_SCORE=0.5: below excluded, at/above retained")
    rows = _with_env("MEMNOS_RERANK_MIN_SCORE", "0.5", lambda: mem.recall(NS, QUERY))
    got = _contents(rows)
    check("turn below threshold (0.10 < 0.5) excluded", T_LOW not in got)
    check("fact below threshold (0.05 < 0.5) excluded", F_LOW not in got)
    check("turn AT threshold (0.50 == 0.5) retained (inclusive boundary)", T_MID in got)
    check("fact AT threshold (0.50 == 0.5) retained (inclusive boundary)", F_MID in got)
    check("turn above threshold (0.90 > 0.5) retained", T_HIGH in got)
    check("fact above threshold (0.85 > 0.5) retained", F_HIGH in got)


# --- 3. genuinely operator-tunable: two thresholds -> two different result sets -----
def test_operator_tunable(mem):
    print("threshold is genuinely operator-tunable via env var (two settings differ)")
    rows_loose = _with_env("MEMNOS_RERANK_MIN_SCORE", "0.2", lambda: mem.recall(NS, QUERY))
    rows_tight = _with_env("MEMNOS_RERANK_MIN_SCORE", "0.6", lambda: mem.recall(NS, QUERY))
    loose, tight = _contents(rows_loose), _contents(rows_tight)
    check("0.2 floor drops only the two sub-0.2 candidates (4 of 6 survive)",
          T_LOW not in loose and F_LOW not in loose
          and {T_MID, T_HIGH, F_MID, F_HIGH} <= loose)
    check("0.6 floor drops everything at/below the 0.5 mid tier (2 of 6 survive)",
          tight == {T_HIGH, F_HIGH})
    check("tighter threshold strictly narrows the result set", tight < loose)
    # exercise a threshold above every seeded score: recall degrades to empty, not an
    # error or a silent fallback. 2.0 (not 0.99) sidesteps sigmoid rounding to 1.0.
    rows_empty = _with_env("MEMNOS_RERANK_MIN_SCORE", "2.0", lambda: mem.recall(NS, QUERY))
    check("threshold above every score: no candidate survives (empty, not an error)",
          _contents(rows_empty).isdisjoint(SCORE_BY_CONTENT))


# --- 4. unparseable env value degrades to "unset" (matches _env_float's contract) ---
def test_garbage_value_falls_back_to_unset(mem):
    print("MEMNOS_RERANK_MIN_SCORE=<unparseable>: falls back to no filtering, not a crash")
    rows = _with_env("MEMNOS_RERANK_MIN_SCORE", "not-a-number", lambda: mem.recall(NS, QUERY))
    got = _contents(rows)
    for c in SCORE_BY_CONTENT:
        check(f"garbage threshold retains low-score candidate: {c[:40]!r}", c in got)


# --- 5. recall_wide_rank carries the same floor (issue #22 applies to wide recall) --
def test_wide_recall_threshold(mem):
    print("recall_wide: same precision floor applies to the multi-namespace path")
    rows = _with_env("MEMNOS_RERANK_MIN_SCORE", "0.5",
                     lambda: mem.recall_wide([NS], QUERY))
    got = _contents(rows)
    check("wide recall: below-threshold turn excluded", T_LOW not in got)
    check("wide recall: below-threshold fact excluded", F_LOW not in got)
    check("wide recall: at-threshold turn retained (inclusive boundary)", T_MID in got)
    check("wide recall: at-threshold fact retained (inclusive boundary)", F_MID in got)
    check("wide recall: above-threshold turn retained", T_HIGH in got)
    check("wide recall: above-threshold fact retained", F_HIGH in got)

    rows_default = _with_env("MEMNOS_RERANK_MIN_SCORE", None,
                             lambda: mem.recall_wide([NS], QUERY))
    got_default = _contents(rows_default)
    check("wide recall default (unset): every candidate survives",
          all(c in got_default for c in SCORE_BY_CONTENT))


def main():
    store = BrainStore(DSN)
    store.create_schema("memnos")
    with store.conn.cursor() as c:
        c.execute("SELECT atttypmod AS d FROM pg_attribute "
                  "WHERE attrelid='tenant_memnos.semantic'::regclass AND attname='embedding'")
        dim = c.fetchone()["d"]
    if not dim or dim < 1:
        dim = 384

    _auto = {}

    def crafted_embed(text):
        theta = _auto.setdefault(text, 0.35 * len(_auto))
        v = [0.0] * dim
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v

    seed(store, crafted_embed)
    mem = MemnosMemory(store, crafted_embed, dim=dim, llm=None)

    real_rerank = svc.brain_rerank.rerank
    svc.brain_rerank.rerank = _fake_rerank
    try:
        test_default_no_filtering(mem)
        test_threshold_boundary(mem)
        test_operator_tunable(mem)
        test_garbage_value_falls_back_to_unset(mem)
        test_wide_recall_threshold(mem)
    finally:
        svc.brain_rerank.rerank = real_rerank

    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
