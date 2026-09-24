"""`memnos namespace reconcile` lookup performance (issue #155) — CI-sized version of
benchmarks/reconcile_perf.py.

Seeds an isolated tenant schema (tenant_reconcile_perf, 1536-d = production width) with
2000 target facts carrying REAL local embeddings (fastembed bge-small, 384-d, stored
tiled x4 — cosine distances unchanged) twice over, plus 2000 distractor facts in other
namespaces, then compares the pre-#155 lookup SQL (reproduced verbatim in
benchmarks/reconcile_perf.py's LegacyStore) against the shipped BrainStore lookups:

  * EXPLAIN (ANALYZE, BUFFERS): for an anchor WITH a subject and one WITHOUT, the old
    dedupe lookup (the per-fact hot path) must touch >=10x more buffers than the new one
    (the old CTE-join form computes a distance for EVERY live fact in the namespace —
    that is what grew to ~600k buffers/fact in production) and the new plan must be an
    index path (subject index, or sem_hnsw for a subject-less anchor — which one is the
    planner's call, so it is reported, not asserted); the rare negation lookup must stay
    exact (never sem_hnsw — an approximate top-8 changes which facts get closed);
  * measured wall time: sampled per-lookup latency and a full end-to-end run, new
    faster than old;
  * outcome: the full old run (single transaction) and the full new run (chunked) must
    reach the same close / expire / superseded_by decision for every row.

Asserts ratios / plan shapes, not absolute times (CI runners are slow and noisy). The
larger-scale numbers are produced by the benchmark script.
"""
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "benchmarks"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import reconcile_perf as bench  # noqa: E402
import reconcile_dataset as ds  # noqa: E402
from core.reconcile import apply_lookup_settings, run_reconcile  # noqa: E402
from core.service import reconcile_thresholds  # noqa: E402
from core.store import BrainStore  # noqa: E402

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
N = 2000
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    PASS += bool(cond); FAIL += (not cond)


def main():
    bench.TENANT = "reconcile_perf"
    bench.SCHEMA = S = "tenant_reconcile_perf"
    t0 = time.time()
    bench.setup(DSN, N, 1, None, tile=4)
    print(f"(setup incl. local embedding: {time.time() - t0:.1f}s)")
    try:
        conn = psycopg.connect(DSN, row_factory=dict_row, autocommit=True)
        apply_lookup_settings(conn)      # the session settings reconcile's own conn uses
        store, legacy = BrainStore(conn=conn), bench.LegacyStore(conn=conn)
        dedupe_t, _ = reconcile_thresholds()
        page = store.live_facts_page(S, bench.NS_NEW, limit=N)
        embs = store.fact_embeddings(S, [f["id"] for f in page])
        anchors = [{**f, "embedding": embs[f["id"]]} for f in page[::20]]

        print("=== EXPLAIN (ANALYZE, BUFFERS) ===")
        a = next(f for f in anchors if f["subject_entity"])
        a0 = next({**f, "embedding": embs[f["id"]]} for f in page if not f["subject_entity"])
        for label, x in (("anchor with a subject", a), ("subject-less anchor", a0)):
            e_old = bench.explain(conn, bench.LEGACY_DEDUPE_SQL.format(schema=S),
                                  {"id": x["id"], "ns": bench.NS_NEW, "t": dedupe_t})
            e_new = bench.explain(conn, *bench.shipped_sql(store, "older_near_duplicate", S,
                                                           bench.NS_NEW, x, dedupe_t))
            print(f"    [{label}] old: {e_old['buffers']} buffers {e_old['ms']:.2f}ms {e_old['nodes']}")
            print(f"    [{label}] new: {e_new['buffers']} buffers {e_new['ms']:.2f}ms {e_new['nodes']}")
            if x is a:
                check(f"dedupe lookup, {label}: old touches >=10x the buffers of new "
                      f"({e_old['buffers']} vs {e_new['buffers']})",
                      e_old["buffers"] >= 10 * max(1, e_new["buffers"]))
            else:
                # at this CI size the planner may legitimately prefer an exact scan of the
                # (small) namespace over sem_hnsw; the full-scale benchmark shows the flip
                check(f"dedupe lookup, {label}: new never costs more buffers than old "
                      f"({e_old['buffers']} vs {e_new['buffers']})",
                      e_new["buffers"] <= e_old["buffers"])
        # index ELIGIBILITY, independent of the planner's size-dependent choice: with the
        # non-vector access paths disabled, the new query shape is served by sem_hnsw and
        # the old one still cannot be (its right operand is a join column, not a constant)
        with conn.cursor() as c:
            c.execute("SET enable_seqscan = off")
            c.execute("SET enable_bitmapscan = off")
        f_new = bench.explain(conn, *bench.shipped_sql(store, "older_near_duplicate", S,
                                                       bench.NS_NEW, a0, dedupe_t))
        f_old = bench.explain(conn, bench.LEGACY_DEDUPE_SQL.format(schema=S),
                              {"id": a0["id"], "ns": bench.NS_NEW, "t": dedupe_t})
        with conn.cursor() as c:
            c.execute("RESET enable_seqscan")
            c.execute("RESET enable_bitmapscan")
        print(f"    [forced] new: {f_new['nodes']}\n    [forced] old: {f_old['nodes']}")
        check("new dedupe query shape IS servable by sem_hnsw",
              any("sem_hnsw" in n for n in f_new["nodes"]), str(f_new["nodes"]))
        check("old dedupe query shape is NOT (the bug), even when scans are disabled",
              not any("sem_hnsw" in n for n in f_old["nodes"]), str(f_old["nodes"]))
        n_old = bench.explain(conn, bench.LEGACY_NEAREST_SQL.format(schema=S),
                              {"id": a["id"], "ns": bench.NS_NEW, "k": 8})
        n_new = bench.explain(conn, *bench.shipped_sql(store, "nearest_live_facts_to", S,
                                                       bench.NS_NEW, a, k=8))
        print(f"    negation old: {n_old['buffers']} buffers {n_old['nodes']}")
        print(f"    negation new: {n_new['buffers']} buffers {n_new['nodes']}")
        check("the OLD lookups could never use sem_hnsw (anchor joined in from a CTE)",
              not any("sem_hnsw" in n for n in n_old["nodes"] + e_old["nodes"]))
        check("the negation lookup stays EXACT on purpose (never sem_hnsw)",
              not any("sem_hnsw" in n for n in n_new["nodes"]), str(n_new["nodes"]))

        print("=== measured lookup latency (sampled anchors) ===")

        def mean_ms(fn):
            for x in anchors[:5]:
                fn(x)
            ts = []
            for x in anchors:
                t = time.perf_counter()
                fn(x)
                ts.append((time.perf_counter() - t) * 1000)
            return statistics.mean(ts)

        od = mean_ms(lambda x: legacy.older_near_duplicate(S, bench.NS_NEW, x, dedupe_t))
        nd = mean_ms(lambda x: store.older_near_duplicate(S, bench.NS_NEW, x, dedupe_t))
        print(f"    dedupe  old {od:.2f}ms  new {nd:.2f}ms  ({od / nd:.1f}x)")
        check(f"dedupe lookup at least 2x faster ({od:.2f}ms -> {nd:.2f}ms)", nd * 2 <= od)
        conn.close()

        print("=== full run: old single-txn vs new chunked, same data ===")
        c_old = psycopg.connect(DSN, row_factory=dict_row, autocommit=False)
        t = time.perf_counter()
        r_old = bench.legacy_reconcile(c_old, bench.NS_OLD)
        t_old = time.perf_counter() - t
        c_old.close()
        t = time.perf_counter()
        r_new = run_reconcile(DSN, bench.NS_NEW, schema=S, install_signal_handlers=False)
        t_new = time.perf_counter() - t
        print(f"    old {t_old:.2f}s ({r_old})  new {t_new:.2f}s "
              f"({r_new['status']}, {r_new['chunks']} chunks)")
        check(f"full run faster ({t_old:.2f}s -> {t_new:.2f}s)", t_new < t_old)
        with psycopg.connect(DSN, row_factory=dict_row) as c:
            cmp = ds.compare(ds.snapshot(c, S, bench.NS_OLD), ds.snapshot(c, S, bench.NS_NEW))
        check(f"same deduped/closed totals (old {r_old['deduped']}/{r_old['closed']}, "
              f"new {r_new['deduped']}/{r_new['closed']})",
              (r_old["deduped"], r_old["closed"]) == (r_new["deduped"], r_new["closed"]))
        check(f"identical close/expire/superseded_by decision on all {cmp['rows']} rows",
              not cmp["decision_diffs"], str(cmp["decision_diffs"][:5]))
        check(f"restatement-counter differences only where an exact-distance tie exists "
              f"({len(cmp['restatement_diffs'])} tie rows)",
              not cmp["untied_restatement_diffs"])
    finally:
        BrainStore(DSN).drop_schema("reconcile_perf")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
