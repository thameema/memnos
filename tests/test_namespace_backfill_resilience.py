"""Namespace registry backfill resilience (issue #41 fix A follow-up, PR #43 review).

Before this fix, Control.init() ran _run_namespace_registry_backfill() on a pooled
connection with the standard request-scoped statement_timeout (default 15s) active and NO
exemption -- i.e. the exact full-table-scan pattern issue #41 exists to get off a
15s-timeout connection, just moved from request time to boot time. On a large
pre-existing deployment (exactly what the backfill targets), that scan could raise
psycopg.errors.QueryCanceled uncaught out of Control.init() and crash the server before it
served any traffic. Separately, the old completion guard only cleared once a row got
auto_registered=true via the backfill's own INSERT -- but that INSERT is `ON CONFLICT DO
NOTHING`, so a deployment where every namespace was already explicitly registered before
this PR shipped could never produce such a row, meaning the guard reported "needs backfill"
forever and the full scan re-ran on every single restart.

Covers:
  1. TIMEOUT EXEMPTION: the backfill completes without QueryCanceled even on a connection
     whose statement_timeout is calibrated (in-test, against THIS Postgres instance -- not
     a hardcoded wall-clock guess) to reliably cancel the OLD, unexempted scan pattern.
     Also proves the connection's statement_timeout is restored to its prior value
     afterward (not left at 0 indefinitely).
  2. CHUNKING: the backfill issues MULTIPLE bounded INSERT...SELECT statements (keyset-
     paginated by id) rather than one unbounded scan, so a huge table means more batches,
     not one query that is itself the failure point.
  3. MARKER-DRIVEN COMPLETION: once the backfill completes, _namespace_registry_needs_backfill
     returns False and STAYS False even after new, never-backfilled data appears --
     proving the guard is driven by the completion marker, not by data state (i.e. no
     re-scan "on every restart" once the marker exists).
  4. THE CRASH-LOOP DEPLOYMENT SHAPE (finding 1b): a namespace that was already explicitly
     registered (auto_registered=false) before this PR shipped -- the backfill's
     ON CONFLICT DO NOTHING is a no-op for it, yet the marker still gets set (unconditional),
     so the guard correctly reports "no backfill needed" afterward instead of looping.
  5. Idempotency: running the backfill again after the marker is set is a fast no-op
     (skipped by the guard) and doesn't duplicate/alter any registry row.

No server needed (direct-DB control-plane path, same pattern as
test_readable_namespaces_registry.py).
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from core.store import BrainStore
from core.control import Control, NAMESPACE_BACKFILL_BATCH

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

NS_PREFIX = "test:backfillresil:"
NS_EXPLICIT_PREREG = "test:backfillresil_crashloop:explicit"
NS_CHUNK_PREFIX = "test:backfillresil_chunk:"

# The OLD (pre-fix) query shape: an unbounded UNION of two full-table scans, no id
# bounds, no timeout exemption -- exactly what _run_namespace_registry_backfill used to
# run directly on Control.init()'s connection.
_OLD_STYLE_SCAN = f"""
    SELECT namespace FROM (
        SELECT namespace FROM {SCHEMA}.raw_turns
        UNION
        SELECT namespace FROM {SCHEMA}.semantic
    ) existing
"""


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def _measure_ms(conn, sql, fetch=False):
    with conn.cursor() as c:
        t0 = time.time()
        c.execute(sql)
        if fetch:
            c.fetchall()
        return (time.time() - t0) * 1000


def main():
    store = BrainStore(DSN)
    store.create_schema("memnos")
    conn = store.conn
    Control.init(conn)

    def reset(vacuum=False):
        with conn.cursor() as c:
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace LIKE %s", (NS_PREFIX + '%',))
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace LIKE %s", (NS_CHUNK_PREFIX + '%',))
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS_EXPLICIT_PREREG,))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name LIKE %s", (NS_PREFIX + '%',))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name LIKE %s", (NS_CHUNK_PREFIX + '%',))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (NS_EXPLICIT_PREREG,))
            if vacuum:
                # this file seeds and deletes millions of rows to simulate a large
                # pre-existing table -- reclaim the dead tuples immediately rather than
                # leaving bloat for autovacuum to clean up on its own schedule, so
                # whatever test runs next in the same CI job's shared Postgres doesn't
                # inherit a slower raw_turns table.
                c.execute(f"VACUUM {SCHEMA}.raw_turns")

    reset()

    print("=== sanity: keyset chunking constant is a real batch size ===")
    check("NAMESPACE_BACKFILL_BATCH is a positive int",
          isinstance(NAMESPACE_BACKFILL_BATCH, int) and NAMESPACE_BACKFILL_BATCH > 0)

    # --- 1 & 2: timeout exemption + chunking, on a large-ish simulated pre-existing table --
    print("=== boot-time backfill survives a statement_timeout that would cancel the old scan ===")
    with conn.cursor() as c:
        for _ in range(5):            # warm the connection so round-trip overhead is steady
            c.execute("SELECT 1")
        n = 5_000_000
        c.execute(
            f"INSERT INTO {SCHEMA}.raw_turns(namespace,session_id,speaker,text,observed_at) "
            f"SELECT %s||(i%%50), NULL, 'user', 'turn '||i, now() "
            f"FROM generate_series(1,%s) AS i",
            (NS_PREFIX, n))

    # calibrated against THIS live instance, not a hardcoded wall-clock guess -- avoids
    # flakiness across dev machines / CI hardware with very different round-trip overhead.
    # baseline is the MAX (not min) of many samples, and the multiplier/floor are generous
    # -- this test can run alongside a live server + the rest of the suite hammering the
    # same DB, so trivial-query latency can spike well above an idle-machine reading; a
    # 2M-row table keeps the old-style scan orders of magnitude slower than that spike
    # headroom regardless.
    baseline_ms = max(_measure_ms(conn, "SELECT 1") for _ in range(20))
    old_scan_ms = _measure_ms(conn, _OLD_STYLE_SCAN, fetch=True)
    timeout_ms = max(int(baseline_ms * 10), 100)
    check(f"calibration sanity: the old-style full scan ({old_scan_ms:.1f}ms) is far slower "
          f"than the chosen statement_timeout ({timeout_ms}ms) on this instance -- "
          "otherwise this test can't prove anything",
          old_scan_ms > timeout_ms * 5)

    with conn.cursor() as c:
        c.execute(f"SET statement_timeout = {timeout_ms}")
        raised = False
        try:
            c.execute(_OLD_STYLE_SCAN)
            c.fetchall()
        except psycopg.errors.QueryCanceled:
            raised = True
        check(f"sanity: the OLD unexempted/unchunked scan DOES get canceled at {timeout_ms}ms "
              "on this data (proves the trap is real, not just asserting success blindly)",
              raised)

        c.execute("DELETE FROM memnos_control.namespace_registry_backfill")
        statements = []
        orig_execute = psycopg.Cursor.execute

        def _recording_execute(self, query, *a, **kw):
            statements.append(query if isinstance(query, str) else str(query))
            return orig_execute(self, query, *a, **kw)

        psycopg.Cursor.execute = _recording_execute
        crashed = False
        try:
            Control._run_namespace_registry_backfill(c)
        except psycopg.errors.QueryCanceled:
            crashed = True
        finally:
            psycopg.Cursor.execute = orig_execute
        check("the FIXED backfill does NOT raise QueryCanceled under the same "
              f"{timeout_ms}ms connection timeout that cancels the old scan (statement_timeout "
              "exemption works)", not crashed)

        exempt_issued = any("statement_timeout = 0" in s for s in statements)
        check("backfill issued a statement_timeout=0 exemption before scanning",
              exempt_issued)

        chunk_inserts = [s for s in statements
                          if "INSERT INTO memnos_control.namespaces" in s and "SELECT DISTINCT" in s]
        check(f"backfill issued MULTIPLE bounded chunk queries ({len(chunk_inserts)}), not one "
              "unbounded scan -- a huge table means more batches, not one query that's the "
              "failure point", len(chunk_inserts) >= 2)
        check("every chunk query is bounded by an id range (id > ... AND id <= ...), not an "
              "unbounded full-table scan",
              all("id >" in s and "id <=" in s for s in chunk_inserts))

        c.execute("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'")
        restored = c.fetchone()["setting"]
        check(f"connection's statement_timeout is restored to {timeout_ms}ms after the "
              f"backfill (got {restored!r}) -- not left disabled", str(restored) == str(timeout_ms))

        c.execute("SET statement_timeout = 0")   # don't let a leftover tiny timeout break the rest of this test

    with conn.cursor() as c:
        c.execute("SELECT count(DISTINCT name) AS n FROM memnos_control.namespaces WHERE name LIKE %s",
                   (NS_PREFIX + '%',))
        check("the large simulated table's 50 namespaces all landed in the registry despite "
              "the chunking", c.fetchone()["n"] == 50)

    # --- 3. marker-driven completion: guard stays False even when fresh unregistered data appears
    print("=== completion guard is marker-driven, not data-driven (no re-scan on restart) ===")
    with conn.cursor() as c:
        check("guard reports NOT needed right after the backfill completes",
              Control._namespace_registry_needs_backfill(c) is False)
        now = datetime.now(timezone.utc)
        c.execute(
            f"INSERT INTO {SCHEMA}.raw_turns(namespace,session_id,speaker,text,observed_at) "
            f"VALUES(%s,NULL,'user','never-backfilled data appearing after the marker',%s)",
            (NS_PREFIX + "postmarker", now))
        check("guard STAYS False even after new, never-registered data appears -- proves it's "
              "driven by the completion marker, not by scanning tenant_memnos.* for gaps "
              "(this is what stops the full scan from re-running on every restart)",
              Control._namespace_registry_needs_backfill(c) is False)

    # --- 4. the crash-loop deployment shape: every namespace already explicitly registered --
    print("=== crash-loop deployment shape: all-namespaces-already-registered ===")
    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.namespace_registry_backfill")
        now = datetime.now(timezone.utc)
        c.execute(
            f"INSERT INTO {SCHEMA}.raw_turns(namespace,session_id,speaker,text,observed_at) "
            f"VALUES(%s,NULL,'user','pre-existing data, namespace already registered by an admin',%s)",
            (NS_EXPLICIT_PREREG, now))
        # simulates a disciplined-admin deployment: every namespace was create_namespace()'d
        # (auto_registered=false) BEFORE this PR's write-path auto-registration existed
        c.execute("INSERT INTO memnos_control.namespaces(name, auto_registered) VALUES(%s, false)",
                   (NS_EXPLICIT_PREREG,))
        check("sanity: guard reports backfill needed (marker was just cleared)",
              Control._namespace_registry_needs_backfill(c) is True)

        Control._run_namespace_registry_backfill(c)
        c.execute("SELECT auto_registered FROM memnos_control.namespaces WHERE name=%s",
                   (NS_EXPLICIT_PREREG,))
        check("the pre-registered namespace's auto_registered flag is untouched by "
              "ON CONFLICT DO NOTHING (still False)", c.fetchone()["auto_registered"] is False)
        check("guard reports NOT needed afterward EVEN THOUGH the backfill inserted zero "
              "rows for this namespace -- this is finding 1(b): before this fix, a "
              "deployment shaped exactly like this could never satisfy the old "
              "auto_registered-row guard and would re-scan forever",
              Control._namespace_registry_needs_backfill(c) is False)

    # --- 5. idempotency ------------------------------------------------------------------
    print("=== idempotency ===")
    with conn.cursor() as c:
        c.execute("SELECT count(*) AS n FROM memnos_control.namespaces WHERE name=%s",
                   (NS_EXPLICIT_PREREG,))
        before = c.fetchone()["n"]
        Control._run_namespace_registry_backfill(c)   # marker already set -- callers gate via
                                                        # the guard, but the method itself must
                                                        # still be safe to call again directly
        c.execute("SELECT count(*) AS n FROM memnos_control.namespaces WHERE name=%s",
                   (NS_EXPLICIT_PREREG,))
        after = c.fetchone()["n"]
    check("re-running the backfill directly doesn't duplicate/alter existing registry rows",
          before == after == 1)

    reset(vacuum=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
