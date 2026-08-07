"""insert_raw_turn survives a failing namespace-registry upsert (PR #43 review, finding 3).

core/store.py:insert_raw_turn's write-path auto-registration (issue #41 fix A) upserts a
row into memnos_control.namespaces AFTER the raw_turn row is already durably committed
(connections run autocommit=True, so the raw_turns INSERT is its own committed transaction
by the time the registry upsert even runs). Before this fix, that second statement had no
error handling: if it raised for ANY reason (a lock wait on memnos_control.namespaces, a
transient connection blip, that table's own statement_timeout), the exception propagated
out of insert_raw_turn() as if the WHOLE write had failed -- even though the turn was
already saved. A caller that retries on failure (a normal resilience pattern) would then
create a duplicate turn; a caller that doesn't retry gets a spurious error for a write that
actually succeeded.

Covers:
  1. insert_raw_turn returns a valid turn id and does NOT raise even when every registry
     upsert on this connection is forced to fail.
  2. Exactly one raw_turn row exists for that call -- the failure doesn't roll back or
     duplicate the already-committed write.
  3. A caller using a normal retry-on-exception pattern (mirroring remember_turn /
     ingest_session callers) does NOT end up retrying at all -- because insert_raw_turn
     never raised in the first place -- so no duplicate turn is created even though the
     registry upsert fails on every attempt.
  4. The failure is logged (not silently swallowed) via the module logger.
  5. The namespace registry itself is genuinely left unregistered (proves the failure
     is real, not a no-op the test accidentally didn't exercise).

No server needed (direct-DB store path, same pattern as test_readable_namespaces_registry.py).
"""
import logging
import os
import sys
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from datetime import datetime, timezone

import psycopg

from core.store import BrainStore
import core.store as store_mod

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

NS = "test:insertrawturn_regfail"


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


class _RegistryUpsertFailure(RuntimeError):
    pass


def _patched_execute(orig_execute, match_substr, exc_cls):
    def _execute(self, query, *a, **kw):
        q = query if isinstance(query, str) else str(query)
        if match_substr in q:
            raise exc_cls("simulated transient failure on the registry upsert")
        return orig_execute(self, query, *a, **kw)
    return _execute


def _caller_with_retry(fn, *args, max_attempts=3, **kwargs):
    """Mirrors a normal resilience pattern a real caller (an HTTP client, an ingest job)
    might use: retry on any exception. If insert_raw_turn is fixed, this NEVER retries --
    the first call already returns successfully."""
    attempts = 0
    while True:
        attempts += 1
        try:
            return fn(*args, **kwargs), attempts
        except Exception:
            if attempts >= max_attempts:
                raise


def main():
    store = BrainStore(DSN)
    store.create_schema("memnos")
    conn = store.conn

    def reset():
        with conn.cursor() as c:
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (NS,))
        store_mod._known_registered_namespaces.discard(NS)

    reset()
    now = datetime.now(timezone.utc)
    orig_execute = psycopg.Cursor.execute

    # --- 1 & 2. a single call survives the injected failure, exactly one row committed ---
    print("=== single write survives a failing registry upsert ===")
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, "INSERT INTO memnos_control.namespaces", _RegistryUpsertFailure)
    try:
        with patch.object(store_mod.logger, "warning") as mock_warn:
            tid = store.insert_raw_turn(SCHEMA, NS, None, "user", "first write", now, None)
    finally:
        psycopg.Cursor.execute = orig_execute

    check("insert_raw_turn returns a valid turn id despite the injected registry-upsert "
          "failure (does not raise)", isinstance(tid, int))
    check("the failure is logged via the module logger (not silently swallowed)",
          mock_warn.called)

    with conn.cursor() as c:
        c.execute(f"SELECT count(*) AS n, array_agg(id) AS ids FROM {SCHEMA}.raw_turns "
                  f"WHERE namespace=%s AND text=%s", (NS, "first write"))
        row = c.fetchone()
    check("exactly one raw_turn row exists for that call -- the already-committed write "
          "was not rolled back or duplicated by the downstream failure",
          row["n"] == 1 and row["ids"] == [tid])

    # --- 5. the namespace registry is genuinely unregistered (the failure was real) ------
    with conn.cursor() as c:
        c.execute("SELECT 1 FROM memnos_control.namespaces WHERE name=%s", (NS,))
        check("sanity: the namespace registry row was NOT created (proves the injected "
              "failure genuinely prevented the upsert, this isn't a vacuous pass)",
              c.fetchone() is None)
    check("the process-level registration cache was NOT updated on failure (so a later, "
          "healthy write still retries the upsert instead of skipping it forever)",
          NS not in store_mod._known_registered_namespaces)

    # --- 3. a retrying caller does not end up duplicating the turn -----------------------
    print("=== a caller using a normal retry-on-exception pattern does not retry at all ===")
    reset()
    psycopg.Cursor.execute = _patched_execute(
        orig_execute, "INSERT INTO memnos_control.namespaces", _RegistryUpsertFailure)
    try:
        result, attempts = _caller_with_retry(
            store.insert_raw_turn, SCHEMA, NS, None, "user", "retry-prone write", now, None)
    finally:
        psycopg.Cursor.execute = orig_execute

    check("a retry-wrapped caller succeeds on the FIRST attempt (never had to retry) -- "
          "insert_raw_turn never raised even though the registry upsert failed on every "
          "call it would have made", attempts == 1)

    with conn.cursor() as c:
        c.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns WHERE namespace=%s AND text=%s",
                  (NS, "retry-prone write"))
        n = c.fetchone()["n"]
    check("exactly ONE raw_turn row exists -- a resilience-pattern caller retrying on "
          "error would otherwise have double-inserted this turn (the exact failure "
          "scenario finding 3 describes)", n == 1)

    reset()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
