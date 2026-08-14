"""Regression tests for issue #12 phase 2: hybrid recall's independent SQL arms
(primary raw-turn + semantic search, plus one raw+semantic pair per grounded/wide
namespace — core/service.py's recall_fetch / recall_wide_fetch) now run CONCURRENTLY,
each on its own short-lived pooled connection, via the new `conn_factory` parameter
(core/service.py's MemnosMemory._dispatch_sql_jobs) instead of one after another on a
single held connection.

Covers the three things that matter about this change, not just "it still works":
  (a) CONCURRENCY IS REAL — two arms' queries are observably in flight at the same
      wall-clock time, on distinct threads, when a real conn_factory is supplied; the
      SAME code path with conn_factory=None (every caller before this fix) is provably
      still fully sequential.
  (b) RESULTS ARE UNCHANGED — a recall_fetch/recall_wide_fetch bundle produced through
      the concurrent path (real psycopg_pool.ConnectionPool as conn_factory) is
      byte-identical (order, `_ns` tags, `dup_count`, degraded flags) to the one
      produced by the pre-#12 sequential path, across the four branches the fix
      restructures: narrow non-temporal, temporal (search_semantic_temporal),
      grounded fan-out, and recall_wide_fetch.
  (c) NO POOL EXHAUSTION — bounded concurrency (MEMNOS_RECALL_SQL_CONCURRENCY, default
      2) keeps several simultaneous recalls comfortably inside a modestly-sized real
      pool with zero degraded arms; a DELIBERATELY starved pool (a single connection,
      held elsewhere) degrades just the one arm that couldn't get a connection
      (psycopg_pool.PoolTimeout is a psycopg.OperationalError/DatabaseError subclass,
      so it's already a RECALL_ARM_FAILURES member) instead of hanging or crashing the
      whole recall.

Engine-level (no LLM, no embedding API, no HTTP server): rows are seeded directly with
a crafted-vector stub embedder, same technique as test_recall_arm_degrade.py. Fault/
delay injection reuses that file's monkeypatched-psycopg.Cursor.execute pattern,
matched on query-text fingerprint + bound namespace so it fires deterministically
regardless of which of the concurrent path's several physical connections runs it.

Run: MEMNOS_DSN=... python tests/test_recall_sql_concurrency.py
"""
import math
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from core.store import BrainStore
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

NS = "test:sqlconc"
NS_A = "test:sqlconc:a"
NS_B = "test:sqlconc:b"
NS_WIDE_1 = "test:sqlconc:w1"
NS_WIDE_2 = "test:sqlconc:w2"
NS_WIDE_3 = "test:sqlconc:w3"
ALL_NS = [NS, NS_A, NS_B, NS_WIDE_1, NS_WIDE_2, NS_WIDE_3]

# same fingerprints test_recall_arm_degrade.py verified against the real SQL: unique to
# search_raw_turns / search_semantic respectively, so a delay/failure can target exactly
# one arm without touching the other's identically-shaped call.
_RAW_FINGERPRINT = f"{SCHEMA}.raw_turns"
_SEM_FINGERPRINT = "inference_basis"          # search_semantic only
_SEM_TEMPORAL_FINGERPRINT = "salience, row_number()"  # search_semantic_temporal only
# core/store.py's detect_vector_type -- the ONLY query that touches pg_attribute for
# an 'embedding' column; unique in the codebase (verified via grep).
_VTYPE_PROBE_FINGERPRINT = "a.attname = 'embedding'"


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def main():
    store = BrainStore(DSN)
    store.create_schema("memnos")
    conn = store.conn
    orig_execute = psycopg.Cursor.execute

    dim = 384
    _auto = {}

    def crafted_embed(text):
        theta = _auto.setdefault(text, 0.35 * len(_auto))
        v = [0.0] * dim
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v

    def reset():
        with conn.cursor() as c:
            for n in ALL_NS:
                c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (n,))
                c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (n,))
                c.execute(f"DELETE FROM {SCHEMA}.episodic WHERE namespace=%s", (n,))

    mem = MemnosMemory(store, crafted_embed, dim=dim, llm=None)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def seed_turn(ns, text, when=now):
        return store.insert_raw_turn(SCHEMA, ns, None, "user", text, when, crafted_embed(text))

    def seed_fact(ns, statement, subject=None, when=now):
        return store.insert_semantic(SCHEMA, ns, "fact", statement, subject=subject,
                                     valid_from=when, vec=crafted_embed(statement),
                                     source_turn_ids=[], observed_at=when)

    # a real, modestly-sized production-shaped pool -- min_size/max_size/kwargs mirror
    # memnos_server.py's own ConnectionPool(...) construction.
    pool = ConnectionPool(DSN, min_size=2, max_size=8, open=True, timeout=5,
                          kwargs={"autocommit": True, "row_factory": dict_row})
    pool.wait(timeout=10)

    # ================================================================================
    # (a) CONCURRENCY IS REAL
    # ================================================================================
    print("=== (a) primary raw + primary semantic arms run CONCURRENTLY (real pool) ===")
    reset()
    seed_turn(NS, "the observatory telescope was recalibrated in march")
    seed_fact(NS, "the observatory telescope now tracks satellites automatically")

    DELAY = 0.3
    events = []
    events_lock = threading.Lock()

    _delay_namespaces = {NS}   # mutated per section below to widen/narrow which ns's arms delay

    def _delay_predicate(q, params):
        if not (isinstance(params, dict) and params.get("ns") in _delay_namespaces):
            return None
        if _RAW_FINGERPRINT in q:
            return "raw"
        if _SEM_FINGERPRINT in q:
            return "semantic"
        return None

    def _delayed_execute(self_cur, query, *a, **kw):
        q = query if isinstance(query, str) else str(query)
        params = a[0] if a else kw.get("params")
        arm = _delay_predicate(q, params)
        if arm is not None:
            t0 = time.monotonic()
            time.sleep(DELAY)
            result = orig_execute(self_cur, query, *a, **kw)
            t1 = time.monotonic()
            with events_lock:
                events.append((arm, threading.get_ident(), t0, t1))
            return result
        return orig_execute(self_cur, query, *a, **kw)

    query = "observatory telescope"
    qv = crafted_embed(query)

    events.clear()
    psycopg.Cursor.execute = _delayed_execute
    try:
        t_start = time.monotonic()
        b_conc = mem.recall_fetch(NS, query, qv=qv, conn_factory=pool.connection)
        t_conc = time.monotonic() - t_start
    finally:
        psycopg.Cursor.execute = orig_execute

    check("both arms' delayed queries were observed", len(events) == 2, str(events))
    if len(events) == 2:
        (arm1, tid1, s1, e1), (arm2, tid2, s2, e2) = events
        overlap = max(s1, s2) < min(e1, e2)
        check("the two arms ran on DISTINCT threads", tid1 != tid2, f"{tid1} vs {tid2}")
        check("the two arms' execution windows OVERLAP in wall-clock time", overlap,
              f"[{s1:.3f},{e1:.3f}] vs [{s2:.3f},{e2:.3f}]")
    check(f"concurrent wall-clock ({t_conc:.3f}s) well under 2x the per-arm delay "
          f"({2*DELAY:.1f}s) -- proves overlap, not just 'both ran'",
          t_conc < 1.5 * DELAY, f"{t_conc:.3f}s")
    check("concurrent path still returns the real raw content",
          any(r.get("content", "").startswith("the observatory telescope was")
              for r in b_conc.get("raw", [])))
    check("concurrent path still returns the real semantic content",
          any(r.get("content", "").startswith("the observatory telescope now")
              for r in b_conc.get("sem", [])))

    print("=== (a) SAME delayed arms, conn_factory=None -- baseline stays SEQUENTIAL ===")
    events.clear()
    psycopg.Cursor.execute = _delayed_execute
    try:
        t_start = time.monotonic()
        b_seq = mem.recall_fetch(NS, query, qv=qv, conn_factory=None)
        t_seq = time.monotonic() - t_start
    finally:
        psycopg.Cursor.execute = orig_execute
    check("both arms' delayed queries were observed (sequential)", len(events) == 2, str(events))
    check(f"sequential wall-clock ({t_seq:.3f}s) is AT LEAST ~2x the per-arm delay "
          f"({2*DELAY:.1f}s) -- confirms the default path genuinely serializes",
          t_seq >= 1.8 * DELAY, f"{t_seq:.3f}s")
    if len(events) == 2:
        (arm1, tid1, s1, e1), (arm2, tid2, s2, e2) = events
        check("sequential arms do NOT overlap", e1 <= s2 or e2 <= s1,
              f"[{s1:.3f},{e1:.3f}] vs [{s2:.3f},{e2:.3f}]")

    print("=== (a) grounded fan-out (6 arms: primary NS + NS_A + NS_B, raw+sem each): "
          "concurrency is BOUNDED to the cap ===")
    reset()
    seed_turn(NS, "the pipeline turn")
    seed_turn(NS_A, "namespace A raw content about the rollout")
    seed_fact(NS_B, "namespace B fact about the rollout")
    q2 = "rollout"
    qv2 = crafted_embed(q2)
    N_JOBS = 6         # primary raw+sem + (NS_A, NS_B) x (raw, sem)
    CAP = 2
    os.environ["MEMNOS_RECALL_SQL_CONCURRENCY"] = str(CAP)
    _delay_namespaces.clear(); _delay_namespaces.update({NS, NS_A, NS_B})
    events.clear()
    psycopg.Cursor.execute = _delayed_execute
    try:
        t_start = time.monotonic()
        mem.recall_fetch(NS, q2, qv=qv2, extra_namespaces=[NS_A, NS_B],
                         conn_factory=pool.connection)
        t_fanout = time.monotonic() - t_start
    finally:
        psycopg.Cursor.execute = orig_execute
        os.environ.pop("MEMNOS_RECALL_SQL_CONCURRENCY", None)
        _delay_namespaces.clear(); _delay_namespaces.add(NS)
    distinct_threads = {e[1] for e in events}
    n_waves = math.ceil(N_JOBS / CAP)   # 3 waves of <=2 concurrent jobs each
    check(f"{N_JOBS}-arm grounded fan-out: all {N_JOBS} delayed queries observed",
          len(events) == N_JOBS, str(events))
    check(f"concurrency bounded to the cap (<={CAP} distinct worker threads used for {N_JOBS} jobs)",
          len(distinct_threads) <= CAP, f"{len(distinct_threads)} threads: {distinct_threads}")
    check(f"{N_JOBS}-arm wall-clock ({t_fanout:.3f}s) beats full-sequential "
          f"({N_JOBS*DELAY:.1f}s) but reflects ~{n_waves} waves of {CAP}, not full unbounded "
          f"parallelism (~{DELAY:.1f}s)",
          (n_waves - 0.5) * DELAY <= t_fanout < N_JOBS * DELAY, f"{t_fanout:.3f}s")

    # ================================================================================
    # (b) RESULTS ARE UNCHANGED across conn_factory=None vs a real pool
    # ================================================================================
    def _strip(rows):
        # drop nothing -- compare full row dicts (order-preserving) so `_ns`/`dup_count`/
        # every scored field is covered, not just content.
        return rows

    print("=== (b) parity: narrow NON-temporal recall_fetch ===")
    reset()
    seed_turn(NS, "the greenhouse thermostat was replaced in january")
    seed_turn(NS, "the greenhouse thermostat was replaced in january")   # exact dup -> dup_count
    seed_fact(NS, "the greenhouse thermostat now reports temperature every minute")
    q3 = "greenhouse thermostat"
    qv3 = crafted_embed(q3)
    b_none = mem.recall_fetch(NS, q3, qv=qv3, conn_factory=None)
    b_pool = mem.recall_fetch(NS, q3, qv=qv3, conn_factory=pool.connection)
    check("raw bundle identical (incl. dup_count from the exact-duplicate collapse)",
          _strip(b_none["raw"]) == _strip(b_pool["raw"]), f"{b_none['raw']} vs {b_pool['raw']}")
    check("sem bundle identical", _strip(b_none["sem"]) == _strip(b_pool["sem"]))
    check("any raw row shows dup_count>=2 (dedup actually exercised)",
          any(r.get("dup_count", 1) >= 2 for r in b_none["raw"]), str(b_none["raw"]))
    check("degraded flags identical", b_none.get("_degraded") == b_pool.get("_degraded"))

    print("=== (b) parity: TEMPORAL recall_fetch (search_semantic_temporal branch) ===")
    reset()
    seed_turn(NS, "the deployment window opened last tuesday")
    seed_fact(NS, "the deployment finished on schedule")
    q4 = "when did the deployment happen"
    qv4 = crafted_embed(q4)
    b_none_t = mem.recall_fetch(NS, q4, qv=qv4, conn_factory=None)
    b_pool_t = mem.recall_fetch(NS, q4, qv=qv4, conn_factory=pool.connection)
    check("temporal query took the search_semantic_temporal branch (both paths)",
          b_none_t["intent"].temporal is True and b_pool_t["intent"].temporal is True)
    check("raw bundle identical (temporal)", _strip(b_none_t["raw"]) == _strip(b_pool_t["raw"]))
    check("sem bundle identical (temporal)", _strip(b_none_t["sem"]) == _strip(b_pool_t["sem"]))

    print("=== (b) parity: GROUNDED fan-out recall_fetch (extra_namespaces) ===")
    reset()
    seed_turn(NS, "primary namespace turn about the rollout")
    seed_turn(NS_A, "namespace A raw content about the rollout")
    seed_fact(NS_A, "namespace A fact about the rollout")
    seed_fact(NS_B, "namespace B fact about the rollout")
    q5 = "rollout"
    qv5 = crafted_embed(q5)
    b_none_g = mem.recall_fetch(NS, q5, qv=qv5, extra_namespaces=[NS_A, NS_B], conn_factory=None)
    b_pool_g = mem.recall_fetch(NS, q5, qv=qv5, extra_namespaces=[NS_A, NS_B], conn_factory=pool.connection)
    check("grounded raw bundle identical, incl. _ns tags and ORDER",
          _strip(b_none_g["raw"]) == _strip(b_pool_g["raw"]),
          f"{b_none_g['raw']} vs {b_pool_g['raw']}")
    check("grounded sem bundle identical, incl. _ns tags and ORDER",
          _strip(b_none_g["sem"]) == _strip(b_pool_g["sem"]),
          f"{b_none_g['sem']} vs {b_pool_g['sem']}")
    check("NS_A raw row is tagged _ns=NS_A in both",
          any(r.get("_ns") == NS_A for r in b_none_g["raw"]) and
          any(r.get("_ns") == NS_A for r in b_pool_g["raw"]))

    print("=== (b) parity: recall_wide_fetch across 3 namespaces ===")
    reset()
    seed_turn(NS_WIDE_1, "wide namespace one raw content about the launch")
    seed_fact(NS_WIDE_2, "wide namespace two fact about the launch")
    seed_turn(NS_WIDE_3, "wide namespace three raw content about the launch")
    seed_fact(NS_WIDE_3, "wide namespace three fact about the launch")
    q6 = "launch"
    qv6 = crafted_embed(q6)
    wide_ns = [NS_WIDE_1, NS_WIDE_2, NS_WIDE_3]
    raw_none, sem_none = mem.recall_wide_fetch(wide_ns, q6, qv=qv6, conn_factory=None)
    raw_pool, sem_pool = mem.recall_wide_fetch(wide_ns, q6, qv=qv6, conn_factory=pool.connection)
    check("wide raw_c identical, incl. order + _ns tags", _strip(raw_none) == _strip(raw_pool),
          f"{raw_none} vs {raw_pool}")
    check("wide sem_c identical, incl. order + _ns tags", _strip(sem_none) == _strip(sem_pool),
          f"{sem_none} vs {sem_pool}")
    check("all 3 namespaces contributed on both paths",
          {r.get("_ns") for r in raw_none + sem_none} >= {NS_WIDE_1, NS_WIDE_2, NS_WIDE_3})

    # ================================================================================
    # (c) NO CONNECTION-POOL EXHAUSTION
    # ================================================================================
    print("=== (c) several simultaneous recalls fit comfortably in a real pool (no degrade) ===")
    reset()
    for i, ns in enumerate([NS, NS_A, NS_B]):
        seed_turn(ns, f"concurrent-load raw content {i} about the migration")
        seed_fact(ns, f"concurrent-load fact {i} about the migration")
    results = [None, None, None]
    errors = [None, None, None]

    def _one_recall(i, ns):
        try:
            with pool.connection() as held_conn:
                local_store = BrainStore(conn=held_conn)
                local_mem = MemnosMemory(local_store, crafted_embed, dim=dim, llm=None)
                q = "migration"
                results[i] = local_mem.recall_fetch(ns, q, qv=crafted_embed(q),
                                                    conn_factory=pool.connection)
        except Exception as e:
            errors[i] = e

    threads = [threading.Thread(target=_one_recall, args=(i, ns))
               for i, ns in enumerate([NS, NS_A, NS_B])]
    for t in threads: t.start()
    for t in threads: t.join(timeout=15)
    check("all 3 concurrent recalls completed without raising",
          all(e is None for e in errors), str(errors))
    check("none of the 3 concurrent recalls degraded (pool sized adequately)",
          all(r is not None and not r.get("_degraded") for r in results),
          str([r.get("_degraded_reasons") for r in results if r]))
    check("every concurrent recall got its real content back",
          all(r and any("concurrent-load raw content" in row.get("content", "")
                        for row in r.get("raw", [])) for r in results))

    print("=== (c) a STARVED pool degrades just the one arm that couldn't get a connection ===")
    reset()
    seed_turn(NS, "starved pool raw content survives")
    seed_fact(NS, "starved pool semantic content is the one that should degrade")
    starved_pool = ConnectionPool(DSN, min_size=1, max_size=1, open=True, timeout=0.3,
                                  kwargs={"autocommit": True, "row_factory": dict_row})
    starved_pool.wait(timeout=10)
    # hold the pool's ONLY connection so any conn_factory() draw times out.
    hold_cm = starved_pool.connection()
    hold_cm.__enter__()
    try:
        q7 = "starved pool"
        b_starved = mem.recall_fetch(NS, q7, qv=crafted_embed(q7),
                                     conn_factory=starved_pool.connection)
    finally:
        hold_cm.__exit__(None, None, None)
        starved_pool.close()
    check("recall_fetch did NOT raise despite the starved pool", b_starved is not None)
    check("bundle is flagged degraded (one arm couldn't get a connection)",
          b_starved.get("_degraded") is True)
    reasons = b_starved.get("_degraded_reasons") or []
    check("exactly one degraded arm recorded (job[0] reused the held conn and survived)",
          len(reasons) == 1, str(reasons))
    if reasons:
        check("the degraded reason is a genuine PoolTimeout-class failure",
              reasons[0].get("error") in ("PoolTimeout", "OperationalError"), str(reasons[0]))
    check("the OTHER arm (job[0], reused connection) still returned real content",
          any(row.get("content") == "starved pool raw content survives"
              for row in b_starved.get("raw", [])) or
          any(row.get("content") == "starved pool semantic content is the one that should degrade"
              for row in b_starved.get("sem", [])),
          str(b_starved))

    # ================================================================================
    # A QUERY failure (not a connection-acquisition failure) surviving the thread
    # boundary intact -- the capture-in-worker-thread -> Future -> resolver -> raise
    # machinery in _dispatch_sql_jobs is new; this proves it preserves the same
    # namespace/arm/error-class/sqlstate shape test_recall_arm_degrade.py already
    # verifies for conn_factory=None, over a REAL healthy pool this time.
    # ================================================================================
    print("=== semantic arm's QUERY fails via conn_factory (new connection) -- degrades "
          "just that arm, raw arm (reused connection) survives ===")
    reset()
    raw_text = "the greenhouse thermostat was replaced in january"
    sem_text = "the greenhouse thermostat now reports temperature every minute"
    seed_turn(NS, raw_text)
    seed_fact(NS, sem_text)

    def _ns_predicate(fingerprint, target_ns):
        def pred(q, params):
            return fingerprint in q and isinstance(params, dict) and params.get("ns") == target_ns
        return pred

    def _patched_execute(predicate, exc_factory):
        def _execute(self_cur, query, *a, **kw):
            q = query if isinstance(query, str) else str(query)
            params = a[0] if a else kw.get("params")
            if predicate(q, params):
                raise exc_factory()
            return orig_execute(self_cur, query, *a, **kw)
        return _execute

    psycopg.Cursor.execute = _patched_execute(
        _ns_predicate(_SEM_FINGERPRINT, NS),
        lambda: psycopg.errors.QueryCanceled("simulated statement_timeout cancellation"))
    try:
        b_qfail = mem.recall_fetch(NS, "greenhouse thermostat", qv=crafted_embed("greenhouse thermostat"),
                                   conn_factory=pool.connection)
    finally:
        psycopg.Cursor.execute = orig_execute
    check("bundle is flagged degraded", b_qfail.get("_degraded") is True)
    reasons = b_qfail.get("_degraded_reasons") or []
    check("exactly one degraded reason recorded", len(reasons) == 1, str(reasons))
    if reasons:
        r = reasons[0]
        check("reason names the right namespace", r.get("namespace") == NS, str(r))
        check("reason names the semantic arm", r.get("arm") == "semantic", str(r))
        check("reason preserves the real exception class across the thread boundary",
              r.get("error") == "QueryCanceled", str(r))
        check("reason preserves the real SQLSTATE across the thread boundary",
              r.get("sqlstate") == "57014", str(r))
    check("failed arm degrades to an EMPTY contribution, not a crash", b_qfail.get("sem") == [])
    check("the OTHER arm (raw, reused connection) still returned real content",
          any(row.get("content") == raw_text for row in b_qfail.get("raw", [])))

    print("=== raw arm's QUERY fails via the REUSED connection -- semantic (new "
          "connection) survives ===")
    reset()
    seed_turn(NS, raw_text)
    seed_fact(NS, sem_text)
    psycopg.Cursor.execute = _patched_execute(
        _ns_predicate(_RAW_FINGERPRINT, NS),
        lambda: psycopg.OperationalError("simulated transient connection blip"))
    try:
        b_qfail2 = mem.recall_fetch(NS, "greenhouse thermostat", qv=crafted_embed("greenhouse thermostat"),
                                    conn_factory=pool.connection)
    finally:
        psycopg.Cursor.execute = orig_execute
    check("bundle is flagged degraded (raw arm)", b_qfail2.get("_degraded") is True)
    reasons2 = b_qfail2.get("_degraded_reasons") or []
    check("reason names the raw arm", any(r.get("arm") == "raw" and r.get("namespace") == NS
                                          for r in reasons2), str(reasons2))
    check("failed arm degrades to an EMPTY contribution, not a crash", b_qfail2.get("raw") == [])
    check("the OTHER arm (semantic, new pooled connection) still returned real content",
          any(row.get("content") == sem_text for row in b_qfail2.get("sem", [])))

    # ================================================================================
    # Cross-vendor review finding: _dispatch_sql_jobs' EAGER vtype probe
    # (self.store.vtype, run on the main thread before any job is even submitted)
    # was unguarded -- a live DB catalog failure there (core/store.py's
    # detect_vector_type, e.g. a dropped/terminated phase-B connection) propagated
    # straight out of recall_fetch and 500'd the whole request instead of degrading,
    # a regression of issue #41's degrade-not-crash guarantee. Simulates exactly
    # that: self.store's connection persistently fails ONLY the vtype probe (a
    # fresh pooled connection, e.g. job[1]'s, is untouched and healthy).
    # ================================================================================
    print("=== eager vtype probe fails on self.store's connection -- degrades to the "
          "raw arm instead of 500ing the whole recall ===")
    reset()
    seed_turn(NS, "vtype probe raw content survives via a healthy pooled connection")
    seed_fact(NS, "vtype probe semantic content survives via a healthy pooled connection")
    mem.store._vtype = None   # force re-detection (create_schema() cached it earlier)
    broken_conn = mem.store.conn

    def _vtype_probe_fails_on_broken_conn(self_cur, query, *a, **kw):
        q = query if isinstance(query, str) else str(query)
        if _VTYPE_PROBE_FINGERPRINT in q and self_cur.connection is broken_conn:
            raise psycopg.OperationalError("simulated dropped connection during vtype probe")
        return orig_execute(self_cur, query, *a, **kw)

    psycopg.Cursor.execute = _vtype_probe_fails_on_broken_conn
    try:
        q8 = "vtype probe"
        b_vtype = mem.recall_fetch(NS, q8, qv=crafted_embed(q8), conn_factory=pool.connection)
    except Exception as e:
        b_vtype = None
        vtype_probe_exc = e
    else:
        vtype_probe_exc = None
    finally:
        psycopg.Cursor.execute = orig_execute
        mem.store._vtype = None   # leave store clean for anything after this section

    check("recall_fetch did NOT raise/500 despite the broken vtype probe",
          b_vtype is not None, repr(vtype_probe_exc))
    if b_vtype is not None:
        check("bundle is flagged degraded", b_vtype.get("_degraded") is True)
        reasons3 = b_vtype.get("_degraded_reasons") or []
        check("the raw arm (self.store's broken connection) is recorded as degraded",
              any(r.get("arm") == "raw" and r.get("namespace") == NS for r in reasons3),
              str(reasons3))
        check("raw arm degraded to an EMPTY contribution, not a crash",
              b_vtype.get("raw") == [])
        check("the semantic arm (fresh, healthy pooled connection) still returned real content",
              any(row.get("content") ==
                  "vtype probe semantic content survives via a healthy pooled connection"
                  for row in b_vtype.get("sem", [])),
              str(b_vtype.get("sem")))

    pool.close()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
