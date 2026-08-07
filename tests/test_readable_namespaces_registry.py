"""readable_namespaces registry gate (issue #41 fix A).

Before this fix, a wildcard grant's namespace expansion ran two full-table DISTINCT
scans (`SELECT DISTINCT namespace FROM tenant_memnos.raw_turns UNION ... semantic`) on
every wide recall for that principal — 528ms warm for the raw_turns scan alone per the
issue, and cold/contended it blew the 15s statement_timeout. The fix resolves wildcard
grants against memnos_control.namespaces (small, control-plane) instead, kept COMPLETE by
(a) core/store.py insert_raw_turn auto-registering a namespace on its first write, and
(b) a one-time boot-time backfill (Control._run_namespace_registry_backfill) for namespaces
that already had data before this fix shipped.

Covers:
  1. readable_namespaces() no longer issues the DISTINCT-scan query for a wildcard grant
     (asserted via a recording cursor.execute wrapper — no pg_stat_statements dependency).
  2. A namespace that receives its first write is IMMEDIATELY visible to a wildcard grant
     (no backfill needed) — the write-path auto-registration.
  3. A namespace with PRE-EXISTING data but no registry row (simulating data written
     before this fix) is invisible to a wildcard grant UNTIL the backfill runs, then
     becomes visible — proves the registry is genuinely the source of truth (not silently
     still data-scanning under the hood) and that the backfill actually closes the gap.
  4. Grant scoping is unaffected: a registered/data-bearing namespace OUTSIDE the
     wildcard's prefix is never included.
  5. list_namespaces()'s `registered` field still distinguishes an explicitly-created
     namespace from one that only self-registered via a write (ui/app.js "discovered"
     pill) — auto-registration must not make everything read as registered.

No server needed (direct-DB control-plane path, same pattern as test_namespace_prune.py).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from datetime import datetime, timezone

import psycopg

from core.store import BrainStore
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

NS_WRITE = "test:rnsreg:write"
NS_LEGACY = "test:rnsreg:legacy"
NS_EXPLICIT = "test:rnsreg:explicit"
NS_OUTSIDE = "test:rnsreg_outside:data"
ALL_NS = [NS_WRITE, NS_LEGACY, NS_EXPLICIT, NS_OUTSIDE]
WILDCARD = "test:rnsreg:*"


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def main():
    store = BrainStore(DSN)
    store.create_schema("memnos")
    conn = store.conn
    Control.init(conn)

    def reset():
        with conn.cursor() as c:
            for ns in ALL_NS:
                c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (ns,))
                c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (ns,))
                c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (ns,))
                c.execute("DELETE FROM memnos_control.grants WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.grants WHERE namespace=%s", (WILDCARD,))
            c.execute("DELETE FROM memnos_control.principals WHERE name='rnsreg_test_agent'")

    reset()

    pid = Control.create_principal(conn, "rnsreg_test_agent", "agent")
    Control.grant(conn, pid, WILDCARD, can_read=True, can_write=False)

    now = datetime.now(timezone.utc)

    # --- 1. write-path auto-registration: a fresh namespace's first write registers it ----
    print("=== write-path auto-registration ===")
    store.insert_raw_turn(SCHEMA, NS_WRITE, None, "user", "hello from the write path", now, None)
    with conn.cursor() as c:
        c.execute("SELECT auto_registered FROM memnos_control.namespaces WHERE name=%s", (NS_WRITE,))
        row = c.fetchone()
    check(f"{NS_WRITE} landed in the registry as auto_registered=true after its first write",
          row is not None and row["auto_registered"] is True)

    nss = Control.readable_namespaces(conn, pid)
    check(f"wildcard grant sees {NS_WRITE} immediately (no backfill needed)", NS_WRITE in nss)

    # non-blocking review finding: the registry upsert must not re-run on every write to an
    # already-registered namespace -- a process-level cache should skip it after the first
    # successful write, so an N-turn ingest_session pays the round trip once, not N times.
    print("=== registry upsert is cached per-process after the first write (no re-upsert) ===")
    statements = []
    orig_execute = psycopg.Cursor.execute

    def _recording_execute(self, query, *a, **kw):
        q = query if isinstance(query, str) else str(query)
        if "memnos_control.namespaces" in q and "INSERT" in q:
            statements.append(q)
        return orig_execute(self, query, *a, **kw)

    psycopg.Cursor.execute = _recording_execute
    try:
        for i in range(10):
            store.insert_raw_turn(SCHEMA, NS_WRITE, None, "user", f"turn {i}", now, None)
    finally:
        psycopg.Cursor.execute = orig_execute
    check(f"10 more writes to the already-registered {NS_WRITE} issued ZERO additional "
          f"registry upserts ({len(statements)} issued) -- the process-level cache "
          "skips the now-redundant round trip", len(statements) == 0)

    # --- 2. legacy data (pre-fix: no registry row) is invisible until backfill -------------
    print("=== pre-existing (unregistered) data + backfill ===")
    with conn.cursor() as c:
        c.execute(
            f"INSERT INTO {SCHEMA}.raw_turns(namespace,session_id,speaker,text,observed_at) "
            f"VALUES(%s,NULL,'user','legacy turn, written before issue #41 shipped',%s)",
            (NS_LEGACY, now))
    with conn.cursor() as c:
        c.execute("SELECT 1 FROM memnos_control.namespaces WHERE name=%s", (NS_LEGACY,))
        check(f"sanity: {NS_LEGACY} has data but NO registry row yet", c.fetchone() is None)

    nss = Control.readable_namespaces(conn, pid)
    check(f"BEFORE backfill: wildcard grant does NOT see {NS_LEGACY} "
          "(proves resolution is genuinely registry-driven, not still data-scanning)",
          NS_LEGACY not in nss)

    # called directly (bypassing the module-level "has it ever run" guard, which this
    # shared test DB may already satisfy from other tests/prior writes) so the backfill
    # SQL itself is exercised deterministically regardless of that global state.
    with conn.cursor() as c:
        Control._run_namespace_registry_backfill(c)

    with conn.cursor() as c:
        c.execute("SELECT auto_registered FROM memnos_control.namespaces WHERE name=%s", (NS_LEGACY,))
        row = c.fetchone()
    check(f"AFTER backfill: {NS_LEGACY} is in the registry as auto_registered=true",
          row is not None and row["auto_registered"] is True)

    nss = Control.readable_namespaces(conn, pid)
    check(f"AFTER backfill: wildcard grant now sees {NS_LEGACY}", NS_LEGACY in nss)

    # idempotency: running the backfill's scan+insert logic again (still called directly,
    # bypassing the guard, same as above) must not error or duplicate/alter any row --
    # including the completion marker row this PR's fix added.
    with conn.cursor() as c:
        Control._run_namespace_registry_backfill(c)
        c.execute("SELECT count(*) AS n FROM memnos_control.namespaces WHERE name=%s", (NS_LEGACY,))
        check("backfill is idempotent (re-running it doesn't duplicate the namespace row)",
              c.fetchone()["n"] == 1)
        c.execute("SELECT count(*) AS n FROM memnos_control.namespace_registry_backfill")
        check("the completion marker stays a singleton even after multiple direct backfill "
              "calls (no duplicate/error on the marker row either)", c.fetchone()["n"] == 1)

    # the boot-time guard is MARKER-driven (a dedicated completion row set unconditionally
    # at the end of a successful backfill), NOT derived from whether any auto_registered
    # row happens to exist -- PR #43 review finding 1(b): a deployment where every
    # namespace was already explicitly registered before this PR shipped would never
    # produce an auto_registered row via the backfill's ON CONFLICT DO NOTHING, which made
    # an auto_registered-row-driven guard report "needs backfill" forever on that shape
    # (see tests/test_namespace_backfill_resilience.py for that scenario end-to-end). This
    # marker is what keeps Control.init() from re-scanning tenant_memnos.* on every restart.
    with conn.cursor() as c:
        check("boot-time guard reports backfill NOT needed once the completion marker exists",
              Control._namespace_registry_needs_backfill(c) is False)

    # --- 3. explicit registration (no data yet) is also visible to the wildcard ------------
    print("=== explicit registration, no data ===")
    Control.create_namespace(conn, NS_EXPLICIT, description="explicitly created, no data")
    nss = Control.readable_namespaces(conn, pid)
    check(f"wildcard grant sees explicitly-registered {NS_EXPLICIT} even with zero data",
          NS_EXPLICIT in nss)

    # --- 4. grant scoping: a namespace outside the wildcard prefix is never included -------
    print("=== grant scoping unaffected ===")
    store.insert_raw_turn(SCHEMA, NS_OUTSIDE, None, "user", "unrelated namespace", now, None)
    nss = Control.readable_namespaces(conn, pid)
    check(f"{NS_OUTSIDE} (outside the wildcard prefix) is never included even though it "
          "has data and a registry row", NS_OUTSIDE not in nss)

    # --- 5. no DISTINCT-scan query is issued for a wildcard grant (the actual fix) ---------
    print("=== no DISTINCT-scan on the recall hot path ===")
    statements = []
    orig_execute = psycopg.Cursor.execute

    def _recording_execute(self, query, *a, **kw):
        statements.append(query if isinstance(query, str) else str(query))
        return orig_execute(self, query, *a, **kw)

    psycopg.Cursor.execute = _recording_execute
    try:
        nss = Control.readable_namespaces(conn, pid)
    finally:
        psycopg.Cursor.execute = orig_execute

    old_scan_issued = any(
        "distinct" in s.lower() and "tenant_memnos" in s.lower() for s in statements)
    registry_query_issued = any("memnos_control.namespaces" in s for s in statements)
    check("readable_namespaces() issued NO DISTINCT scan over tenant_memnos.* "
          f"({len(statements)} statement(s) executed)", not old_scan_issued)
    check("readable_namespaces() DID query the memnos_control.namespaces registry",
          registry_query_issued)
    check("...and the result is still correct with the recording wrapper on",
          {NS_WRITE, NS_LEGACY, NS_EXPLICIT} <= set(nss) and NS_OUTSIDE not in nss)

    # --- 6. list_namespaces() `registered` still distinguishes auto vs explicit ------------
    print("=== registered vs. auto_registered (ui/app.js \"discovered\" pill) ===")
    by_name = {n["name"]: n for n in Control.list_namespaces(conn)}
    check(f"{NS_EXPLICIT} (create_namespace) reads as registered=True",
          by_name.get(NS_EXPLICIT, {}).get("registered") is True)
    check(f"{NS_WRITE} (write-only, never explicitly created) reads as registered=False "
          "(still shows the \"discovered\" pill)",
          by_name.get(NS_WRITE, {}).get("registered") is False)
    check(f"{NS_LEGACY} (backfilled, never explicitly created) reads as registered=False",
          by_name.get(NS_LEGACY, {}).get("registered") is False)

    # re-registering an auto-registered namespace explicitly should claim it
    Control.create_namespace(conn, NS_WRITE, description="claimed by an admin after the fact")
    by_name = {n["name"]: n for n in Control.list_namespaces(conn)}
    check(f"explicitly re-registering {NS_WRITE} flips it to registered=True "
          "(create_namespace claims a previously auto-registered row)",
          by_name.get(NS_WRITE, {}).get("registered") is True)

    reset()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
