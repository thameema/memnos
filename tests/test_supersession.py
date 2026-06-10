"""Belief-change supersession regression (issue #8 guard).

The supersession UPDATEs gained partial btree indexes (sem_supersede_pred /
sem_supersede_subj) so they no longer seq-scan the namespace per fact write. The UPDATE
predicates themselves are UNCHANGED — this test pins the classic behaviors so any future
"candidate-limited" optimization can't silently stop supersession from firing:

  1. lives_in Austin -> lives_in Seattle  : old fact gets valid_to (superseded)
  2. did_activity (multi-valued)          : NEVER superseded (additive)
  3. older fact arriving later            : does NOT supersede the newer one

Engine-level (no LLM, no embedding API): facts are injected via write_facts with a stub
embedder, exactly the path /remember P3 runs.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.store import BrainStore
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
NS = "test:supersession"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def main():
    store = BrainStore(DSN)
    schema = "tenant_memnos"
    # detect the schema's embedding dim so the stub vectors match (384 local / 1536 openai)
    with store.conn.cursor() as c:
        c.execute("SELECT atttypmod AS d FROM pg_attribute "
                  "WHERE attrelid='tenant_memnos.semantic'::regclass AND attname='embedding'")
        dim = c.fetchone()["d"]
    if not dim or dim < 1:
        dim = 384

    def stub_embed(text):
        # deterministic, text-dependent — distance between different texts is nonzero
        h = abs(hash(text))
        return [((h >> (i % 32)) & 1) * 0.1 + 0.001 for i in range(dim)]

    mem = MemnosMemory(store, stub_embed, dim=dim, llm=None)
    # clean slate
    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {schema}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {schema}.raw_turns WHERE namespace=%s", (NS,))

    print("=== belief-change supersession regression ===")
    d1 = datetime(2024, 1, 10, tzinfo=timezone.utc)
    d2 = datetime(2025, 6, 1, tzinfo=timezone.utc)
    tid = store.insert_raw_turn(schema, NS, None, "user", "seed turn", d1, stub_embed("seed turn"))

    # the supersession indexes exist (added for issue #8)
    with store.conn.cursor() as c:
        c.execute("SELECT indexname FROM pg_indexes WHERE schemaname='tenant_memnos' "
                  "AND indexname IN ('sem_supersede_pred','sem_supersede_subj')")
        idx = {r["indexname"] for r in c.fetchall()}
    check("sem_supersede_pred index exists", "sem_supersede_pred" in idx)
    check("sem_supersede_subj index exists", "sem_supersede_subj" in idx)

    # 1. classic lives-in: new value closes out the old one
    f_austin = {"subject": "Melanie", "predicate": "lives_in", "object": "Austin",
                "statement": "Melanie lives in Austin."}
    f_seattle = {"subject": "Melanie", "predicate": "lives_in", "object": "Seattle",
                 "statement": "Melanie lives in Seattle."}
    nf, ns_ = mem.write_facts(NS, [f_austin], d1, turn_id=tid)
    check("first lives_in stored, nothing to supersede", nf == 1 and ns_ == 0)
    nf, ns_ = mem.write_facts(NS, [f_seattle], d2, turn_id=tid)
    check("new lives_in supersedes the old value", nf == 1 and ns_ == 1)
    with store.conn.cursor() as c:
        c.execute(f"SELECT object, valid_to FROM {schema}.semantic "
                  f"WHERE namespace=%s AND predicate='lives_in' ORDER BY id", (NS,))
        rows = c.fetchall()
    check("Austin fact got valid_to (history kept, not deleted)",
          rows[0]["object"] == "Austin" and rows[0]["valid_to"] is not None)
    check("Seattle fact is the current one (valid_to IS NULL)",
          rows[1]["object"] == "Seattle" and rows[1]["valid_to"] is None)

    # 2. multi-valued predicate stays ADDITIVE (a new martial art doesn't replace one)
    a1 = {"subject": "Melanie", "predicate": "did_activity", "object": "kickboxing",
          "statement": "Melanie did kickboxing."}
    a2 = {"subject": "Melanie", "predicate": "did_activity", "object": "taekwondo",
          "statement": "Melanie did taekwondo."}
    mem.write_facts(NS, [a1], d1, turn_id=tid)
    nf, ns_ = mem.write_facts(NS, [a2], d2, turn_id=tid)
    check("multi-valued predicate is additive (no supersession)", ns_ == 0)
    with store.conn.cursor() as c:
        c.execute(f"SELECT count(*) AS n FROM {schema}.semantic WHERE namespace=%s "
                  f"AND predicate='did_activity' AND valid_to IS NULL", (NS,))
        check("both activities remain current", c.fetchone()["n"] == 2)

    # 3. an OLDER fact arriving later must not supersede the newer current value
    f_old = {"subject": "Melanie", "predicate": "lives_in", "object": "Boston",
             "statement": "Melanie lived in Boston."}
    nf, ns_ = mem.write_facts(NS, [f_old], datetime(2020, 1, 1, tzinfo=timezone.utc), turn_id=tid)
    with store.conn.cursor() as c:
        c.execute(f"SELECT valid_to FROM {schema}.semantic WHERE namespace=%s "
                  f"AND object='Seattle'", (NS,))
        check("backdated fact does not supersede the newer current value",
              c.fetchone()["valid_to"] is None)

    # cleanup
    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {schema}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {schema}.raw_turns WHERE namespace=%s", (NS,))
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
