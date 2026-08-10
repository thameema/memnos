"""Regression tests for issue #49: _tsquery_within_bound's probe-failure catch used to
treat EVERY DatabaseError-class failure of the numnode() safety probe as "this query is
pathological, clamp it" -- including probe-infrastructure failures (AdminShutdown, a
dropped connection, QueryCanceled from unrelated admin/lock contention) that have nothing
to do with the query's own shape. Under SUSTAINED failure of that kind, an entirely
ordinary query got progressively shrunk all the way down to a single character, chasing a
"pathological" signal that was never about the query's complexity -- a real mechanism,
found as a deferred (round-4, non-blocking) finding during PR #43's review of issue #41.

Fix (core/store.py's _tsquery_within_bound): a FIXED, narrow set of exceptions --
ProgramLimitExceeded (the parser's own "value is too big in tsquery" limit),
InternalError_ (XX000 "tsquery stack too small"), DataError (psycopg's own pre-flight
rejection of the query text, e.g. an embedded NUL byte) -- are still treated as
query-shape evidence with no extra work: these are provably about the query text, not the
server, so behavior for them is UNCHANGED from before this fix. Every other
DatabaseError-class probe failure now gets a CONTROL PROBE (one trivial numnode() call,
under the same tight timeout, on the same cursor) before a verdict: control succeeds ->
the server just answered a text-search query fine, so the original failure really was
this query's fault -- clamp it, exactly as before. Control ALSO fails -> the probe itself
is broken, not the query -- the ORIGINAL exception is re-raised (not silently filed as
either "safe" or "pathological"), so it reaches core/service.py's existing
RECALL_ARM_FAILURES degrade-not-raise path (issue #41 fix C) and degrades just that one
recall arm.

Covers:
  1. The three genuinely-pathological exception types still clamp WITHOUT ever issuing a
     control probe -- precision preserved, zero behavior change for the failure shapes
     issue #41's rounds 2-3 already proved are real (test_fts_clamp_shape.py covers those
     against a REAL Postgres; this file re-confirms the exact catch boundary with
     deterministic injection).
  2. A ONE-SHOT ambiguous failure (e.g. a single stray AdminShutdown) whose control probe
     succeeds still clamps -- exactly the pre-#49 behavior for an isolated blip. This is
     the contrast case proving the fix doesn't turn ordinary transient noise into hard
     failures.
  3. A SUSTAINED ambiguous failure (main probe AND control probe both fail, simulating a
     connection that's genuinely going down) is no longer swallowed as "pathological" --
     fts_clamp raises the ORIGINAL exception on the FIRST probe attempt: no shrink loop,
     no 1-character return. This is the exact ambiguity issue #49 reports, reproduced and
     closed.
  4. The exception the caller actually sees is the ORIGINAL one (AdminShutdown, 57P01),
     not something from the statement_timeout restore -- proves the restore's own
     `except psycopg.DatabaseError: pass` guard can't clobber the real signal when the
     connection is genuinely dead (the restore call fails too, on the same dead
     connection).
  5. End-to-end through core/service.py: a sustained infra failure in ONE recall arm's fts
     probe degrades JUST that arm (degraded=true, reason names the exception class +
     SQLSTATE) while the OTHER arm's real, seeded content still comes back -- same shape
     as issue #41 fix C's existing arm-degrade tests, now covering the probe-
     infrastructure failure mode specifically instead of a failure in the arm's main
     query.

No server needed (direct-DB store path, same pattern as test_recall_arm_degrade.py /
test_fts_clamp_shape.py). Exception injection follows test_recall_arm_degrade.py's
monkeypatched-psycopg.Cursor.execute pattern.

Run: MEMNOS_DSN=... python tests/test_fts_clamp_probe_failure.py
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from core.store import BrainStore, _tsquery_within_bound, fts_clamp, _fts_max_tokens, _fts_node_bound
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

NS = "test:ftsprobefailure"


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


# The two probe queries _tsquery_within_bound issues have structurally distinct SQL text
# (one is parameterized with %s, the other is a hardcoded 'x' literal, per the fix) -- no
# bind-param inspection needed to tell them apart.
_MAIN_PROBE_MARKER = "numnode(websearch_to_tsquery('english', %s))"
_CONTROL_PROBE_MARKER = "numnode(websearch_to_tsquery('english', 'x'))"


def _classify(q):
    if _CONTROL_PROBE_MARKER in q:
        return "control"
    if _MAIN_PROBE_MARKER in q:
        return "main"
    return None


def _tracking_patch(orig_execute, rule, log):
    """Monkeypatched psycopg.Cursor.execute. Every main/control probe call is classified
    and appended to `log` as (kind, params). `rule(kind, params)` returns an exception
    INSTANCE to raise instead of running the real query, or None to run it for real."""
    def _execute(self, query, *a, **kw):
        q = query if isinstance(query, str) else str(query)
        kind = _classify(q)
        if kind:
            params = a[0] if a else kw.get("params")
            log.append((kind, params))
            exc = rule(kind, params)
            if exc is not None:
                raise exc
        return orig_execute(self, query, *a, **kw)
    return _execute


def _execute_dead_connection(orig_execute, target_qtext, main_exc, after_exc):
    """Simulates a connection that goes fully dead the moment the main probe fails: the
    main probe for `target_qtext` raises `main_exc`; EVERY execute() call on this cursor
    after that (the control probe, AND the finally block's `SET statement_timeout =
    DEFAULT` restore) raises `after_exc` instead -- a DIFFERENT exception type/message, so
    the test can prove the caller sees `main_exc` specifically, not whatever the restore
    path produced."""
    state = {"armed": False}
    def _execute(self, query, *a, **kw):
        q = query if isinstance(query, str) else str(query)
        params = a[0] if a else kw.get("params")
        if not state["armed"] and _classify(q) == "main" and params and params[0] == target_qtext:
            state["armed"] = True
            raise main_exc
        if state["armed"]:
            raise after_exc
        return orig_execute(self, query, *a, **kw)
    return _execute


def main():
    orig_execute = psycopg.Cursor.execute
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    cap = _fts_max_tokens()
    bound = _fts_node_bound(cap)

    print("=== 1. genuinely-pathological signals: fast path, no control probe ===")
    PATHOLOGICAL = [
        ("ProgramLimitExceeded (tsquery too big)", psycopg.errors.ProgramLimitExceeded),
        ("InternalError_ (tsquery stack too small, XX000)", psycopg.errors.InternalError_),
        ("DataError (pre-flight rejection, e.g. NUL byte)", psycopg.DataError),
    ]
    for label, exc_type in PATHOLOGICAL:
        target = f"pathological-marker-{exc_type.__name__}"
        log = []
        def rule(kind, params, _target=target, _exc=exc_type):
            if kind == "main" and params and params[0] == _target:
                return _exc(f"simulated {_exc.__name__}")
            return None
        psycopg.Cursor.execute = _tracking_patch(orig_execute, rule, log)
        try:
            result = _tsquery_within_bound(conn, target, bound)
        finally:
            psycopg.Cursor.execute = orig_execute
        check(f"[{label}] treated as pathological (returns False)", result is False)
        check(f"[{label}] NO control probe issued (fast path)", log == [("main", (target, bound))],
              str(log))

    print("=== 2. ONE-SHOT ambiguous failure (control succeeds): still clamps, no raise ===")
    target = "ordinary-query-one-shot-admin-shutdown-blip"
    log = []
    def rule_one_shot(kind, params):
        if kind == "main" and params and params[0] == target:
            return psycopg.errors.AdminShutdown("simulated one-shot AdminShutdown")
        return None   # the control probe (and anything else) runs for real -- server's fine
    psycopg.Cursor.execute = _tracking_patch(orig_execute, rule_one_shot, log)
    try:
        result = _tsquery_within_bound(conn, target, bound)
        raised = None
    except Exception as e:
        result, raised = None, e
    finally:
        psycopg.Cursor.execute = orig_execute
    check("one-shot ambiguous failure does NOT raise", raised is None,
          f"raised {type(raised).__name__}" if raised else "")
    check("one-shot ambiguous failure still clamps (returns False, control probe confirmed server healthy)",
          result is False)
    check("exactly one main + one control probe fired",
          log == [("main", (target, bound)), ("control", None)], str(log))

    print("=== 3. SUSTAINED ambiguous failure (control ALSO fails): raised, not clamped ===")
    # an ORDINARY, safe multi-word query -- the only reason this would ever shrink is the
    # bug issue #49 reports, not any real complexity in the text itself.
    ordinary_query = "the greenhouse thermostat firmware needs a recalibration pass"
    log = []
    def rule_sustained(kind, params):
        return psycopg.errors.AdminShutdown("simulated sustained AdminShutdown")
    psycopg.Cursor.execute = _tracking_patch(orig_execute, rule_sustained, log)
    try:
        out = fts_clamp(ordinary_query, conn)
        clamp_raised = None
    except Exception as e:
        out, clamp_raised = None, e
    finally:
        psycopg.Cursor.execute = orig_execute
    check("fts_clamp RAISES under sustained probe-infrastructure failure (not silently clamped)",
          clamp_raised is not None)
    check("the raised exception is the real AdminShutdown, not something else",
          isinstance(clamp_raised, psycopg.errors.AdminShutdown), str(clamp_raised))
    if clamp_raised is not None:
        check("raised exception carries the real SQLSTATE (57P01)",
              getattr(clamp_raised, "sqlstate", None) == "57P01")
    check("fts_clamp made exactly ONE probe attempt (main + its control) -- NO shrink loop, "
          "confirming the ordinary query was never mangled chasing a false pathology signal",
          log == [("main", (ordinary_query, bound)), ("control", None)], str(log))

    print("=== 4. restore-guard: the ORIGINAL exception survives even if the SET DEFAULT "
          "restore (and the control probe) also fail on the same dead connection ===")
    target = "dead-connection-restore-guard-marker"
    main_exc = psycopg.errors.AdminShutdown("THE REAL main-probe AdminShutdown")
    after_exc = psycopg.OperationalError("a DIFFERENT error from the dead connection's "
                                          "control probe / statement_timeout restore")
    psycopg.Cursor.execute = _execute_dead_connection(orig_execute, target, main_exc, after_exc)
    try:
        _tsquery_within_bound(conn, target, bound)
        guard_raised = None
    except Exception as e:
        guard_raised = e
    finally:
        psycopg.Cursor.execute = orig_execute
    check("something raised", guard_raised is not None)
    if guard_raised is not None:
        check("the CALLER sees the ORIGINAL main-probe AdminShutdown, not the restore's "
              "OperationalError (the finally block's restore is correctly guarded)",
              guard_raised is main_exc, f"got {type(guard_raised).__name__}: {guard_raised}")

    print("=== 5. end-to-end through core/service.py: sustained infra failure degrades "
          "JUST the semantic arm; the raw arm's real content still comes back ===")
    store = BrainStore(DSN)
    store.create_schema("memnos")
    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.episodic WHERE namespace=%s", (NS,))

    dim = 384
    _auto = {}
    def crafted_embed(text):
        theta = _auto.setdefault(text, 0.35 * len(_auto))
        v = [0.0] * dim
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v

    mem = MemnosMemory(store, crafted_embed, dim=dim, llm=None)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    query_text = "greenhouse thermostat firmware release notes"
    raw_text = "the greenhouse thermostat firmware was rolled back last night"
    sem_text = "the greenhouse thermostat firmware release notes mention a sensor fix"
    store.insert_raw_turn(SCHEMA, NS, None, "user", raw_text, now, crafted_embed(raw_text))
    store.insert_semantic(SCHEMA, NS, "fact", sem_text, subject=None, valid_from=now,
                          vec=crafted_embed(sem_text), source_turn_ids=[], observed_at=now)

    # recall_fetch probes the raw arm's fts_clamp FIRST, then the semantic arm's -- both
    # with the IDENTICAL query text (the recall query itself), so the 2nd occurrence of a
    # main-probe call for this exact text is unambiguously the semantic arm's.
    state = {"main_seen": 0, "armed": False}
    def rule_e2e(kind, params):
        if kind == "main" and params and params[0] == query_text:
            state["main_seen"] += 1
            if state["main_seen"] == 2:
                state["armed"] = True
                return psycopg.errors.AdminShutdown("simulated AdminShutdown (arm-degrade e2e)")
            return None
        if kind == "control" and state["armed"]:
            state["armed"] = False
            return psycopg.errors.AdminShutdown("simulated AdminShutdown (control, arm-degrade e2e)")
        return None
    psycopg.Cursor.execute = _tracking_patch(orig_execute, rule_e2e, [])
    try:
        b = mem.recall_fetch(NS, query_text)
    finally:
        psycopg.Cursor.execute = orig_execute

    check("bundle is flagged degraded", b.get("_degraded") is True)
    reasons = b.get("_degraded_reasons") or []
    check("exactly one degraded reason recorded", len(reasons) == 1, str(reasons))
    if reasons:
        r = reasons[0]
        check("reason names the right namespace", r.get("namespace") == NS, str(r))
        check("reason names the semantic arm specifically", r.get("arm") == "semantic", str(r))
        check("reason names AdminShutdown (not a generic/swallowed label)",
              r.get("error") == "AdminShutdown", str(r))
        check("reason carries the real SQLSTATE (57P01)", r.get("sqlstate") == "57P01", str(r))
    check("the failed semantic arm degrades to an EMPTY contribution, not a crash",
          b.get("sem") == [])
    check("the OTHER (raw) arm's REAL seeded content is still present -- its own probe "
          "succeeded before the semantic arm's ever failed",
          any(row.get("content") == raw_text for row in b.get("raw", [])))

    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.episodic WHERE namespace=%s", (NS,))

    conn.close()
    store.conn.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
