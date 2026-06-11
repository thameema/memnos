"""Broad-query recall ranking (issue #11).

On broad status questions ("where are we with the deployment?") recall used to lead
with long meta-conversation RAW TURNS (they embed close to everything) while the
distilled semantic facts that actually answer the question ranked below them. The
issue #11 tune is rank-time only (storage untouched, same rows, same quotas):

  1. query-specificity heuristic (deterministic, no LLM): broad questions put
     facts/dossiers AHEAD of raw turns; specific verbatim questions ("what exactly
     did X say...") and neutral LoCoMo-style questions keep the turn-first order;
  2. mild log-length penalty on raw-turn scores at rank time;
  3. bounded restatements/salience reinforcement boost on fact scores;
  4. kill switch: MEMNOS_BROAD_QUERY_TUNE=0 restores the old behavior exactly
     (individual knobs MEMNOS_TURN_LENGTH_PENALTY=0 / MEMNOS_SALIENCE_BOOST=0).

Engine-level (no LLM, no embedding API): rows are seeded directly with a crafted-vector
stub embedder; recall() runs the REAL production retrieval + rerank pipeline. The
score-shaping checks patch the cross-encoder with uniform scores so length/restatement
effects are isolated deterministically.
"""
import math
import os
import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.store import BrainStore
from core import service as svc
from core.service import MemnosMemory, query_specificity

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
NS = "test:broadrank"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


# --- 1. query-specificity battery (pure function, no DB) ---------------------------
def test_specificity():
    print("specificity heuristic")
    battery = [
        # broad status questions — must classify broad
        ("where are we with the deployment?", "broad"),
        ("what is the status of the migration", "broad"),
        ("catch me up on the project", "broad"),
        ("give me an overview of progress", "broad"),
        ("whats the latest on the deployment?", "broad"),
        ("how is the rollout going?", "broad"),
        ("bring me up to speed", "broad"),
        ("quick recap of where things stand", "broad"),
        # verbatim/specific — raw turns must stay on top
        ("what exactly did Sam say about the deployment?", "specific"),
        ("quote what Alice told the team", "specific"),
        ('did anyone mention "node pool" capacity?', "specific"),
        ("what did Priya say word for word?", "specific"),
        ("how did Bob describe the outage?", "specific"),
        # LoCoMo-style questions — must NOT trip the broad pathway
        ("When did Caroline go to the LGBTQ support group?", "neutral"),
        ("What did Melanie paint in June 2023?", "neutral"),
        ("Would Melanie consider herself religious?", "neutral"),
        ("How does Caroline feel about adoption?", "neutral"),
        ("What instrument does John play?", "neutral"),
        ("Where did Melanie go camping last summer?", "neutral"),
        ("What pets does Caroline have?", "neutral"),
        # entity-dense status question — about THOSE entities, stays neutral
        ("what is the status of Apollo and Hermes services in Frankfurt?", "neutral"),
    ]
    for q, want in battery:
        check(f"{want:8s} <- {q[:58]}", query_specificity(q) == want)


# --- 2/3. end-to-end arm ordering over seeded rows ----------------------------------
LONG_TURNS = [
    ("Priya", "Okay so quick sync on a bunch of things. First, about the deployment — "
     "we talked about it yesterday and honestly the discussion went in circles for a "
     "while. Someone brought up the memory system's own answer about the deployment "
     "which was funny. We also covered the hiring pipeline: two candidates moved to "
     "onsite, one declined. The offsite agenda needs an owner; Marcus volunteered "
     "then un-volunteered. On the billing rewrite, the proto schema review is "
     "scheduled. Lunch options near the new office are terrible, we should expense "
     "delivery. Also the deployment came up again at the end when someone asked "
     "whether the runbook was updated — nobody knew. Then we spent twenty minutes "
     "on the conference talk submissions and the team t-shirt design poll, and "
     "agreed to revisit the on-call rotation next week after the holiday schedule "
     "is published. Long meeting, few decisions."),
    ("Marcus", "Replying to the thread above — I think the deployment discussion "
     "yesterday missed the point. We keep relitigating the same tradeoffs. For the "
     "record I also want to flag: the analytics dashboard migration, the Q3 budget "
     "asks, the intern project scoping, and whether we should move the standup to "
     "9:30. On the deployment specifically, my view is that the conversation about "
     "the conversation is itself the problem — we narrate status instead of shipping. "
     "Anyway, the design doc for the cache layer is out for review, the security "
     "questionnaire from the enterprise prospect is due Friday, and someone needs to "
     "renew the staging TLS cert before it expires. Adding all this here so it's "
     "captured somewhere because the notes doc is a mess and nobody reads it."),
]
FACTS = [
    "The zeta deployment is 80 percent complete as of 2026-06-10.",
    "The zeta deployment's remaining work is the smoke-test suite only.",
    "The zeta deployment is unblocked; the database capacity problem was resolved.",
]


def seed(store, embed):
    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
    d = datetime(2026, 6, 10, tzinfo=timezone.utc)
    for spk, txt in LONG_TURNS:
        store.insert_raw_turn(SCHEMA, NS, None, spk, txt, d, embed(txt))
    # a short verbatim turn the specific-question check should surface
    short = "Priya: the deployment is basically done, only smoke tests left."
    store.insert_raw_turn(SCHEMA, NS, None, "Priya", short, d, embed(short))
    for f in FACTS:
        store.insert_semantic(SCHEMA, NS, "fact", f, valid_from=d, salience=0.5,
                              vec=embed(f))


def first_kinds(rows, n=6):
    return [r["kind"] for r in rows[:n]]


def test_arm_ordering(mem):
    print("broad vs specific arm ordering (real pipeline)")
    broad_q = "where are we with the deployment?"
    rows = mem.recall(NS, broad_q)
    check("broad: results non-empty", len(rows) > 0)
    kinds = [r["kind"] for r in rows]
    check("broad: first row is a fact", kinds and kinds[0] == "fact")
    first_turn = kinds.index("turn") if "turn" in kinds else len(kinds)
    fact_idx = [i for i, k in enumerate(kinds) if k == "fact"]
    n_facts_present = sum(1 for r in rows if r["kind"] == "fact")
    check("broad: all fact rows precede the first turn",
          all(i < first_turn for i in fact_idx))
    check("broad: distilled deployment facts are in the results",
          any("smoke-test" in r["content"] for r in rows if r["kind"] == "fact"))
    check("broad: long meta-turns did not crowd out facts",
          n_facts_present >= 2)

    specific_q = "what exactly did Priya say about the deployment?"
    rows_s = mem.recall(NS, specific_q)
    check("specific: first row is a raw turn", rows_s and rows_s[0]["kind"] == "turn")

    neutral_q = "When did the smoke tests run?"
    rows_n = mem.recall(NS, neutral_q)
    check("neutral: turn-first order preserved",
          rows_n and rows_n[0]["kind"] == "turn")


def test_kill_switch(mem):
    print("kill switch: MEMNOS_BROAD_QUERY_TUNE=0 restores old behavior")
    os.environ["MEMNOS_BROAD_QUERY_TUNE"] = "0"
    try:
        rows = mem.recall(NS, "where are we with the deployment?")
        check("switch off: first row is a raw turn again",
              rows and rows[0]["kind"] == "turn")
    finally:
        del os.environ["MEMNOS_BROAD_QUERY_TUNE"]


# --- 4. score shaping, isolated with a uniform-score reranker -----------------------
def test_score_shaping(mem):
    print("score shaping (uniform reranker, deterministic)")
    real = svc.brain_rerank.rerank
    svc.brain_rerank.rerank = lambda q, cands, m=None: [(i, 0.9) for i in range(len(cands))]
    try:
        intent = types.SimpleNamespace(temporal=False)
        long_turn = {"content": "deployment " * 300}          # ~3300 chars
        short_turn = {"content": "the deployment is nearly done"}
        b = {"intent": intent, "ents": [],
             "raw": [long_turn, short_turn], "sem": []}
        rows = mem.recall_rank("any neutral question", dict(b))
        check("length penalty: short turn outranks padded long turn at equal base score",
              rows[0]["content"] == short_turn["content"])
        check("length penalty: scores strictly ordered",
              rows[0]["score"] > rows[1]["score"])

        os.environ["MEMNOS_TURN_LENGTH_PENALTY"] = "0"
        rows0 = mem.recall_rank("any neutral question", dict(b))
        del os.environ["MEMNOS_TURN_LENGTH_PENALTY"]
        check("length penalty knob=0: original rerank order kept (stable tie)",
              rows0[0]["content"] == long_turn["content"]
              and rows0[0]["score"] == rows0[1]["score"])

        f_plain = {"content": "fact stated once", "restatements": 0, "salience": 0.5}
        f_rest = {"content": "fact restated fifty times", "restatements": 50, "salience": 1.0}
        b2 = {"intent": intent, "ents": [], "raw": [],
              "sem": [f_plain, f_rest]}
        rows2 = mem.recall_rank("any neutral question", dict(b2))
        check("restatement boost: 50x-restated fact outranks once-stated fact",
              rows2[0]["content"] == f_rest["content"])
        check("restatement boost is bounded (<= +25%)",
              rows2[0]["score"] <= 0.9 * 1.25 + 1e-9)

        os.environ["MEMNOS_SALIENCE_BOOST"] = "0"
        rows20 = mem.recall_rank("any neutral question", dict(b2))
        del os.environ["MEMNOS_SALIENCE_BOOST"]
        check("salience knob=0: original order kept",
              rows20[0]["content"] == f_plain["content"])

        # wide-rank path carries the same tune
        wt = [dict(long_turn, _ns="ns1"), dict(short_turn, _ns="ns1")]
        wf = [dict(f_plain, _ns="ns1"), dict(f_rest, _ns="ns1")]
        wrows = mem.recall_wide_rank("where are we with the deployment?", wt, wf)
        check("wide rank broad: facts lead", wrows[0]["kind"] == "fact")
        check("wide rank: restated fact first among facts",
              wrows[0]["content"] == f_rest["content"])
        wrows_n = mem.recall_wide_rank("a neutral question", wt, wf)
        check("wide rank neutral: turns lead", wrows_n[0]["kind"] == "turn")
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

    test_specificity()
    seed(store, crafted_embed)
    mem = MemnosMemory(store, crafted_embed, dim=dim, llm=None)
    test_arm_ordering(mem)
    test_kill_switch(mem)
    test_score_shaping(mem)

    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
