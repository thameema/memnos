"""Entity-aware recall — subject disambiguation (issue #17).

THE BUG: two unrelated efforts share ONE namespace and a topic vocabulary, so their
embeddings sit next to each other. A query about subject A pulls in subject B's facts
purely on vector/FTS proximity. The store ALREADY holds them as distinct subjects
(semantic.subject_entity / mentions); recall just never used that binding at query time.

  A = "Gateway"   — an Interoperability Gateway service (repo, CI/CD, deploy work).
  B = "Crosswalk" — a one-off record-ID crosswalk data task (different people, a
                    storage-bucket deliverable, no relation to the Gateway).

Both are FHIR/interop-flavored. With the entity arm OFF, B's crosswalk facts rank
among A's gateway facts. With it ON (default), facts that mention the queried entity
rank above facts that are merely semantically near but carry a competing subject.

ENGINE-LEVEL, NO OpenAI: rows are seeded with a crafted-vector stub embedder (the same
trick the ranking-quality test uses); recall() runs the REAL production pipeline.

CI-DETERMINISM: like test_recall_ranking_quality, we pin MEMNOS_RERANK=0 so candidates
keep their stable retrieval (RRF) order and the cross-encoder's float variance across
ONNX builds cannot make the ordering flaky. The POLICY under test — does the entity arm
lift A's facts above B's, and does the kill switch reproduce the conflation — still has
to fire for the assertions to pass, so the pin keeps the test meaningful, not tautological.
The arm's score shaping (boost / competing-subject demote) is bounded and deterministic.
"""
import math
import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.store import BrainStore
from core import service as svc
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
NS = "test:entityscope"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


# --- the labeled micro-set: a "query -> correct-subject facts" set --------------------
# Subject A facts mention the Gateway; subject B facts mention the Crosswalk. They share
# the FHIR/interop vocabulary so they are genuinely close in embedding space.
A_FACTS = [
    ("Gateway", "The Gateway service exposes a FHIR R4 interop endpoint for partners."),
    ("Gateway", "The Gateway service deploy pipeline runs CI on every merge request."),
    ("Gateway", "The Gateway service handles FHIR Bundle ingestion for healthcare partners."),
]
B_FACTS = [
    ("Crosswalk", "The Crosswalk task maps legacy record IDs to FHIR resource IDs."),
    ("Crosswalk", "The Crosswalk task delivers a storage-bucket of mapped healthcare IDs."),
    ("Crosswalk", "The Crosswalk task reconciles FHIR identifiers across two systems."),
]
QUERY = "what is the work on the Gateway"          # non-temporal; names entity "Gateway"


def seed(store, embed):
    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
    d = datetime(2026, 6, 10, tzinfo=timezone.utc)
    # interleave so neither subject owns the natural retrieval order; vectors are placed
    # so A and B facts are MUTUALLY ADJACENT (shared vocabulary -> the conflation setup).
    for (sa, fa), (sb, fb) in zip(A_FACTS, B_FACTS):
        store.insert_semantic(SCHEMA, NS, "fact", fa, subject=sa, valid_from=d,
                              salience=0.5, vec=embed(fa))
        store.insert_semantic(SCHEMA, NS, "fact", fb, subject=sb, valid_from=d,
                              salience=0.5, vec=embed(fb))


def _facts(rows):
    return [r for r in rows if r["kind"] == "fact"]


def _is_a(r):  # an A-fact mentions the Gateway
    return "gateway" in r["content"].lower()


def _is_b(r):  # a B-fact mentions the Crosswalk
    return "crosswalk" in r["content"].lower()


# --- 1. entity arm ON: A's facts rank above B's --------------------------------------
def test_entity_boost_on(mem):
    print("entity arm ON (default): Gateway facts rank above Crosswalk facts")
    rows = mem.recall(NS, QUERY)
    facts = _facts(rows)
    check("at least one Gateway fact returned", any(_is_a(r) for r in facts))
    check("the FIRST fact is a Gateway (queried-subject) fact",
          facts and _is_a(facts[0]))
    first_a = next((i for i, r in enumerate(facts) if _is_a(r)), None)
    first_b = next((i for i, r in enumerate(facts) if _is_b(r)), None)
    # POLICY: every A-fact outranks every B-fact (subject identity, not just proximity).
    last_a = max((i for i, r in enumerate(facts) if _is_a(r)), default=-1)
    check("all Gateway facts precede the first Crosswalk fact",
          first_b is None or (last_a >= 0 and last_a < first_b))


# --- 2. the BOOST/DEMOTE arm in isolation: genuine policy contrast --------------------
# The end-to-end test above is partly carried by the pre-existing #15 entity-GUARANTEE
# dump arm (it front-loads facts whose subject_entity / content matches the query token).
# To prove the #17 BOOST/DEMOTE arm is the thing reordering the vector pool — not a
# tautology — drive recall_rank with a hand-built bundle that has NO dump and a sem pool
# in which a COMPETING-subject (Crosswalk) fact retrieves ABOVE the on-subject (Gateway)
# fact (vector proximity, the exact conflation). The reranker is stubbed to a constant so
# every candidate's BASE score is identical; only the entity arm can change the order.
def test_boost_demote_arm_isolated(mem):
    print("entity boost/demote arm (isolated): on-subject fact overtakes the near neighbor")
    real = svc.brain_rerank.rerank
    svc.brain_rerank.rerank = lambda q, cands, m=None: [(i, 0.5) for i in range(len(cands))]
    try:
        intent = types.SimpleNamespace(temporal=False, start=None, end=None,
                                       current=False, order=None)
        # sem pool: a Crosswalk fact FIRST (it was the vector-closest), Gateway fact second.
        sem = [
            {"content": "The Crosswalk task maps FHIR IDs.", "subject_entity": "Crosswalk",
             "restatements": 0, "salience": 0.5},
            {"content": "The Gateway service exposes a FHIR endpoint.",
             "subject_entity": "Gateway", "restatements": 0, "salience": 0.5},
        ]
        # ents = the query's resolved entity; NO "dump" key -> entity-guarantee arm inert,
        # so the ordering is decided purely by the boost/demote arm.
        b = {"intent": intent, "ents": ["Gateway"], "raw": [], "sem": sem}

        rows_on = _facts(mem.recall_rank("what is the work on the Gateway", dict(b)))
        check("arm ON: the on-subject (Gateway) fact leads despite worse retrieval rank",
              rows_on and _is_a(rows_on[0]))
        check("arm ON: the competing (Crosswalk) fact is demoted below it",
              rows_on and _is_b(rows_on[-1]))

        os.environ["MEMNOS_RECALL_ENTITY_BOOST"] = "0"
        try:
            rows_off = _facts(mem.recall_rank("what is the work on the Gateway", dict(b)))
        finally:
            del os.environ["MEMNOS_RECALL_ENTITY_BOOST"]
        # POLICY CONTRAST: with the arm OFF, base scores are equal so the original retrieval
        # order stands — the competing Crosswalk fact LEADS (the bug reproduced). The toggle
        # genuinely flips which subject wins.
        check("kill switch MEMNOS_RECALL_ENTITY_BOOST=0: competing fact leads (bug reproduced)",
              rows_off and _is_b(rows_off[0]) and rows_on and _is_a(rows_on[0]))
    finally:
        svc.brain_rerank.rerank = real


# --- 2b. SHARED-VOCABULARY no-op regression (the Mac Mini bug) ------------------------
# The case the OLD arm silently no-op'd on: a multi-word query subject
# ("Interoperability Gateway") whose query_entities() SPLITS into ['Interoperability',
# 'Gateway'], competing with an adjacent subject ("Record ID Crosswalk") whose fact
# CONTENT literally contains the word "interoperability". Under the old logic the
# `qe in content` substring clause matched the split token 'interoperability' against the
# Crosswalk fact's prose -> it was treated as on-topic and BOOSTED, never demoted, so the
# arm did NOTHING in exactly the case it exists for (ON == OFF, byte-identical order).
# The fixed arm must match the ENTITY BINDING (subject_entity / named entities), as a
# whole phrase, so the off-subject Crosswalk fact is demoted and the Gateway fact leads.
SV_QUERY = "what is the work on the Interoperability Gateway"


def test_shared_vocab_noop_regression(mem):
    print("shared-vocabulary regression: split-token in competing content must NOT save it")
    real = svc.brain_rerank.rerank
    svc.brain_rerank.rerank = lambda q, cands, m=None: [(i, 0.5) for i in range(len(cands))]
    try:
        intent = types.SimpleNamespace(temporal=False, start=None, end=None,
                                       current=False, order=None)
        # sem pool: the COMPETING Crosswalk fact retrieves FIRST (vector-closest) AND its
        # content contains the query token "interoperability" — the exact trap. The on-
        # subject Gateway fact retrieves second. Only correct entity-level matching reorders.
        sem = [
            {"content": "The Crosswalk task maps legacy record IDs for the interoperability program.",
             "subject_entity": "Record ID Crosswalk", "restatements": 0, "salience": 0.5},
            {"content": "The Gateway service exposes a FHIR R4 endpoint for partners.",
             "subject_entity": "Interoperability Gateway", "restatements": 0, "salience": 0.5},
        ]
        b = {"intent": intent, "ents": ["Interoperability", "Gateway"], "raw": [], "sem": sem}

        rows_on = _facts(mem.recall_rank(SV_QUERY, dict(b)))
        order_on = [r["content"][:20] for r in rows_on]

        os.environ["MEMNOS_RECALL_ENTITY_BOOST"] = "0"
        try:
            rows_off = _facts(mem.recall_rank(SV_QUERY, dict(b)))
        finally:
            del os.environ["MEMNOS_RECALL_ENTITY_BOOST"]
        order_off = [r["content"][:20] for r in rows_off]

        # The defect signature: ON and OFF identical (arm is a no-op). The fix makes them
        # DIFFER — a genuine arm-driven contrast, the thing the old test never exercised.
        check("arm ON differs from OFF on shared-vocab content (NOT a no-op)",
              order_on != order_off)
        check("arm ON: on-subject (Gateway) fact leads despite worse retrieval + shared token",
              rows_on and _is_a(rows_on[0]))
        check("arm ON: competing (Crosswalk) fact demoted to last",
              rows_on and _is_b(rows_on[-1]))
        check("kill switch OFF: conflation returns (competing Crosswalk fact leads)",
              rows_off and _is_b(rows_off[0]))
    finally:
        svc.brain_rerank.rerank = real


# --- 3. precision@k on the labeled set (end-to-end) ----------------------------------
def test_precision_on_labeled_set(mem):
    print("precision@3 on the labeled query->correct-subject set")
    rows_on = _facts(mem.recall(NS, QUERY))

    def prec_at(facts, n=3):
        top = facts[:n]
        return (sum(1 for r in top if _is_a(r)) / len(top)) if top else 0.0
    p_on = prec_at(rows_on)
    print(f"    precision@3  ON={p_on:.2f}")
    check("precision@3 (correct-subject) ON is perfect on the labeled set (== 1.0)",
          abs(p_on - 1.0) < 1e-9)


# --- 3. hard subject scope: caller pins one entity -----------------------------------
def test_hard_subject_scope(mem):
    print("hard scope subject='Crosswalk': ONLY Crosswalk facts returned")
    rows = _facts(mem.recall(NS, QUERY, subject="Crosswalk"))
    check("scoped result returns Crosswalk facts", any(_is_b(r) for r in rows))
    check("scoped result excludes ALL Gateway facts", not any(_is_a(r) for r in rows))

    # kill switch: scope param ignored when MEMNOS_RECALL_ENTITY_SCOPE=0
    os.environ["MEMNOS_RECALL_ENTITY_SCOPE"] = "0"
    try:
        rows_off = _facts(mem.recall(NS, QUERY, subject="Crosswalk"))
    finally:
        del os.environ["MEMNOS_RECALL_ENTITY_SCOPE"]
    check("kill switch MEMNOS_RECALL_ENTITY_SCOPE=0: scope ignored, Gateway facts return",
          any(_is_a(r) for r in rows_off))


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
        # deterministic angle per distinct text; A/B facts land mutually adjacent on the
        # unit circle so vector proximity alone CANNOT separate the two subjects.
        theta = _auto.setdefault(text, 0.20 * len(_auto))
        v = [0.0] * dim
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v

    seed(store, crafted_embed)
    mem = MemnosMemory(store, crafted_embed, dim=dim, llm=None)

    # Pin retrieval (RRF) order so the POLICY assertions are CI-deterministic (cross-encoder
    # floats vary across ONNX builds — see test_recall_ranking_quality docstring).
    _saved = os.environ.get("MEMNOS_RERANK")
    os.environ["MEMNOS_RERANK"] = "0"
    try:
        test_entity_boost_on(mem)
        test_boost_demote_arm_isolated(mem)
        test_shared_vocab_noop_regression(mem)
        test_precision_on_labeled_set(mem)
        test_hard_subject_scope(mem)
    finally:
        if _saved is None:
            os.environ.pop("MEMNOS_RERANK", None)
        else:
            os.environ["MEMNOS_RERANK"] = _saved

    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
