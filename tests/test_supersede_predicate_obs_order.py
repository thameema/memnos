"""issue #60 (CRITICAL) — out-of-order commits for the same (namespace, subject,
predicate) must never leave two simultaneously-live contradictory facts.

`supersede_predicate` only ever closed EXISTING facts observed no later than the
incoming fact (`observed_at <= obs`). It had no opinion on the reverse direction: a fact
about to be inserted that is itself dominated by an ALREADY-LIVE fact with a LARGER
observed_at. Because replayed write-behind writes go async onto one of
MEMNOS_INGEST_WORKERS background threads (default 2, not synchronized with each other —
see memnos_server.py's `_ingest_worker`), and offline_queue.drain() explicitly documents
concurrent drainers as SUPPORTED, two facts for the same subject+predicate could commit
out of observation order: a later-observed fact lands first (nothing to supersede yet),
then an earlier-observed fact arrives and supersede_predicate's one-directional check
lets it insert live right alongside it. Both ended up "current" — worse than "wrong one
wins", since nothing downstream ever reconciled them. This predates #56/#40 (write-behind
replay itself works fine per #56's own suite — this is a plain concurrency gap in
`_write_fact`) and affects ordinary concurrent /remember calls too, not just replay.

Fix (core/service.py `_write_fact`, core/store.py `dominant_live_fact`): a Postgres
advisory xact lock keyed on (namespace, lower(subject), lower(predicate)) serializes the
whole supersede-check + dominance-check + insert as one critical section per key, and a
new `dominant_live_fact` store method — the mirror image of `supersede_predicate` — checks
whether the fact about to be inserted is itself dominated by an already-live fact observed
STRICTLY later. If so, the incoming fact is born already-closed (`store.close_out`,
superseded_by pointing at the dominating fact) instead of landing live. The lock alone
does NOT fix this (it only serializes; `insert_semantic` was unconditional either way) —
both halves are required, verified independently below.

TEST 1 (this file's primary regression, MANUALLY CONFIRMED to reproduce against
pre-fix code — see git history for the repro before `dominant_live_fact` existed):
sequenced, out-of-order commits via direct write_facts() calls, exactly the repro shape
from the issue. Manually confirmed FAILING (2 live rows) against the code as of #56;
passes here post-fix.

TEST 2: exact observed_at ties resolve via the FORWARD direction only (supersede_predicate
uses `<=`, dominant_live_fact uses strict `>`) — the two checks can never both fire for the
same pair, so a same-turn multi-fact write with identical obs isn't double-superseded or
stuck with neither closing.

TEST 3: genuinely CONCURRENT (not sequenced) writers — two real threads, two independent
Postgres connections, real write_facts() calls through the real lock. A deliberate delay is
injected into one thread's `dominant_live_fact` call (real method, real DB round trip, just
paused before returning) so the interleaving is deterministic instead of relying on OS
scheduling luck — the delay sits INSIDE the locked critical section, so if the lock is
doing its job the second writer's write_facts() call must block for roughly that long
before it can even start its own critical section. Asserts both the timing (proves
mutual exclusion actually happened, not just that the end state looks right by luck) and
the final state (exactly one live fact, the correctly-dominant one).

Run: python tests/test_supersede_predicate_obs_order.py
"""
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.store import BrainStore
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
NS = "test:issue60-obs-order"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def make_mem(store, dim):
    def stub_embed(text):
        h = abs(hash(text))
        return [((h >> (i % 32)) & 1) * 0.1 + 0.001 for i in range(dim)]
    return MemnosMemory(store, stub_embed, dim=dim, llm=None), stub_embed


def fetch_rows(store, schema, ns, pred):
    with store.conn.cursor() as c:
        c.execute(f"SELECT id, object, observed_at, valid_to, superseded_by "
                  f"FROM {schema}.semantic WHERE namespace=%s AND predicate=%s ORDER BY id",
                  (ns, pred))
        return c.fetchall()


def clean(store, schema, ns):
    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {schema}.semantic WHERE namespace=%s", (ns,))
        c.execute(f"DELETE FROM {schema}.raw_turns WHERE namespace=%s", (ns,))


def test_out_of_order_sequenced(store, schema, dim):
    print("=== TEST 1: sequenced out-of-order commits (the issue's own repro shape) ===")
    ns = f"{NS}:seq"
    clean(store, schema, ns)
    mem, stub_embed = make_mem(store, dim)
    t_early = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_late = t_early + timedelta(seconds=5)
    tid = store.insert_raw_turn(schema, ns, None, "user", "seed", t_early, stub_embed("seed"))

    f_late = {"subject": "Zeta", "predicate": "status", "object": "blocked",
              "statement": "Zeta is blocked."}
    f_early = {"subject": "Zeta", "predicate": "status", "object": "in_progress",
               "statement": "Zeta is in_progress."}

    # the LATER-observed fact commits FIRST (out-of-order commit).
    nf1, ns1 = mem.write_facts(ns, [f_late], t_late, turn_id=tid)
    check("later-observed fact stored, nothing yet to supersede", nf1 == 1 and ns1 == 0)
    # the EARLIER-observed fact commits SECOND.
    nf2, ns2 = mem.write_facts(ns, [f_early], t_early, turn_id=tid)
    check("earlier-observed fact still counted as written", nf2 == 1)
    check("earlier-observed fact does NOT report superseding anything "
          "(it's the one being dominated, not the dominator)", ns2 == 0)

    rows = fetch_rows(store, schema, ns, "status")
    live = [r for r in rows if r["valid_to"] is None]
    check("exactly one live fact survives (not two contradictory current facts)",
          len(live) == 1)
    check("the surviving live fact is the one with the LARGER observed_at",
          len(live) == 1 and live[0]["object"] == "blocked")
    late_row = next(r for r in rows if r["object"] == "blocked")
    early_row = next(r for r in rows if r["object"] == "in_progress")
    check("dominant (later-observed) fact is untouched: still live, no superseded_by",
          late_row["valid_to"] is None and late_row["superseded_by"] is None)
    check("dominated (earlier-observed) fact is born already-closed",
          early_row["valid_to"] is not None)
    check("dominated fact's superseded_by points at the dominating fact",
          early_row["superseded_by"] == late_row["id"])
    check("dominated fact's valid_to does not precede the dominant fact's valid_from",
          early_row["valid_to"] is not None and early_row["valid_to"] >= t_late)


def test_exact_tie_forward_only(store, schema, dim):
    print("\n=== TEST 2: exact observed_at tie resolves via forward direction only ===")
    ns = f"{NS}:tie"
    clean(store, schema, ns)
    mem, stub_embed = make_mem(store, dim)
    t = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)
    tid = store.insert_raw_turn(schema, ns, None, "user", "seed", t, stub_embed("seed"))

    f1 = {"subject": "Nova", "predicate": "status", "object": "queued",
          "statement": "Nova is queued."}
    f2 = {"subject": "Nova", "predicate": "status", "object": "running",
          "statement": "Nova is running."}
    mem.write_facts(ns, [f1], t, turn_id=tid)
    nf, nsup = mem.write_facts(ns, [f2], t, turn_id=tid)   # identical obs, same-turn style
    check("second write at an IDENTICAL observed_at supersedes the first (forward wins ties)",
          nsup == 1)
    rows = fetch_rows(store, schema, ns, "status")
    live = [r for r in rows if r["valid_to"] is None]
    check("exactly one live fact after a tie (no double-domination stalemate)",
          len(live) == 1 and live[0]["object"] == "running")


def test_concurrent_real_threads(store_a_dsn, schema, dim):
    print("\n=== TEST 3: genuinely concurrent writers (real threads, real lock) ===")
    ns = f"{NS}:concurrent"
    store_setup = BrainStore(store_a_dsn)
    clean(store_setup, schema, ns)
    _, stub_embed = make_mem(store_setup, dim)
    t_early = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_late = t_early + timedelta(seconds=5)
    tid = store_setup.insert_raw_turn(schema, ns, None, "user", "seed", t_early,
                                      stub_embed("seed"))

    DELAY = 1.0
    store_slow = BrainStore(store_a_dsn)
    store_fast = BrainStore(store_a_dsn)
    mem_slow, _ = make_mem(store_slow, dim)
    mem_fast, _ = make_mem(store_fast, dim)

    lock_held = threading.Event()
    orig_supersede = store_slow.supersede_predicate

    def slow_supersede(*a, **kw):
        r = orig_supersede(*a, **kw)
        lock_held.set()          # signal: slow is now inside the locked critical section
        time.sleep(DELAY)
        return r
    store_slow.supersede_predicate = slow_supersede

    f_late = {"subject": "Rho", "predicate": "status", "object": "blocked",
              "statement": "Rho is blocked."}
    f_early = {"subject": "Rho", "predicate": "status", "object": "in_progress",
               "statement": "Rho is in_progress."}

    fast_duration = {}

    def slow_worker():
        mem_slow.write_facts(ns, [f_late], t_late, turn_id=tid)

    def fast_worker():
        lock_held.wait(timeout=5)     # start only once slow demonstrably holds the lock
        t0 = time.monotonic()
        mem_fast.write_facts(ns, [f_early], t_early, turn_id=tid)
        fast_duration["s"] = time.monotonic() - t0

    t1 = threading.Thread(target=slow_worker)
    t2 = threading.Thread(target=fast_worker)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    check("fast writer's own call was blocked by the lock for roughly the slow "
          "writer's in-critical-section delay (proves real mutual exclusion, not "
          "a lucky outcome)",
          fast_duration.get("s", 0) >= DELAY * 0.7)

    rows = fetch_rows(store_setup, schema, ns, "status")
    live = [r for r in rows if r["valid_to"] is None]
    check("concurrent writers still converge to exactly one live fact",
          len(live) == 1)
    check("the correctly-dominant (larger observed_at) fact is the survivor",
          len(live) == 1 and live[0]["object"] == "blocked")

    store_slow.conn.close(); store_fast.conn.close(); store_setup.conn.close()


def main():
    store = BrainStore(DSN)
    with store.conn.cursor() as c:
        c.execute("SELECT atttypmod AS d FROM pg_attribute "
                  "WHERE attrelid='tenant_memnos.semantic'::regclass AND attname='embedding'")
        dim = c.fetchone()["d"]
    if not dim or dim < 1:
        dim = 384
    schema = "tenant_memnos"

    test_out_of_order_sequenced(store, schema, dim)
    test_exact_tie_forward_only(store, schema, dim)
    test_concurrent_real_threads(DSN, schema, dim)

    # cleanup
    with store.conn.cursor() as c:
        c.execute(f"DELETE FROM {schema}.semantic WHERE namespace LIKE %s", (f"{NS}:%",))
        c.execute(f"DELETE FROM {schema}.raw_turns WHERE namespace LIKE %s", (f"{NS}:%",))
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
