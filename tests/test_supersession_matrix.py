"""Write-path supersession field-regression matrix (the broken-supersession fix).

Field diagnosis (all six were FAILING before the fix):
  1. status flip                ("status" predicate)            -> SPO supersession
  2. functional predicate change ("uses" Postgres -> MySQL)     -> SPO supersession (new cue)
  3. explicit negation           (pred=None on the reversal)    -> near-neighbour close-out
  4. value update                ("can_handle" 100 -> 500 rps)  -> SPO supersession (new cue)
  5. verbatim duplicate                                          -> write-path dedupe
  6. backdated NEW assertion     ("moved last week" resolves an event date EARLIER than
                                  the old fact's valid_from)     -> observation-axis guard

Plus the FALSE-POSITIVE guards the fix must not violate:
  - a backdated HISTORICAL statement ("lived in Boston in 2019") must NOT supersede the
    current value (same semantics tests/test_supersession.py pins);
  - an unrelated live fact about the SAME entity must NOT be closed by a negation;
  - multi-valued predicates stay additive; substring traps ('causes') stay multi-valued;
  - MEMNOS_DEDUPE_THRESHOLD=0 disables the dedupe.

Engine-level (no LLM, no embedding API): facts are injected via write_facts with a
CRAFTED-vector stub embedder (controlled pairwise cosine distances), exactly the path
/remember P3 runs.
"""
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.store import BrainStore
from core.service import MemnosMemory, _is_single_valued

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


# --- crafted-vector stub embedder: distance(a,b) = 1 - cos(angle_a - angle_b) ----------
ANGLES = {
    # case 3 negation cluster: blocked-fact at 0, reversal at dist 0.20, an UNRELATED
    # same-entity fact placed INSIDE the distance threshold (dist 0.30 to the reversal)
    # so only the token-overlap guard can save it.
    "Project zeta is blocked by the database migration.": 0.0,
    "Project zeta is no longer blocked.": math.acos(1 - 0.20),
    "Project zeta added new dashboards last sprint.": math.acos(1 - 0.20) + math.acos(1 - 0.30),
    # case 6 cluster — far apart pairwise (no dedupe/negation interference)
    "Alice lives in Austin.": 3.0,
    "Alice moved to Seattle on 2026-06-04.": 4.0,
    "Alice lived in Boston in 2019.": 4.8,
}


def main():
    store = BrainStore(DSN)
    schema = "tenant_memnos"
    with store.conn.cursor() as c:
        c.execute("SELECT atttypmod AS d FROM pg_attribute "
                  "WHERE attrelid='tenant_memnos.semantic'::regclass AND attname='embedding'")
        dim = c.fetchone()["d"]
    if not dim or dim < 1:
        dim = 384

    _auto = {}

    def crafted_embed(text):
        theta = ANGLES.get(text)
        if theta is None:                      # default: a distinct well-separated angle
            # spacing 0.35 rad -> pairwise distance >= 0.06, safely above the 0.03
            # dedupe threshold (namespaces isolate the crafted clusters anyway)
            theta = _auto.setdefault(text, 1.9 + 0.35 * len(_auto))
        v = [0.0] * dim
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v

    mem = MemnosMemory(store, crafted_embed, dim=dim, llm=None)

    def reset(ns):
        with store.conn.cursor() as c:
            c.execute(f"DELETE FROM {schema}.semantic WHERE namespace=%s", (ns,))
            c.execute(f"DELETE FROM {schema}.raw_turns WHERE namespace=%s", (ns,))

    def rows(ns, **eq):
        cond = " AND ".join(f"{k}=%s" for k in eq)
        with store.conn.cursor() as c:
            c.execute(f"SELECT id, statement, subject_entity, predicate, object, valid_from, "
                      f"valid_to, superseded_by, restatements, salience, source_turn_ids "
                      f"FROM {schema}.semantic WHERE namespace=%s"
                      + (f" AND {cond}" if cond else "") + " ORDER BY id",
                      (ns, *eq.values()))
            return c.fetchall()

    d1 = datetime(2026, 6, 8, tzinfo=timezone.utc)
    d2 = datetime(2026, 6, 11, tzinfo=timezone.utc)
    os.environ.pop("MEMNOS_DEDUPE_THRESHOLD", None)
    os.environ.pop("MEMNOS_NEGATION_THRESHOLD", None)

    print("=== write-path supersession matrix ===")

    # --- 1. STATUS FLIP --------------------------------------------------------------
    ns = "test:matrix:1"; reset(ns)
    tid = store.insert_raw_turn(schema, ns, None, "u", "seed", d1, crafted_embed("seed"))
    mem.write_facts(ns, [{"subject": "zeta deployment", "predicate": "status",
                          "object": "blocked", "statement": "The zeta deployment status is blocked."}], d1, tid)
    nf, nsup = mem.write_facts(ns, [{"subject": "zeta deployment", "predicate": "status",
                                     "object": "resolved", "statement": "The zeta deployment status is resolved."}], d2, tid)
    r = rows(ns, predicate="status")
    check("1. status flip supersedes", nsup == 1 and r[0]["valid_to"] is not None and r[1]["valid_to"] is None)
    check("1. superseded_by links old -> new", r[0]["superseded_by"] == r[1]["id"])

    # --- 2. FUNCTIONAL PREDICATE CHANGE (uses) ----------------------------------------
    ns = "test:matrix:2"; reset(ns)
    tid = store.insert_raw_turn(schema, ns, None, "u", "seed", d1, crafted_embed("seed"))
    mem.write_facts(ns, [{"subject": "ingest pipeline", "predicate": "uses",
                          "object": "Postgres", "statement": "The ingest pipeline uses Postgres."}], d1, tid)
    nf, nsup = mem.write_facts(ns, [{"subject": "ingest pipeline", "predicate": "uses",
                                     "object": "MySQL", "statement": "The ingest pipeline switched to MySQL."}], d2, tid)
    r = rows(ns, predicate="uses")
    check("2. functional predicate change supersedes", nsup == 1
          and r[0]["object"] == "Postgres" and r[0]["valid_to"] is not None and r[1]["valid_to"] is None)

    # --- 3. EXPLICIT NEGATION (pred=None on the reversal) ------------------------------
    ns = "test:matrix:3"; reset(ns)
    tid = store.insert_raw_turn(schema, ns, None, "u", "seed", d1, crafted_embed("seed"))
    mem.write_facts(ns, [{"subject": "Project zeta", "predicate": "is_blocked_by",
                          "object": "database migration",
                          "statement": "Project zeta is blocked by the database migration."}], d1, tid)
    # FP guard target: unrelated live fact about the SAME entity, vector-NEAR the reversal
    mem.write_facts(ns, [{"subject": "Project zeta", "predicate": "",
                          "object": "", "statement": "Project zeta added new dashboards last sprint."}], d1, tid)
    nf, nsup = mem.write_facts(ns, [{"subject": "Project zeta", "predicate": "",
                                     "object": "", "statement": "Project zeta is no longer blocked."}], d2, tid)
    blocked = rows(ns, predicate="is_blocked_by")[0]
    dash = [x for x in rows(ns) if "dashboards" in x["statement"]][0]
    new = [x for x in rows(ns) if "no longer" in x["statement"]][0]
    check("3. negation closes the blocked fact", nsup == 1 and blocked["valid_to"] is not None)
    check("3. superseded_by links blocked -> negation", blocked["superseded_by"] == new["id"])
    check("3. FP guard: unrelated same-entity fact stays live", dash["valid_to"] is None)

    # --- 4. VALUE UPDATE (can_handle) ---------------------------------------------------
    ns = "test:matrix:4"; reset(ns)
    tid = store.insert_raw_turn(schema, ns, None, "u", "seed", d1, crafted_embed("seed"))
    mem.write_facts(ns, [{"subject": "API", "predicate": "can_handle",
                          "object": "100 rps", "statement": "The API can handle 100 requests per second."}], d1, tid)
    nf, nsup = mem.write_facts(ns, [{"subject": "API", "predicate": "can_handle",
                                     "object": "500 rps", "statement": "The API can now handle 500 requests per second."}], d2, tid)
    r = rows(ns, predicate="can_handle")
    check("4. value update supersedes", nsup == 1 and r[0]["valid_to"] is not None and r[1]["valid_to"] is None)

    # --- 5. VERBATIM DUPLICATE (write-path dedupe) --------------------------------------
    ns = "test:matrix:5"; reset(ns)
    tid1 = store.insert_raw_turn(schema, ns, None, "u", "seed1", d1, crafted_embed("seed1"))
    tid2 = store.insert_raw_turn(schema, ns, None, "u", "seed2", d2, crafted_embed("seed2"))
    f = {"subject": "Marcus", "predicate": "prefers", "object": "PostgreSQL",
         "statement": "Marcus prefers PostgreSQL over MySQL for analytics workloads."}
    mem.write_facts(ns, [f], d1, tid1)
    nf, nsup = mem.write_facts(ns, [dict(f)], d2, tid2)
    r = rows(ns)
    check("5. duplicate is NOT inserted", nf == 0 and len(r) == 1)
    check("5. restatements counter + salience bump", r[0]["restatements"] == 1 and r[0]["salience"] > 0.5)
    check("5. source_turn_ids extended", set(r[0]["source_turn_ids"] or []) == {tid1, tid2})
    # disable via env -> duplicate inserts again
    os.environ["MEMNOS_DEDUPE_THRESHOLD"] = "0"
    nf, _ = mem.write_facts(ns, [dict(f)], d2, tid2)
    os.environ.pop("MEMNOS_DEDUPE_THRESHOLD", None)
    check("5. MEMNOS_DEDUPE_THRESHOLD=0 disables dedupe", nf == 1 and len(rows(ns)) == 2)

    # --- 6. BACKDATED NEW ASSERTION ("moved last week") ---------------------------------
    ns = "test:matrix:6"; reset(ns)
    tid = store.insert_raw_turn(schema, ns, None, "u", "seed", d1, crafted_embed("seed"))
    mem.write_facts(ns, [{"subject": "Alice", "predicate": "lives_in", "object": "Austin",
                          "statement": "Alice lives in Austin."}], d1, tid)
    # extraction resolved "last week" to an ABSOLUTE date EARLIER than Austin's valid_from
    nf, nsup = mem.write_facts(ns, [{"subject": "Alice", "predicate": "lives_in", "object": "Seattle",
                                     "statement": "Alice moved to Seattle on 2026-06-04."}], d2, tid)
    r = rows(ns, predicate="lives_in")
    check("6. backdated NEW assertion supersedes (observation axis)",
          nsup == 1 and r[0]["object"] == "Austin" and r[0]["valid_to"] is not None)
    check("6. Seattle is current; its event date kept", r[1]["valid_to"] is None
          and r[1]["valid_from"].astimezone(timezone.utc).date().isoformat() == "2026-06-04")
    check("6. valid_to clamped to old valid_from (no inverted interval)",
          r[0]["valid_to"] >= r[0]["valid_from"])
    # FP guard: backdated HISTORICAL statement observed even later must NOT supersede
    d3 = datetime(2026, 6, 12, tzinfo=timezone.utc)
    nf, nsup = mem.write_facts(ns, [{"subject": "Alice", "predicate": "lives_in", "object": "Boston",
                                     "statement": "Alice lived in Boston in 2019."}], d3, tid)
    seattle = [x for x in rows(ns, predicate="lives_in") if x["object"] == "Seattle"][0]
    check("6. FP guard: backdated HISTORICAL statement does not supersede",
          nsup == 0 and seattle["valid_to"] is None)

    # --- cue-list review guards ----------------------------------------------------------
    for p in ("is_blocked_by", "uses", "runs_on", "can_handle", "recommended_action",
              "status", "version", "lives_in", "works_at", "deployed_version"):
        check(f"cue: '{p}' is single-valued", _is_single_valued(p))
    for p in ("causes", "houses", "did_activity", "visited", "likes", "met_person",
              "owns", "recommended_books", ""):
        check(f"cue: '{p}' stays additive", not _is_single_valued(p))

    # cleanup
    for i in range(1, 7):
        reset(f"test:matrix:{i}")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
