"""Regression tests for issue #41 fix C: a recall arm (raw-turn search, semantic search,
the timeline/entity-guarantee arm, the temporal 'now' watermark, or a wide-recall
per-namespace fan-out) that hits a live, reachable-server failure -- a canceled/timed-out
query, a transient DB error -- degrades to a PARTIAL result (degraded=true, with which
namespace/arm failed and why) instead of raising and failing the WHOLE recall. Before
this fix, core/service.py's recall_fetch/recall_wide_fetch/recall_prefetch let any such
error propagate straight to the caller (see the now-updated TODO that used to sit on
core/store.py's search_semantic).

Covers:
  1. A forced arm failure degrades to a partial result (degraded=true, degraded_reasons
     names the namespace + arm + exception class) instead of raising, for:
       - recall_fetch's primary raw-turn arm
       - recall_fetch's primary semantic arm (both non-temporal and temporal queries)
       - recall_fetch's grounded per-namespace fan-out (one linked namespace fails,
         another succeeds)
       - recall_prefetch's max_observed_at ('now' watermark) and timeline (entity-
         guarantee / temporal-guarantee) arms
       - recall_wide_fetch's per-namespace fan-out (one namespace's one arm fails; that
         namespace's OTHER arm and every OTHER namespace still succeed)
  2. The OTHER, non-failing arms/namespaces' results are still returned -- verified both
     at the DB-phase (bundle / raw_c+sem_c) level AND through the full recall()/
     recall_wide() pipeline (rank included), so the degrade isn't just a fetch-phase
     artifact that gets lost downstream.
  3. A genuine client/programming error (psycopg.InterfaceError) is NOT caught here --
     it still raises, same as before this fix. RECALL_ARM_FAILURES is a closed set
     (psycopg.DatabaseError, TimeoutError); InterfaceError is deliberately outside it.
  4. A REAL (non-mocked) Postgres statement_timeout cancellation -- forced via an
     ACCESS EXCLUSIVE lock held on tenant_memnos.semantic from a SECOND connection,
     racing the primary connection's own tight statement_timeout -- degrades exactly
     like the mocked cases. This proves the fix catches the actual failure SHAPE issue
     #41 describes (psycopg.errors.QueryCanceled, a live DatabaseError), not just a
     fake exception class a mock happens to raise.

Engine-level (no LLM, no embedding API): rows are seeded directly with a crafted-vector
stub embedder, the same technique as test_recall_staleness.py. Exception injection for
the deterministic cases follows test_insert_raw_turn_registry_failure.py's monkeypatched-
psycopg.Cursor.execute pattern, extended to match on BIND PARAMETERS (not just query
text) since this codebase's search_raw_turns/search_semantic queries take the namespace
as a %(ns)s bind param, not interpolated SQL text -- so a specific namespace's query can
be targeted without disturbing any other namespace's identical-looking query.

No server needed (direct-DB store path, same pattern as test_recall_staleness.py /
test_insert_raw_turn_registry_failure.py).

Run: MEMNOS_DSN=... python tests/test_recall_arm_degrade.py
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from datetime import datetime, timezone

import psycopg

from core.store import BrainStore
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

NS = "test:armdegrade"
NS_A = "test:armdegrade:a"
NS_B = "test:armdegrade:b"
NS_LOCK = "test:armdegrade:lock"
ALL_NS = [NS, NS_A, NS_B, NS_LOCK]

# query-text fingerprints unique to each store method's SQL (so a failure can be scoped
# to exactly one arm without touching the others' identically-shaped calls). Verified
# against the actual SQL in core/store.py: search_semantic selects inference_confidence/
# inference_basis columns that search_semantic_temporal does NOT, so "inference_basis"
# is unique to search_semantic; search_semantic_temporal's first (always-executed) CTE
# has "restatements, salience, row_number()" with nothing between salience and
# row_number(), which search_semantic never has (it always has the inference_* columns
# in between there).
_SEM_SEARCH_FINGERPRINT = "inference_basis"          # search_semantic only
_SEM_TEMPORAL_FINGERPRINT = "salience, row_number()"  # search_semantic_temporal's main CTE only
_TIMELINE_FINGERPRINT = "valid_from IS NOT NULL"     # timeline() only
_MAX_OBSERVED_FINGERPRINT = ".episodic WHERE"        # max_observed_at() only


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def _patched_execute(orig_execute, predicate, exc_factory):
    """Monkeypatched psycopg.Cursor.execute: raise exc_factory() the FIRST time
    predicate(query_text, params) is True, else run the real query. `params` is
    whatever positional/keyword payload execute() was given -- a dict for this
    codebase's %(name)s-style queries, a list/tuple for its positional-%s queries."""
    def _execute(self, query, *a, **kw):
        q = query if isinstance(query, str) else str(query)
        params = a[0] if a else kw.get("params")
        if predicate(q, params):
            raise exc_factory()
        return orig_execute(self, query, *a, **kw)
    return _execute


def _ns_predicate(fingerprint, target_ns):
    """Match a NAMED-param query (search_raw_turns / search_semantic: %(ns)s) whose SQL
    contains `fingerprint` and whose bound namespace is exactly `target_ns`."""
    def pred(q, params):
        return fingerprint in q and isinstance(params, dict) and params.get("ns") == target_ns
    return pred


def _text_predicate(fingerprint):
    """Match any query (named- or positional-param) whose SQL contains `fingerprint`,
    regardless of bind params -- used for the single-namespace recall_prefetch tests."""
    def pred(q, params):
        return fingerprint in q
    return pred


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

    reset()
    mem = MemnosMemory(store, crafted_embed, dim=dim, llm=None)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def seed_turn(ns, text):
        return store.insert_raw_turn(SCHEMA, ns, None, "user", text, now, crafted_embed(text))

    def seed_fact(ns, statement, subject=None):
        return store.insert_semantic(SCHEMA, ns, "fact", statement, subject=subject,
                                     valid_from=now, vec=crafted_embed(statement),
                                     source_turn_ids=[], observed_at=now)

    # ================================================================================
    # 1. recall_fetch: primary namespace, one arm forced to fail, the OTHER succeeds
    # ================================================================================
    print("=== recall_fetch: primary semantic arm fails, primary raw arm survives ===")
    reset()
    raw_text = "the greenhouse thermostat was replaced in january"
    sem_text = "the greenhouse thermostat now reports temperature every minute"
    seed_turn(NS, raw_text)
    seed_fact(NS, sem_text)

    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _ns_predicate(_SEM_SEARCH_FINGERPRINT, NS),
        lambda: psycopg.errors.QueryCanceled("simulated statement_timeout cancellation"))
    try:
        b = mem.recall_fetch(NS, "greenhouse thermostat")
    finally:
        psycopg.Cursor.execute = orig_execute

    check("bundle is flagged degraded", b.get("_degraded") is True)
    reasons = b.get("_degraded_reasons") or []
    check("exactly one degraded reason recorded", len(reasons) == 1, str(reasons))
    if reasons:
        r = reasons[0]
        check("reason names the right namespace", r.get("namespace") == NS, str(r))
        check("reason names the semantic arm", r.get("arm") == "semantic", str(r))
        check("reason names the real exception class (not swallowed generically)",
              r.get("error") == "QueryCanceled", str(r))
        check("reason carries the real SQLSTATE", r.get("sqlstate") == "57014", str(r))
    check("failed arm degrades to an EMPTY contribution, not a crash", b.get("sem") == [])
    check("the OTHER (raw) arm's real result is still present",
          any(row.get("content") == raw_text for row in b.get("raw", [])))

    print("=== recall_fetch: primary raw arm fails, primary semantic arm survives ===")
    reset()
    seed_turn(NS, raw_text)
    seed_fact(NS, sem_text)
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _ns_predicate(f"{SCHEMA}.raw_turns", NS),
        lambda: psycopg.OperationalError("simulated transient connection blip"))
    try:
        b = mem.recall_fetch(NS, "greenhouse thermostat")
    finally:
        psycopg.Cursor.execute = orig_execute
    check("bundle is flagged degraded", b.get("_degraded") is True)
    reasons = b.get("_degraded_reasons") or []
    check("reason names the raw arm", any(r.get("arm") == "raw" and r.get("namespace") == NS
                                          for r in reasons), str(reasons))
    check("failed arm degrades to an EMPTY contribution, not a crash", b.get("raw") == [])
    check("the OTHER (semantic) arm's real result is still present",
          any(row.get("content") == sem_text for row in b.get("sem", [])))

    print("=== recall_fetch: TEMPORAL query, semantic_temporal arm fails, raw survives ===")
    reset()
    t_raw = "the deployment window opened last tuesday"
    seed_turn(NS, t_raw)
    seed_fact(NS, "the deployment finished on schedule")
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _ns_predicate(_SEM_TEMPORAL_FINGERPRINT, NS),
        lambda: psycopg.errors.QueryCanceled("simulated timeout"))
    try:
        b = mem.recall_fetch(NS, "when did the deployment happen")
    finally:
        psycopg.Cursor.execute = orig_execute
    check("temporal query took the search_semantic_temporal branch", b["intent"].temporal is True)
    check("bundle is flagged degraded", b.get("_degraded") is True)
    check("reason names the semantic_temporal arm",
          any(r.get("arm") == "semantic_temporal" for r in (b.get("_degraded_reasons") or [])),
          str(b.get("_degraded_reasons")))
    check("raw arm (unaffected) still present",
          any(row.get("content") == t_raw for row in b.get("raw", [])))
    check("bundle['sem'] defaulted to a list, not left missing (no KeyError downstream)",
          b.get("sem") == [])

    # ================================================================================
    # 2. recall_fetch: grounded fan-out -- ONE linked namespace fails, another survives
    # ================================================================================
    print("=== recall_fetch: grounded fan-out, NS_A's semantic arm fails, NS_B survives ===")
    reset()
    seed_turn(NS, "primary namespace turn")
    a_sem = "namespace A's own fact about the rollout"
    b_sem = "namespace B's own fact about the rollout"
    seed_fact(NS_A, a_sem)
    seed_fact(NS_B, b_sem)
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _ns_predicate(_SEM_SEARCH_FINGERPRINT, NS_A),
        lambda: psycopg.errors.QueryCanceled("simulated timeout on grounded namespace A"))
    try:
        b = mem.recall_fetch(NS, "rollout", extra_namespaces=[NS_A, NS_B])
    finally:
        psycopg.Cursor.execute = orig_execute
    check("bundle is flagged degraded", b.get("_degraded") is True)
    reasons = b.get("_degraded_reasons") or []
    check("reason identifies NS_A's semantic arm specifically",
          any(r.get("namespace") == NS_A and r.get("arm") == "semantic" for r in reasons),
          str(reasons))
    check("NS_B (unaffected grounded namespace) results still present",
          any(row.get("content") == b_sem for row in b.get("sem", [])))
    check("NS_A's failed arm contributed nothing (no partial/corrupt rows)",
          not any(row.get("content") == a_sem for row in b.get("sem", [])))

    # ================================================================================
    # 3. recall_prefetch: max_observed_at + timeline arms
    # ================================================================================
    print("=== recall_prefetch: max_observed_at fails -> falls back to wall-clock now ===")
    reset()
    seed_fact(NS, "Atlantis opened a new office", subject="Atlantis")
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _text_predicate(_MAX_OBSERVED_FINGERPRINT),
        lambda: psycopg.OperationalError("simulated episodic-table failure"))
    try:
        b = mem.recall_prefetch(NS, "what did Atlantis do")
    finally:
        psycopg.Cursor.execute = orig_execute
    check("prefetch bundle flagged degraded", b.get("_degraded") is True)
    check("reason names max_observed_at",
          any(r.get("arm") == "max_observed_at" for r in (b.get("_degraded_reasons") or [])),
          str(b.get("_degraded_reasons")))
    check("prefetch did not raise -- intent was still computed", "intent" in b)

    print("=== recall_prefetch: entity-guarantee timeline arm fails (non-temporal) ===")
    reset()
    seed_fact(NS, "Atlantis opened a new office", subject="Atlantis")
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _text_predicate(_TIMELINE_FINGERPRINT),
        lambda: psycopg.errors.QueryCanceled("simulated timeout"))
    try:
        b = mem.recall_prefetch(NS, "what did Atlantis do")
    finally:
        psycopg.Cursor.execute = orig_execute
    check("prefetch bundle flagged degraded", b.get("_degraded") is True)
    check("reason names the timeline arm",
          any(r.get("arm") == "timeline" for r in (b.get("_degraded_reasons") or [])),
          str(b.get("_degraded_reasons")))
    check("b['dump'] is simply absent (recall_rank treats it as empty via .get())",
          "dump" not in b)

    # ================================================================================
    # 4. full pipeline: recall() still returns the surviving arm's content, end to end
    # ================================================================================
    print("=== full recall() pipeline survives a degraded semantic arm ===")
    reset()
    seed_turn(NS, raw_text)
    seed_fact(NS, sem_text)
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _ns_predicate(_SEM_SEARCH_FINGERPRINT, NS),
        lambda: psycopg.errors.QueryCanceled("simulated timeout"))
    try:
        rows = mem.recall(NS, "greenhouse thermostat")
    finally:
        psycopg.Cursor.execute = orig_execute
    check("recall() did not raise despite the semantic arm failing", isinstance(rows, list))
    check("the surviving raw-turn content made it all the way through rank/render",
          any(r.get("content") == raw_text for r in rows))

    # ================================================================================
    # 5. recall_wide_fetch: per-namespace fan-out -- one (namespace, arm) fails, the
    #    REST (same namespace's other arm, and every OTHER namespace) still succeed
    # ================================================================================
    print("=== recall_wide_fetch: NS_A's raw arm fails; NS_A's semantic + NS_B/NS both survive ===")
    reset()
    seed_turn(NS, "primary namespace turn about migrations")
    a_raw = "namespace A's raw turn about migrations"
    a_sem2 = "namespace A's semantic fact about migrations"
    b_raw = "namespace B's raw turn about migrations"
    seed_turn(NS_A, a_raw)
    seed_fact(NS_A, a_sem2)
    seed_turn(NS_B, b_raw)
    wide_reasons = []
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _ns_predicate(f"{SCHEMA}.raw_turns", NS_A),
        lambda: psycopg.errors.QueryCanceled("simulated timeout on NS_A raw arm"))
    try:
        raw_c, sem_c = mem.recall_wide_fetch([NS, NS_A, NS_B], "migrations",
                                             degraded_reasons=wide_reasons)
    finally:
        psycopg.Cursor.execute = orig_execute
    check("exactly one wide-fetch failure recorded", len(wide_reasons) == 1, str(wide_reasons))
    if wide_reasons:
        check("failure identifies (NS_A, raw)",
              wide_reasons[0].get("namespace") == NS_A and wide_reasons[0].get("arm") == "raw",
              str(wide_reasons[0]))
    check("NS_A's raw content is ABSENT (that arm failed)",
          not any(r.get("content") == a_raw for r in raw_c))
    check("NS_A's semantic content IS present (sibling arm, same namespace, survived)",
          any(r.get("content") == a_sem2 for r in sem_c))
    check("NS_B's raw content IS present (different namespace, unaffected)",
          any(r.get("content") == b_raw for r in raw_c))
    check("primary NS's raw content IS present (unaffected)",
          any(r.get("content") == "primary namespace turn about migrations" for r in raw_c))

    print("=== recall_wide(): full wide pipeline survives, degraded_reasons plumbed by caller ===")
    reset()
    seed_turn(NS_A, a_raw)
    seed_turn(NS_B, b_raw)
    wide_reasons = []
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _ns_predicate(f"{SCHEMA}.raw_turns", NS_A),
        lambda: psycopg.errors.QueryCanceled("simulated timeout"))
    try:
        raw_c, sem_c = mem.recall_wide_fetch([NS_A, NS_B], "migrations", degraded_reasons=wide_reasons)
        rows = mem.recall_wide_rank("migrations", raw_c, sem_c)
    finally:
        psycopg.Cursor.execute = orig_execute
    check("wide rank pipeline did not raise", isinstance(rows, list))
    check("NS_B's surviving content made it through the full wide pipeline",
          any(r.get("content") == b_raw for r in rows))
    check("degraded_reasons still shows the NS_A failure after ranking", len(wide_reasons) == 1)

    # ================================================================================
    # 6. genuine client/programming error is NOT caught -- still raises
    # ================================================================================
    print("=== a genuine driver/programming error (InterfaceError) still raises ===")
    reset()
    seed_turn(NS, raw_text)
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _ns_predicate(f"{SCHEMA}.raw_turns", NS),
        lambda: psycopg.InterfaceError("simulated cursor-already-closed misuse"))
    raised = False
    try:
        mem.recall_fetch(NS, "greenhouse thermostat")
    except psycopg.InterfaceError:
        raised = True
    finally:
        psycopg.Cursor.execute = orig_execute
    check("recall_fetch does NOT swallow psycopg.InterfaceError -- it propagates", raised)

    print("=== recall_wide_fetch also does NOT swallow InterfaceError ===")
    reset()
    seed_turn(NS_A, a_raw)
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, _ns_predicate(f"{SCHEMA}.raw_turns", NS_A),
        lambda: psycopg.InterfaceError("simulated misuse"))
    raised = False
    try:
        mem.recall_wide_fetch([NS_A], "migrations")
    except psycopg.InterfaceError:
        raised = True
    finally:
        psycopg.Cursor.execute = orig_execute
    check("recall_wide_fetch does NOT swallow psycopg.InterfaceError -- it propagates", raised)

    # ================================================================================
    # 7. GENUINE (non-mocked) statement_timeout cancellation via a real Postgres lock
    # ================================================================================
    print("=== REAL statement_timeout cancellation (ACCESS EXCLUSIVE lock, no mocking) ===")
    reset()
    lock_raw = "the lock namespace raw turn about the outage"
    lock_sem = "the lock namespace semantic fact about the outage"
    seed_turn(NS_LOCK, lock_raw)
    seed_fact(NS_LOCK, lock_sem)

    # A DEDICATED connection with statement_timeout set via the CONNECTION-STRING option
    # (like the production pool: memnos_server.py's `-c statement_timeout=...`), not a
    # bare session-level SET -- fts_clamp's own numnode() safety probe (core/store.py's
    # _tsquery_within_bound) does `SET statement_timeout = DEFAULT` in its own finally
    # block after every call, which would silently WIPE OUT a session-level SET made
    # before search_semantic ever runs, leaving the main hybrid query with NO timeout at
    # all and the lock wait would never cancel. Setting it as the connection's DEFAULT
    # (via `options`) means fts_clamp's own reset restores exactly the bound this test
    # needs, the same way it does in production.
    lock_test_conn = psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row,
                                     options="-c statement_timeout=1000")
    lock_test_store = BrainStore(conn=lock_test_conn)
    lock_test_mem = MemnosMemory(lock_test_store, crafted_embed, dim=dim, llm=None)
    lock_conn = psycopg.connect(DSN, autocommit=False)
    try:
        with lock_conn.cursor() as lc:
            lc.execute(f"LOCK TABLE {SCHEMA}.semantic IN ACCESS EXCLUSIVE MODE")
        # semantic table is now genuinely locked from a second session; the dedicated
        # connection's search_semantic query will block waiting for it and get CANCELED
        # by its own 1s statement_timeout -- a REAL psycopg.errors.QueryCanceled, not a
        # mock. turn_supersession's staleness pass (also joins semantic) hits the SAME
        # lock afterward and is caught by its own PRE-EXISTING bare except (service.py's
        # _mark_stale_turns) -- unrelated to this fix, just adds one more ~1s wait.
        b = lock_test_mem.recall_fetch(NS_LOCK, "the outage")
    finally:
        lock_conn.rollback()   # release the ACCESS EXCLUSIVE lock
        lock_conn.close()
        lock_test_conn.close()

    check("a REAL Postgres cancellation degrades the bundle (not a mock)",
          b.get("_degraded") is True)
    reasons = b.get("_degraded_reasons") or []
    check("real cancellation reason is a genuine QueryCanceled",
          any(r.get("arm") == "semantic" and r.get("error") == "QueryCanceled"
              and r.get("namespace") == NS_LOCK for r in reasons),
          str(reasons))
    check("semantic arm degraded to empty (real cancellation), raw arm's REAL content survives",
          b.get("sem") == [] and any(row.get("content") == lock_raw for row in b.get("raw", [])))

    reset()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
