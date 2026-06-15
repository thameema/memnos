"""Recall answer-quality / ranking gap (issue #2 — the #11-class field case).

Field evidence (HDIG): on "what projects does the HDIG MR reviewer monitor", recall
FOUND the answer facts but ranked them 15-22, UNDER noise — ranks 1-4 were raw turns,
including the SAME cron-command turn duplicated ~10x. The data was right; ranking +
presentation buried it. Three rank/render-path layers (write path untouched), each
behind an env kill switch (default = new behavior on):

  1. DEDUP duplicate candidates BEFORE rerank — exact normalized-content hash AND
     embedding cosine distance < the write-path threshold (0.03). Collapse to ONE
     survivor. The cron-x10 killer. MEMNOS_RECALL_DEDUP=0 disables.
  2. FACT-FIRST context rendering — list/aggregation ('which / how many / what projects')
     and broad-status questions lead with distilled FACTS, raw turns below as evidence,
     regardless of rerank float order. Verbatim intent keeps raw turns first.
     MEMNOS_RECALL_FACT_FIRST=0 disables.
  3. Bounded, query-type-GATED fact preference in ranking — a small capped nudge for
     fact candidates over raw turns of similar relevance, ONLY for non-verbatim classes.
     MEMNOS_RECALL_FACT_BOOST=0 disables.

Engine-level (no LLM, no embedding API): rows are seeded with a crafted-vector stub
embedder; recall() runs the REAL production retrieval + rerank pipeline.

CI-DETERMINISM: the three RANK/RENDER POLICIES under test (dedup-collapse, fact-first
leading, verbatim turn-first) are decided in recall_rank's quota-ASSEMBLY step, NOT by
the cross-encoder's per-candidate floats. But the cross-encoder's scores DO decide which
near-tied near-duplicate / fact / turn lands at a given index, and those floats vary
across ONNX-Runtime builds (dev vs the 2-core CI runner) — so a test that asserts "row N
is exactly this kind" is flaky on CI even though the POLICY is identical. We therefore
pin MEMNOS_RERANK=0 for the DB-backed pipeline checks: candidates keep their stable
retrieval (RRF) order, the cross-encoder never runs, and the policy assertions (does
dedup collapse? do facts lead a list query? does a turn lead a verbatim query?) become
deterministic while remaining MEANINGFUL — each policy still has to fire for the check to
pass. The bounded fact-boost test (test_fact_boost_gate) stubs the reranker itself, so it
is independent of this pin. (The cross-encoder's real ranking quality is exercised by the
LoCoMo gate + test_broad_query_ranking, not by exact-index asserts here.)
"""
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.store import BrainStore
from core import service as svc
from core.service import MemnosMemory, query_intent

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
NS = "test:rankquality"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


# --- 1. intent classifier battery (pure function, no DB) ----------------------------
def test_intent():
    print("query_intent battery (extends #11 classifier)")
    battery = [
        # list / aggregation -> facts lead
        ("what projects does the HDIG MR reviewer monitor", "list"),
        ("which agents can handle deploys?", "list"),
        ("how many tickets are open", "list"),
        ("what services run on host27", "list"),
        ("list all the vendors", "list"),
        ("what pets does Caroline have?", "list"),
        ("what are the open action items", "list"),
        # verbatim -> raw turns stay first (the regression detector)
        ("what exactly did the reviewer say about the cron job?", "verbatim"),
        ('did anyone mention "node pool"?', "verbatim"),
        ("quote what Alice told the team", "verbatim"),
        # broad status -> facts lead (#11)
        ("where are we with the deployment?", "broad"),
        ("catch me up on the project", "broad"),
        # neutral single-hop / temporal -> turn-first (today's behavior)
        ("When did Caroline go to the support group?", "neutral"),
        ("What instrument does John play?", "neutral"),
    ]
    for q, want in battery:
        check(f"{want:8s} <- {q[:54]}", query_intent(q) == want)


# --- HDIG repro seed ----------------------------------------------------------------
# (a) the SAME operational raw turn duplicated ~10x (the dup source — exact content);
# (b) one NEAR-identical variant (embedding-close, not byte-identical);
# (c) a few answer-bearing distilled facts the list question should surface.
CRON_TURN = ("bot: cron job hdig-mr-reviewer ran at 02:00 UTC; command "
             "`python -m hdig.mr_reviewer --scan`; exit 0; nothing to review.")
NEAR_VARIANT = ("bot: cron job hdig-mr-reviewer ran at 02:00 UTC; command "
                "`python -m hdig.mr_reviewer --scan`; exit 0; no changes to review.")
ANSWER_FACTS = [
    "The HDIG MR reviewer monitors the payments-core project.",
    "The HDIG MR reviewer monitors the ledger-service project.",
    "The HDIG MR reviewer monitors the fraud-detection project.",
]
VERBATIM_TURN = ("reviewer: I told the team the cron job for hdig-mr-reviewer "
                 "should never auto-merge, only flag.")


def seed(store, embed):
    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
    d = datetime(2026, 6, 10, tzinfo=timezone.utc)
    for _ in range(10):                                   # cron turn x10 (exact dup)
        store.insert_raw_turn(SCHEMA, NS, None, "bot", CRON_TURN, d, embed(CRON_TURN))
    # embedding-near variant: place its vector within <0.03 cosine of the cron turn's
    store.insert_raw_turn(SCHEMA, NS, None, "bot", NEAR_VARIANT, d,
                          near_vec(embed(CRON_TURN)))
    store.insert_raw_turn(SCHEMA, NS, None, "reviewer", VERBATIM_TURN, d,
                          embed(VERBATIM_TURN))
    for f in ANSWER_FACTS:
        store.insert_semantic(SCHEMA, NS, "fact", f, valid_from=d, salience=0.5,
                              vec=embed(f))


def near_vec(v):
    """A vector within <0.03 cosine distance of v (tiny perturbation)."""
    out = list(v)
    out[2] = (out[2] if len(out) > 2 else 0.0) + 0.01
    return out


# --- 2. dedup collapses the cron x10 ------------------------------------------------
def test_dedup(mem):
    print("dedup: cron-x10 collapses to one candidate")
    q = "what projects does the HDIG MR reviewer monitor"
    def cron_count(rows):
        return sum(1 for r in rows if r["kind"] == "turn"
                   and "hdig-mr-reviewer ran" in r["content"])

    rows = mem.recall(NS, q)
    cron_rows = [r for r in rows if r["kind"] == "turn"
                 and "hdig-mr-reviewer ran" in r["content"]]
    on_count = len(cron_rows)
    check("cron-style turns collapsed to a single survivor", on_count <= 1)
    if cron_rows:
        check("survivor carries a dup_count annotation", cron_rows[0].get("dup_count", 0) >= 2)

    os.environ["MEMNOS_RECALL_DEDUP"] = "0"
    # ISOLATE the variable under test: the #17 entity arm is an uncontrolled third
    # variable here (it boosts/demotes fact candidates, which can change which rows
    # survive the quotas and swamp the dedup contrast on CI's float distribution). Pin
    # it OFF so this check measures ONLY the dedup kill switch — same philosophy as the
    # MEMNOS_RERANK=0 determinism pin (module docstring). The entity arm has its own
    # dedicated coverage in tests/test_recall_entity_scope.py.
    os.environ["MEMNOS_RECALL_ENTITY_BOOST"] = "0"
    os.environ["MEMNOS_RECALL_ENTITY_SCOPE"] = "0"
    try:
        off_count = cron_count(mem.recall(NS, q))
        # POLICY (not a brittle absolute count): turning dedup OFF must let the collapsed
        # duplicates RETURN — strictly more cron turns survive than with dedup on, and the
        # ~10 seeded exact-dups are no longer folded into one survivor.
        check("kill switch MEMNOS_RECALL_DEDUP=0: duplicates return (more than with dedup on)",
              off_count > on_count and off_count >= 2)
    finally:
        del os.environ["MEMNOS_RECALL_DEDUP"]
        del os.environ["MEMNOS_RECALL_ENTITY_BOOST"]
        del os.environ["MEMNOS_RECALL_ENTITY_SCOPE"]


# --- 3. fact-first: the answer facts lead the list-intent result + context ----------
def test_fact_first(mem):
    print("fact-first: answer facts surface above the turns (list intent)")
    q = "what projects does the HDIG MR reviewer monitor"
    rows = mem.recall(NS, q)
    kinds = [r["kind"] for r in rows]
    check("list (fact-first ON): the FIRST result is a fact",
          rows and rows[0]["kind"] == "fact")
    first_turn = kinds.index("turn") if "turn" in kinds else len(kinds)
    fact_idx = [i for i, k in enumerate(kinds) if k == "fact"]
    check("list: at least 2 answer facts present",
          sum(1 for r in rows if r["kind"] == "fact") >= 2)
    check("list: all fact rows precede the first raw turn",
          fact_idx and all(i < first_turn for i in fact_idx))
    check("list: a monitored-project fact is present",
          any("project" in r["content"] for r in rows if r["kind"] == "fact"))

    ctx = mem.context(NS, q)
    first_line = ctx.splitlines()[0] if ctx else ""
    check("list: rendered context LEADS with a fact line",
          first_line.startswith("- (fact"))

    # kill switch: fact-first off -> rendered context no longer forced fact-first
    os.environ["MEMNOS_RECALL_FACT_FIRST"] = "0"
    # ISOLATE the variable under test: pin the #17 entity arm OFF so this contrast
    # measures ONLY the fact-first kill switch. The entity arm reshapes fact-candidate
    # scores (uncontrolled third variable) and on CI's float distribution that can change
    # which row leads independently of fact-first — swamping the flip this check asserts.
    # Same hygiene as the MEMNOS_RERANK=0 pin; entity arm covered by test_recall_entity_scope.py.
    os.environ["MEMNOS_RECALL_ENTITY_BOOST"] = "0"
    os.environ["MEMNOS_RECALL_ENTITY_SCOPE"] = "0"
    try:
        rows_off = mem.recall(NS, q)
        # POLICY contrast: with fact-first ON a fact led (asserted above); with it OFF the
        # forced fact-first assembly no longer fires, so the raw-turn arm leads again. The
        # toggle FLIPS the lead — proving the kill switch genuinely controls the behavior.
        check("kill switch MEMNOS_RECALL_FACT_FIRST=0: a turn leads the result (lead flips)",
              rows_off and rows_off[0]["kind"] == "turn"
              and rows and rows[0]["kind"] == "fact")
    finally:
        del os.environ["MEMNOS_RECALL_FACT_FIRST"]
        del os.environ["MEMNOS_RECALL_ENTITY_BOOST"]
        del os.environ["MEMNOS_RECALL_ENTITY_SCOPE"]


# --- 4. verbatim guard: raw turn must stay first ------------------------------------
def test_verbatim_guard(mem):
    print("verbatim guard: a verbatim-intent query keeps the raw turn first")
    q = "what exactly did the reviewer say about the cron job?"
    rows = mem.recall(NS, q)
    check("verbatim: first result is a raw turn", rows and rows[0]["kind"] == "turn")
    ctx = mem.context(NS, q)
    first_line = ctx.splitlines()[0] if ctx else ""
    check("verbatim: rendered context LEADS with a said/turn line",
          first_line.startswith("- (said"))


# --- 5. fact-boost gating: bounded + verbatim-exempt --------------------------------
def test_fact_boost_gate(mem):
    print("fact-boost: gated (non-verbatim only) and bounded")
    real = svc.brain_rerank.rerank
    svc.brain_rerank.rerank = lambda q, cands, m=None: [(i, 0.5) for i in range(len(cands))]
    try:
        import types
        intent = types.SimpleNamespace(temporal=False)
        b = {"intent": intent, "ents": [],
             "raw": [{"content": "a turn that mentions projects once"}],
             "sem": [{"content": "fact about a monitored project", "restatements": 0,
                      "salience": 0.5}]}
        rows_list = mem.recall_rank("which projects are monitored?", dict(b))
        f = [r for r in rows_list if r["kind"] == "fact"][0]
        check("non-verbatim: fact score got a bounded boost (> base, <= +10%)",
              0.5 < f["score"] <= 0.5 * 1.10 + 1e-9)

        rows_vb = mem.recall_rank('what exactly did they say "projects"?', dict(b))
        fv = [r for r in rows_vb if r["kind"] == "fact"][0]
        check("verbatim: NO fact boost (score == base)", abs(fv["score"] - 0.5) < 1e-9)

        os.environ["MEMNOS_RECALL_FACT_BOOST"] = "0"
        try:
            rows0 = mem.recall_rank("which projects are monitored?", dict(b))
            f0 = [r for r in rows0 if r["kind"] == "fact"][0]
            check("kill switch MEMNOS_RECALL_FACT_BOOST=0: score == base",
                  abs(f0["score"] - 0.5) < 1e-9)
        finally:
            del os.environ["MEMNOS_RECALL_FACT_BOOST"]
    finally:
        svc.brain_rerank.rerank = real


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

    test_intent()
    seed(store, crafted_embed)
    mem = MemnosMemory(store, crafted_embed, dim=dim, llm=None)
    # Pin retrieval (RRF) order for the DB-backed pipeline checks so the POLICY
    # assertions are CI-deterministic (cross-encoder floats vary across ONNX builds —
    # see module docstring). test_fact_boost_gate stubs the reranker itself.
    _rerank_saved = os.environ.get("MEMNOS_RERANK")
    os.environ["MEMNOS_RERANK"] = "0"
    try:
        test_dedup(mem)
        test_fact_first(mem)
        test_verbatim_guard(mem)
    finally:
        if _rerank_saved is None:
            os.environ.pop("MEMNOS_RERANK", None)
        else:
            os.environ["MEMNOS_RERANK"] = _rerank_saved
    test_fact_boost_gate(mem)

    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
