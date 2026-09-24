"""issue #156 — contradiction precision + present-tense passive supersession.

Bug 1: BrainStore.contradictions() counted EVERY live (subject, predicate) group with >1
distinct object as a contradiction, including additive/multi-valued predicates
(did_activity, includes, has, ...) that the write path itself (`_supersedable`) never
treats as single-valued — ~92% false positives on the live namespace. health() also
capped the count at 1000 (len of a LIMIT 1000 list) and its score penalty
`min(40, groups*5)` saturated at 8 groups, so the score could not move.

Bug 2: _HISTORICAL_RE matched a bare `was`/`were`, so ordinary present-state passive
reports ("The branch was merged", "Host2 was root-compromised", "The changes were pushed
to main") were classed as past-state statements and, lacking an explicit date, could
NEVER supersede an older conflicting value.

Engine-level (no server, no LLM): facts go through MemnosMemory.write_facts with a
crafted stub embedder (same pattern as test_supersession_matrix.py), and the
contradiction/health signals are read straight from BrainStore.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python tests/test_contradiction_precision.py
"""
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.control import Control
from core.store import BrainStore
from core.service import MemnosMemory, reconcile_namespace, _supersedable

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PREFIX = "test:contra156:"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))
    PASS += bool(cond); FAIL += (not cond)


def main():
    store = BrainStore(DSN)
    Control.init(store.conn)
    with store.conn.cursor() as c:
        c.execute("SELECT to_regclass(%s) AS t", (f"{SCHEMA}.semantic",))
        exists = c.fetchone()["t"] is not None
    if not exists:
        store.create_schema("memnos", dim=384)
    with store.conn.cursor() as c:
        c.execute("SELECT atttypmod AS d FROM pg_attribute "
                  f"WHERE attrelid='{SCHEMA}.semantic'::regclass AND attname='embedding'")
        dim = c.fetchone()["d"]
    if not dim or dim < 1:
        dim = 384

    _auto = {}

    def crafted_embed(text):
        # every statement gets its own well-separated angle -> no dedupe / negation
        # interference; this test is about the SPO gate only.
        theta = _auto.setdefault(text, 0.35 * len(_auto))
        v = [0.0] * dim
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v

    mem = MemnosMemory(store, crafted_embed, dim=dim, llm=None)

    def reset(ns):
        with store.conn.cursor() as c:
            for t in ("semantic", "raw_turns", "entities", "edges"):
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (ns,))

    def rows(ns, **eq):
        cond = " AND ".join(f"{k}=%s" for k in eq)
        with store.conn.cursor() as c:
            c.execute(f"SELECT id, statement, object, valid_to, superseded_by "
                      f"FROM {SCHEMA}.semantic WHERE namespace=%s"
                      + (f" AND {cond}" if cond else "") + " ORDER BY id", (ns, *eq.values()))
            return c.fetchall()

    def bulk_live(ns, groups, *, subj_prefix, predicate, n_objs=2):
        """`groups` live (subject, predicate) groups, each with n_objs distinct objects."""
        with store.conn.cursor() as c:
            c.execute(f"""
                INSERT INTO {SCHEMA}.semantic(namespace, kind, statement, subject_entity, predicate, object)
                SELECT %s, 'proposition', %s || g || ' ' || o, %s || g, %s, 'v' || o
                FROM generate_series(1, %s) g, generate_series(1, %s) o
            """, (ns, subj_prefix, subj_prefix, predicate, groups, n_objs))

    def summary(ns, **kw):
        fn = getattr(store, "contradiction_summary", None)
        return fn(SCHEMA, ns, **kw) if fn else None

    os.environ.pop("MEMNOS_DEDUPE_THRESHOLD", None)
    os.environ.pop("MEMNOS_NEGATION_THRESHOLD", None)
    d1 = datetime(2026, 6, 8, tzinfo=timezone.utc)
    d2 = datetime(2026, 6, 11, tzinfo=timezone.utc)

    # ======================================================================================
    print("=== Bug 1a: contradictions() only counts single-valued predicates ===")
    ns = PREFIX + "a"; reset(ns)
    for obj in ("recall", "remember", "knowledge health"):           # additive: includes
        store.insert_semantic(SCHEMA, ns, "proposition", f"memnos includes {obj}",
                              subject="memnos", predicate="includes", obj=obj)
    for obj in ("hiking", "swimming"):                                 # additive: did_activity
        store.insert_semantic(SCHEMA, ns, "proposition", f"Alice did {obj}",
                              subject="Alice", predicate="did_activity", obj=obj)
    for obj in ("a CI runner", "a staging database"):                  # additive: has
        store.insert_semantic(SCHEMA, ns, "proposition", f"Host2 has {obj}",
                              subject="Host2", predicate="has", obj=obj)
    for obj in ("healthy", "root-compromised"):                        # single-valued: status
        store.insert_semantic(SCHEMA, ns, "proposition", f"Host2 status is {obj}",
                              subject="Host2", predicate="status", obj=obj)
    for obj in ("8 threads", "32 threads"):                            # quantified-object rule
        store.insert_semantic(SCHEMA, ns, "proposition", f"ingest worker concurrency {obj}",
                              subject="ingest worker", predicate="concurrency", obj=obj)
    check("precondition: write path treats includes/did_activity/has as additive",
          not any(_supersedable(p, o) for p, o in
                  (("includes", "recall"), ("did_activity", "hiking"), ("has", "a CI runner"))))
    cons = store.contradictions(SCHEMA, ns)
    preds = {(g["subject"], g["predicate"]) for g in cons}
    check("multi-valued 'includes' group NOT reported", ("memnos", "includes") not in preds, str(preds))
    check("multi-valued 'did_activity' group NOT reported", ("Alice", "did_activity") not in preds, str(preds))
    check("multi-valued 'has' group NOT reported", ("Host2", "has") not in preds, str(preds))
    check("single-valued status conflict IS reported", ("Host2", "status") in preds, str(preds))
    check("quantified-object conflict IS reported", ("ingest worker", "concurrency") in preds, str(preds))
    check("exactly the 2 genuine conflicts reported", len(cons) == 2, str(preds))
    h = store.health(SCHEMA, ns)
    check("health contradiction_groups == 2 (not 5)", h.get("contradiction_groups") == 2, str(h))
    check("health contested_facts == 4", h.get("contested_facts") == 4, str(h))

    # ======================================================================================
    print("=== Bug 1b: true total is reported separately from the capped list ===")
    ns = PREFIX + "b"; reset(ns)
    bulk_live(ns, 1005, subj_prefix="svc", predicate="status")        # 1005 real conflicts
    bulk_live(ns, 50, subj_prefix="proj", predicate="includes", n_objs=3)  # additive noise
    s = summary(ns, limit=5)
    check("contradiction_summary() exists", s is not None)
    s = s or {}
    check("summary total_groups == 1005 (uncapped)", s.get("total_groups") == 1005, str(s.get("total_groups")))
    check("summary contested_facts == 2010", s.get("contested_facts") == 2010, str(s.get("contested_facts")))
    check("summary sample list honors limit (5)", len(s.get("groups") or []) == 5)
    check("contradictions(limit=5) still returns a 5-item list",
          len(store.contradictions(SCHEMA, ns, limit=5)) == 5)
    h = store.health(SCHEMA, ns)
    check("health contradiction_groups == 1005 (was silently capped at 1000)",
          h.get("contradiction_groups") == 1005, str(h.get("contradiction_groups")))

    # ======================================================================================
    print("=== Bug 1c: score responds across the real range (no saturation at 8 groups) ===")
    scores = []
    for k in (10, 20, 40):
        ns = PREFIX + f"c{k}"; reset(ns)
        bulk_live(ns, k, subj_prefix="svc", predicate="status")                 # 2k contested facts
        bulk_live(ns, 1000 - k, subj_prefix="solo", predicate="status", n_objs=1)  # uncontested
        h = store.health(SCHEMA, ns)
        scores.append(h.get("score"))
        reset(ns)
    check("score strictly decreases 10 -> 20 -> 40 contested groups (of ~1000 facts)",
          None not in scores and scores[0] > scores[1] > scores[2], str(scores))
    check("score for 10 contested groups in ~1000 facts is not pinned at the floor",
          scores[0] is not None and scores[0] > 60, str(scores))
    ns = PREFIX + "c0"; reset(ns)
    bulk_live(ns, 100, subj_prefix="proj", predicate="includes", n_objs=3)      # additive only
    h = store.health(SCHEMA, ns)
    check("additive-only namespace scores 100 (no false contradiction penalty)",
          h.get("score") == 100 and h.get("contradiction_groups") == 0, str(h))
    reset(ns)

    # ======================================================================================
    print("=== Bug 2: present-tense passive was/were statements supersede ===")
    ns = PREFIX + "d"; reset(ns)
    tid = store.insert_raw_turn(SCHEMA, ns, None, "u", "seed", d1, crafted_embed("seed"))
    check("precondition: 'status' is supersedable", _supersedable("status", "merged"))
    cases = [  # (subject, old statement/object, new statement/object) — the sampled field cases
        ("feature branch", "The feature branch status is open.", "open",
         "The branch was merged.", "merged"),
        ("Host2", "Host2 status is healthy.", "healthy",
         "Host2 was root-compromised.", "root-compromised"),
        ("the changes", "The changes status is in review.", "in review",
         "The changes were pushed to main.", "pushed to main"),
        ("deploy pipeline", "The deploy pipeline status is running.", "running",
         "The deploy pipeline was paused.", "paused"),
    ]
    for subj, old_s, old_o, new_s, new_o in cases:
        mem.write_facts(ns, [{"subject": subj, "predicate": "status", "object": old_o,
                              "statement": old_s}], d1, tid)
        nf, nsup = mem.write_facts(ns, [{"subject": subj, "predicate": "status", "object": new_o,
                                         "statement": new_s}], d2, tid)
        r = rows(ns, subject_entity=subj, predicate="status")
        check(f"'{new_s}' supersedes '{old_s}'",
              nsup == 1 and len(r) == 2 and r[0]["valid_to"] is not None
              and r[0]["superseded_by"] == r[1]["id"] and r[1]["valid_to"] is None,
              f"nsup={nsup} rows={[(x['object'], x['valid_to']) for x in r]}")

    print("=== Bug 2 guard: genuinely historical was/were statements still do NOT supersede ===")
    hist_cases = [  # (subject, predicate, old, new) — each new statement carries a real past-time marker
        ("Alice", "based_in", ("Alice is based in Austin.", "Austin"),
         ("Alice was based in Denver in 2019.", "Denver")),                 # year
        ("Host3", "status", ("Host3 status is healthy.", "healthy"),
         ("Host3 was offline two years ago.", "offline")),                  # 'ago'
        ("Bob", "works_at", ("Bob works at Acme.", "Acme"),
         ("Bob was employed at Initech back then.", "Initech")),            # 'back then'
        ("Carol", "lives_in", ("Carol lives in Plano.", "Plano"),
         ("Carol lived in Boston in 2019.", "Boston")),                     # standalone cue kept
    ]
    for subj, pred, (old_s, old_o), (new_s, new_o) in hist_cases:
        check(f"precondition: '{pred}' is supersedable", _supersedable(pred, new_o))
        mem.write_facts(ns, [{"subject": subj, "predicate": pred, "object": old_o,
                              "statement": old_s}], d1, tid)
        nf, nsup = mem.write_facts(ns, [{"subject": subj, "predicate": pred, "object": new_o,
                                         "statement": new_s}], d2, tid)
        cur = [x for x in rows(ns, subject_entity=subj, predicate=pred) if x["object"] == old_o][0]
        check(f"historical '{new_s}' does NOT supersede '{old_s}'",
              nsup == 0 and cur["valid_to"] is None, f"nsup={nsup}")

    # change-of-state override still wins over the historical gate (issue #10 case 4)
    mem.write_facts(ns, [{"subject": "zeta API", "predicate": "rate_limit", "object": "100 rps",
                          "statement": "The zeta API rate limit is 100 rps."}], d1, tid)
    nf, nsup = mem.write_facts(ns, [{"subject": "zeta API", "predicate": "rate_limit",
                                     "object": "200 rps",
                                     "statement": "The zeta API rate limit was changed to 200 rps."}], d2, tid)
    check("'was changed to' still supersedes (change-of-state override)", nsup == 1)

    print("=== Bug 2: _is_historical classifier (shared by write path + reconcile) ===")
    import core.service as svc
    ih = getattr(svc, "_is_historical", None)
    check("_is_historical() exists", ih is not None)
    if ih is not None:
        for stmt in ("The branch was merged.", "Host2 was root-compromised.",
                     "The changes were pushed to main.", "The deploy pipeline was paused.",
                     "Host2 was compromised yesterday.", "The rate limit was changed to 200 in 2019.",
                     "PR #2024 was merged.", "Build 2026 was deployed."):
            check(f"not historical: '{stmt}'", not ih(stmt))
        for stmt in ("Alice was based in Denver in 2019.", "Host3 was offline two years ago.",
                     "Bob was employed at Initech back then.", "The branch was merged on 2026-06-10.",
                     "She was a swimmer as a child.", "Carol lived in Boston.",
                     "Dan used to work at Acme.", "They were neighbours in the past.",
                     "The office was in Austin until 2021."):
            check(f"historical: '{stmt}'", ih(stmt))

    print("=== Bug 2 (reconcile path): the backfill walk uses the same historical gate ===")
    ns = PREFIX + "e"; reset(ns)
    store.insert_semantic(SCHEMA, ns, "fact", "Host2 status is healthy.", subject="Host2",
                          predicate="status", obj="healthy", valid_from=d1, observed_at=d1,
                          vec=crafted_embed("Host2 status is healthy. (e)"))
    store.insert_semantic(SCHEMA, ns, "fact", "Host2 was root-compromised.", subject="Host2",
                          predicate="status", obj="root-compromised", valid_from=d2, observed_at=d2,
                          vec=crafted_embed("Host2 was root-compromised. (e)"))
    out = reconcile_namespace(store, ns, schema=SCHEMA)
    r = rows(ns, subject_entity="Host2", predicate="status")
    check("reconcile closes the older value for a present-tense 'was' fact",
          out.get("closed") == 1 and r[0]["valid_to"] is not None and r[1]["valid_to"] is None,
          f"out={out}")

    for sfx in ("a", "b", "c0", "c10", "c20", "c40", "d", "e"):
        reset(PREFIX + sfx)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
