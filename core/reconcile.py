"""Safe execution strategy for `memnos namespace reconcile` (issue #155).

The reconcile LOGIC lives in core.service (reconcile_fact — the write path's own dedupe /
SPO supersession / reversal rules). This module only decides HOW that logic runs against
a live database, so it can eventually be scheduled against production without hurting
live /recall and /remember traffic:

* CHUNKED COMMITS — and no row lock ever held across facts. The newest-first keyset walk
  reads a page of `chunk_size` facts (default 200) and COMMITS IMMEDIATELY AFTER EVERY
  FACT THAT WROTE ANYTHING; the (majority) facts that need no change are read-only and
  are batched up to the page size or `chunk_seconds` (default 1.0s) of work before their
  watermark advance is committed. So a row lock reconcile takes lives only for the few
  milliseconds of the one fact that took it — never for a batch, and never (as before
  this change) for the entire namespace walk, which was one transaction held for hours
  at production scale.

  Why not "one commit per 200 facts": that was the first design, and the live-traffic
  test disproved it. With several facts' writes in one transaction, reconcile holds
  rows from fact 1 while fact 2 waits on a row a live remember() holds — and if that
  remember() then needs a fact-1 row, it is a deadlock whose VICTIM is normally the live
  write (it began waiting first, so its deadlock_timeout expires first): 3 of 180
  concurrent remember() calls failed with DeadlockDetected in the test. Holding locks
  for one fact at a time shrinks that window to a single fact's statements, and a live
  write that needs a row reconcile is touching waits milliseconds, not up to a second.
  The cost is one extra commit (+ watermark UPDATE) per fact that actually changed
  something — negligible next to the lookups. 200 / 1s bound only the read-only
  stretches: long enough to amortise commits across the facts that change nothing,
  short enough that no snapshot is held open long enough to matter to VACUUM.

* PERSISTED WATERMARK. memnos_control.namespace_reconcile_runs stores the keyset cursor
  (observed_at key, id) of the last committed fact. It is UPDATEd in the SAME transaction
  as the chunk's mutations, so a crash / kill / lock-timeout mid-chunk rolls back both
  together: the watermark always describes exactly the work that is durably applied.
  The next run resumes after it (unless `restart=True`) — including a run that stopped
  at `limit`, so a scheduler can work through a large backlog in bounded nightly slices.
  When a walk reaches the end the row is stamped finished_at and the NEXT run re-walks
  the whole namespace from the newest fact (cheap now, and idempotent: already-reconciled
  facts produce no writes).

* ADVISORY LOCK (new pattern for this repo: SESSION-level). The write path already uses
  TRANSACTION-level `pg_advisory_xact_lock(hashtextextended(ns|subj|pred, 0))` in
  service._write_fact — one-bigint keyspace, released at commit. Reconcile must exclude
  a second reconcile of the same namespace across MANY transactions, so it takes a
  session-level `pg_try_advisory_lock(RECONCILE_LOCK_CLASS, hashtext(schema:ns))` on its
  dedicated connection — the TWO-int4 keyspace, which Postgres keeps disjoint from the
  one-bigint keyspace, so it can never collide with (or block) a write-path key. `try`
  never waits: a second run exits immediately with status 'locked'. The lock is released
  explicitly, and also implicitly if the process dies (the session ends). It needs a
  DIRECT (session-pooled) connection — behind a transaction-pooling PgBouncer a
  session-level advisory lock is meaningless; the CLI's admin DSN is direct.

* SHORT TIMEOUTS ON A DEDICATED CONNECTION. lock_timeout (default 2s) + statement_timeout
  (default 30s) are SET on reconcile's own connection only. If reconcile needs a row (or
  lock) a live writer holds, it gives up after lock_timeout instead of queueing behind
  it — the chunk is rolled back (watermark unchanged), reconcile sleeps with exponential
  backoff and retries the same chunk; after `max_retries` it exits with status
  'contention' and a resume hint, never blocking live traffic.

* CLEAN INTERRUPTION. SIGTERM / SIGINT set a stop flag; the walk commits at the next fact
  boundary (a fact is the atomic unit of reconcile logic, so a chunk cut short there is
  exactly as consistent as a full one) and exits with status 'interrupted'. A hard kill
  (SIGKILL, OOM, host crash) just loses the uncommitted chunk — the watermark is still
  the last committed one.
"""
from __future__ import annotations

import random
import signal
import threading
import time

import psycopg
from psycopg import errors as pgerr
from psycopg.rows import dict_row

from .service import reconcile_fact, reconcile_namespace, reconcile_thresholds
from .store import BrainStore

# First int4 of the two-int advisory key: ASCII "mrec" (memnos reconcile). The second is
# hashtext('<schema>:<namespace>'). Distinct keyspace from the write path's bigint keys.
RECONCILE_LOCK_CLASS = 0x6D726563

DEFAULT_CHUNK_SIZE = 200
DEFAULT_CHUNK_SECONDS = 1.0
DEFAULT_LOCK_TIMEOUT_MS = 2000
DEFAULT_STATEMENT_TIMEOUT_MS = 30000
DEFAULT_MAX_RETRIES = 6
# HNSW candidate-list size for reconcile's own lookups (pgvector default: 40). The old
# lookups were EXACT scans; the dedupe lookup of a SUBJECT-LESS anchor is now served by
# sem_hnsw (at scale — the planner's call), and candidates failing the namespace / live /
# older-than filters are discarded AFTER the index scan, so at 40 the true nearest is
# sometimes never seen. Measured on the #155 benchmark (20000 facts, 80000-row table):
# ef_search 40 left 16 facts un-deduped that the exact scan deduped; 200 left 0, at no
# measurable cost. Set on reconcile's connection only; live traffic keeps its settings.
DEFAULT_EF_SEARCH = 200

# Errors that mean "live traffic got there first / transient" — roll back the chunk,
# back off, retry it. Anything else propagates (a real bug should fail loudly).
_RETRYABLE = (pgerr.LockNotAvailable, pgerr.QueryCanceled, pgerr.DeadlockDetected,
              pgerr.SerializationFailure)

RUNS_DDL = """
CREATE SCHEMA IF NOT EXISTS memnos_control;
CREATE TABLE IF NOT EXISTS memnos_control.namespace_reconcile_runs(
    tenant_schema      text NOT NULL,
    namespace          text NOT NULL,
    cursor_observed_at timestamptz,
    cursor_id          bigint,
    facts_scanned      bigint NOT NULL DEFAULT 0,
    deduped            bigint NOT NULL DEFAULT 0,
    closed             bigint NOT NULL DEFAULT 0,
    chunks             bigint NOT NULL DEFAULT 0,
    started_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    PRIMARY KEY (tenant_schema, namespace)
);
"""


class _StopFlag:
    """Set by SIGTERM/SIGINT (or a test); wakes any backoff sleep immediately."""

    def __init__(self):
        self._ev = threading.Event()
        self.signum = None

    def set(self, signum=None, _frame=None):
        self.signum = signum
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set()

    def wait(self, seconds: float) -> bool:
        return self._ev.wait(seconds)


def apply_lookup_settings(conn, ef_search: int = DEFAULT_EF_SEARCH) -> None:
    """Session-level HNSW recall setting for reconcile's lookups (see DEFAULT_EF_SEARCH)."""
    with conn.cursor() as c:
        c.execute(f"SET hnsw.ef_search = {int(ef_search)}")


def connect(dsn: str, *, lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
            statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
            ef_search: int = DEFAULT_EF_SEARCH):
    """Reconcile's OWN connection (never a pooled server connection): non-autocommit,
    session-level timeouts set via SET (works with any DSN, overrides nothing global)."""
    conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    with conn.cursor() as c:
        c.execute("SET application_name = 'memnos-reconcile'")
        c.execute(f"SET lock_timeout = {int(lock_timeout_ms)}")
        c.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        # Per-fact commits (see module docstring) would otherwise each wait for a WAL
        # flush — measured to nearly double a 20000-fact run. Safe for THIS session:
        # every commit carries its own watermark UPDATE, so a crash that loses the last
        # few hundred ms of reconcile commits recovers to a consistent (mutations +
        # watermark together) earlier point, and the resumed run simply redoes those
        # facts. Never set on, and never affects, live-traffic connections.
        c.execute("SET synchronous_commit = off")
    apply_lookup_settings(conn, ef_search)
    conn.commit()
    return conn


def try_lock(conn, schema: str, namespace: str) -> bool:
    with conn.cursor() as c:
        c.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS ok",
                  (RECONCILE_LOCK_CLASS, f"{schema}:{namespace}"))
        ok = bool(c.fetchone()["ok"])
    conn.commit()                                   # session lock survives the commit
    return ok


def unlock(conn, schema: str, namespace: str) -> None:
    with conn.cursor() as c:
        c.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))",
                  (RECONCILE_LOCK_CLASS, f"{schema}:{namespace}"))
    conn.commit()


def _ensure_runs_table(conn) -> None:
    with conn.cursor() as c:
        c.execute(RUNS_DDL)
    conn.commit()


def read_watermark(conn, schema: str, namespace: str) -> dict | None:
    with conn.cursor() as c:
        c.execute("SELECT * FROM memnos_control.namespace_reconcile_runs "
                  "WHERE tenant_schema=%s AND namespace=%s", (schema, namespace))
        row = c.fetchone()
    conn.commit()
    return row


def _start_or_resume(conn, schema, namespace, restart: bool):
    """Returns the cursor to resume after (None = from the newest fact)."""
    row = read_watermark(conn, schema, namespace)
    if row is not None and row["finished_at"] is None and not restart:
        if row["cursor_id"] is not None:
            return (row["cursor_observed_at"], row["cursor_id"])
        return None
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO memnos_control.namespace_reconcile_runs(tenant_schema, namespace) "
            "VALUES (%s, %s) ON CONFLICT (tenant_schema, namespace) DO UPDATE SET "
            "cursor_observed_at=NULL, cursor_id=NULL, facts_scanned=0, deduped=0, closed=0, "
            "chunks=0, started_at=now(), updated_at=now(), finished_at=NULL",
            (schema, namespace))
    conn.commit()
    return None


def run_reconcile(dsn: str, namespace: str, *, schema: str, dry_run: bool = False,
                  limit: int | None = None, chunk_size: int = DEFAULT_CHUNK_SIZE,
                  chunk_seconds: float = DEFAULT_CHUNK_SECONDS, restart: bool = False,
                  lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
                  statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
                  max_retries: int = DEFAULT_MAX_RETRIES, pause_ms: int = 0,
                  stop: _StopFlag | None = None, install_signal_handlers: bool = True,
                  log=None) -> dict:
    """Run a namespace reconcile safely (see module docstring). Returns
    {status, facts_scanned, deduped, closed, chunks, retries, resumed_from, cursor}.
    status: 'complete' | 'limit' | 'interrupted' | 'contention' | 'locked' | 'dry-run'.
    Counts are for THIS invocation (a resumed run reports only what it did itself; the
    cumulative totals live in the watermark row)."""
    log = log or (lambda msg: None)
    stop = stop or _StopFlag()
    res = {"status": None, "facts_scanned": 0, "deduped": 0, "closed": 0, "chunks": 0,
           "retries": 0, "resumed_from": None, "cursor": None}
    prev_handlers = {}
    if install_signal_handlers and threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            prev_handlers[sig] = signal.signal(sig, stop.set)
    conn = connect(dsn, lock_timeout_ms=lock_timeout_ms,
                   statement_timeout_ms=statement_timeout_ms)
    locked = False
    try:
        if not try_lock(conn, schema, namespace):
            res["status"] = "locked"
            return res
        locked = True
        store = BrainStore(conn=conn)

        if dry_run:
            # Exact counts need every mutation visible to the rest of the walk, so a
            # dry-run is ONE transaction that is rolled back — it still holds the row
            # locks it takes until the end. Same timeouts + advisory lock as a real run;
            # bound it with --limit on a large live namespace.
            try:
                out = reconcile_namespace(store, namespace, schema=schema, limit=limit,
                                          page_size=chunk_size)
            except _RETRYABLE as e:
                log(f"dry-run hit {type(e).__name__} (live traffic holds a lock) — "
                    f"nothing was written; rerun later or bound it with --limit")
                res["status"] = "contention"
                return res
            finally:
                conn.rollback()
            res.update(out, status="dry-run")
            return res

        _ensure_runs_table(conn)
        cursor = _start_or_resume(conn, schema, namespace, restart)
        res["resumed_from"] = cursor
        res["cursor"] = cursor
        dedupe_thresh, neg_thresh = reconcile_thresholds()
        # progress that is COMMITTED (the watermark row says exactly the same thing)
        state = {"cursor": cursor, "facts": 0, "deduped": 0, "closed": 0, "commits": 0}

        def sync():
            res.update(cursor=state["cursor"], facts_scanned=state["facts"],
                       deduped=state["deduped"], closed=state["closed"],
                       chunks=state["commits"])

        attempt = 0
        while True:
            if stop.is_set():
                res["status"] = "interrupted"
                break
            if limit is not None and state["facts"] >= limit:
                res["status"] = "limit"
                break
            want = chunk_size if limit is None else min(chunk_size, limit - state["facts"])
            try:
                walked = _walk_page(conn, store, schema, namespace, state, want,
                                    chunk_seconds, stop, dedupe_thresh, neg_thresh)
            except _RETRYABLE as e:
                conn.rollback()      # only the in-flight fact (+ read-only ones) is lost
                sync()
                attempt += 1
                res["retries"] += 1
                if attempt > max_retries:
                    res["status"] = "contention"
                    log(f"giving up after {max_retries} retries ({type(e).__name__}); "
                        f"rerun to resume from the last committed fact")
                    break
                delay = min(30.0, 0.5 * (2 ** (attempt - 1))) * (0.5 + random.random())
                log(f"hit {type(e).__name__} (live traffic holds a lock) — rolled back, "
                    f"retry {attempt}/{max_retries} in {delay:.1f}s")
                if stop.wait(delay):
                    res["status"] = "interrupted"
                    break
                continue
            attempt = 0
            sync()
            if walked == 0:                        # walk reached the oldest live fact
                with conn.cursor() as cur:
                    cur.execute("UPDATE memnos_control.namespace_reconcile_runs SET "
                                "finished_at=now(), updated_at=now() "
                                "WHERE tenant_schema=%s AND namespace=%s", (schema, namespace))
                conn.commit()
                res["status"] = "complete"
                break
            if pause_ms > 0 and stop.wait(pause_ms / 1000.0):
                res["status"] = "interrupted"
                break
        sync()
        return res
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            if locked:
                unlock(conn, schema, namespace)
        except Exception:
            pass
        conn.close()
        for sig, h in prev_handlers.items():
            signal.signal(sig, h)


def _walk_page(conn, store, schema, namespace, state, want, chunk_seconds, stop,
               dedupe_thresh, neg_thresh) -> int:
    """Reconcile the next page of facts after state['cursor']. COMMITS after every fact
    that wrote something (so no row lock is ever held across facts) and at the end of a
    read-only stretch (page end, time budget, or stop request) — each commit advances the
    watermark row to the last fact processed IN THE SAME TRANSACTION, and only then is
    `state` (committed progress) updated. Returns facts walked (0 = walk complete). A
    retryable error propagates with `state` still exactly equal to what is committed."""
    t0 = time.monotonic()
    page = store.live_facts_page(schema, namespace, before=state["cursor"], limit=want)
    if not page:
        conn.rollback()
        return 0
    embs = store.fact_embeddings(schema, [f["id"] for f in page])
    pend = {"n": 0, "d": 0, "c": 0, "last": None}
    walked = 0

    def commit():
        if not pend["n"]:
            conn.rollback()                        # read-only: just end the snapshot
            return
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memnos_control.namespace_reconcile_runs SET cursor_observed_at=%s, "
                "cursor_id=%s, facts_scanned=facts_scanned+%s, deduped=deduped+%s, "
                "closed=closed+%s, chunks=chunks+1, updated_at=now() "
                "WHERE tenant_schema=%s AND namespace=%s",
                (pend["last"][0], pend["last"][1], pend["n"], pend["d"], pend["c"],
                 schema, namespace))
        conn.commit()
        state.update(cursor=pend["last"], facts=state["facts"] + pend["n"],
                     deduped=state["deduped"] + pend["d"], closed=state["closed"] + pend["c"],
                     commits=state["commits"] + 1)
        pend.update(n=0, d=0, c=0)

    for f in page:
        if walked and (stop.is_set() or time.monotonic() - t0 >= chunk_seconds):
            break                                  # stop at a fact boundary
        walked += 1
        pend["last"] = (f["obs_key"], f["id"])
        pend["n"] += 1
        if not store.is_live(schema, namespace, f["id"]):   # closed by an earlier fact
            continue
        d, c = reconcile_fact(store, schema, namespace,
                              {**f, "embedding": embs.get(f["id"])},
                              dedupe_thresh=dedupe_thresh, neg_thresh=neg_thresh)
        pend["d"] += d
        pend["c"] += c
        if d or c:
            commit()                               # never carry row locks to the next fact
    commit()
    return walked
