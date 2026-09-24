"""Before/after benchmark for `memnos namespace reconcile` (issue #155).

BEFORE = the pre-#155 execution strategy, reproduced verbatim below (LegacyStore's two
lookups are the old SQL copied from core/store.py, and legacy_reconcile is the old
single-transaction snapshot walk). AFTER = the shipped code: BrainStore's HNSW-servable
lookups driven by core.reconcile.run_reconcile (chunked commits, watermark, advisory lock).
Both run the SAME per-fact logic (core.service.reconcile_fact) — only the lookup SQL and
the transaction strategy differ — so the benchmark also diffs the two end states
row-for-row to show the outcome is unchanged.

Needs an ISOLATED throwaway Postgres + pgvector (never a live memnos database):

    docker run -d --name rbench -e POSTGRES_USER=memnos -e POSTGRES_PASSWORD=memnos \\
        -e POSTGRES_DB=memnos -p 55437:5432 pgvector/pgvector:pg16
    MEMNOS_DSN=postgresql://memnos:memnos@localhost:55437/memnos \\
        python benchmarks/reconcile_perf.py --sizes 2000,5000,10000

Uses real local embeddings (fastembed bge-small, 384-d); no OpenAI key needed. Each size
gets a fresh tenant schema holding the target namespace TWICE (one copy per strategy)
plus `--distractor-ratio` x N facts spread over other namespaces, like a real multi-
namespace semantic table.
"""
from __future__ import annotations

import argparse
import os
import pickle
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from core.reconcile import apply_lookup_settings, run_reconcile  # noqa: E402
from core.service import reconcile_fact, reconcile_thresholds  # noqa: E402
from core.store import BrainStore  # noqa: E402
import reconcile_dataset as ds  # noqa: E402

TENANT = "rbench"
SCHEMA = f"tenant_{TENANT}"
NS_OLD, NS_NEW = "bench:old", "bench:new"


class LegacyStore(BrainStore):
    """The pre-#155 lookups, verbatim SQL: the anchor comes from a CTE and the distance
    is `s.embedding <=> a.embedding` (a join column — no HNSW index path)."""

    def older_near_duplicate(self, schema, ns, anchor, thresh, *, k=8):
        if thresh <= 0:
            return None
        with self.conn.cursor() as c:
            c.execute(LEGACY_DEDUPE_SQL.format(schema=schema),
                      {"id": anchor["id"], "ns": ns, "t": thresh})
            return c.fetchone()

    def nearest_live_facts_to(self, schema, ns, anchor, *, k=8):
        with self.conn.cursor() as c:
            c.execute(LEGACY_NEAREST_SQL.format(schema=schema),
                      {"id": anchor["id"], "ns": ns, "k": k})
            return c.fetchall()


LEGACY_DEDUPE_SQL = (
    "WITH a AS (SELECT id, embedding, subject_entity, observed_at "
    "           FROM {schema}.semantic WHERE id=%(id)s AND namespace=%(ns)s) "
    "SELECT s.id, s.statement, (s.embedding <=> a.embedding) AS dist "
    "FROM {schema}.semantic s, a "
    "WHERE s.namespace=%(ns)s AND s.kind='fact' AND s.valid_to IS NULL "
    "AND s.expired_at IS NULL AND s.embedding IS NOT NULL "
    "AND (coalesce(s.observed_at,'epoch'), s.id) < (coalesce(a.observed_at,'epoch'), a.id) "
    "AND (a.subject_entity IS NULL OR s.subject_entity IS NULL "
    "     OR lower(s.subject_entity)=lower(a.subject_entity)) "
    "AND (s.embedding <=> a.embedding) < %(t)s "
    "ORDER BY s.embedding <=> a.embedding LIMIT 1")

LEGACY_NEAREST_SQL = (
    "WITH a AS (SELECT id, embedding, observed_at "
    "           FROM {schema}.semantic WHERE id=%(id)s AND namespace=%(ns)s) "
    "SELECT s.id, s.statement, s.subject_entity, "
    "       (s.embedding <=> a.embedding) AS dist "
    "FROM {schema}.semantic s, a "
    "WHERE s.namespace=%(ns)s AND s.kind='fact' AND s.valid_to IS NULL "
    "AND s.expired_at IS NULL AND s.embedding IS NOT NULL AND s.id <> a.id "
    "AND (a.observed_at IS NULL OR s.observed_at <= a.observed_at) "
    "ORDER BY s.embedding <=> a.embedding LIMIT %(k)s")


def legacy_reconcile(conn, ns) -> dict:
    """The pre-#155 driver: snapshot every live fact up front, walk them all inside ONE
    transaction, commit once at the end."""
    store = LegacyStore(conn=conn)
    dedupe, neg = reconcile_thresholds()
    with conn.cursor() as c:
        c.execute(
            f"SELECT id, statement, subject_entity, predicate, object, valid_from, "
            f"observed_at, source_turn_ids FROM {SCHEMA}.semantic "
            f"WHERE namespace=%s AND kind='fact' AND valid_to IS NULL AND expired_at IS NULL "
            f"ORDER BY observed_at DESC NULLS LAST, id DESC", (ns,))
        facts = c.fetchall()
    out = {"facts_scanned": len(facts), "deduped": 0, "closed": 0}
    for f in facts:
        if not store.is_live(SCHEMA, ns, f["id"]):
            continue
        d, n = reconcile_fact(store, SCHEMA, ns, f, dedupe_thresh=dedupe, neg_thresh=neg)
        out["deduped"] += d
        out["closed"] += n
    conn.commit()
    return out


def embeddings_for(texts, cache_path):
    cache = {}
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, "rb") as fh:
            cache = pickle.load(fh)
    missing = [t for t in set(texts) if t not in cache]
    if missing:
        t0 = time.time()
        cache.update(ds.embed_texts(missing))
        print(f"  embedded {len(missing)} new texts locally in {time.time() - t0:.1f}s")
        if cache_path:
            with open(cache_path, "wb") as fh:
                pickle.dump(cache, fh)
    return cache


def setup(dsn, n, distractor_ratio, cache_path, tile):
    admin = BrainStore(dsn)
    admin.drop_schema(TENANT)
    admin.create_schema(TENANT, dim=384 * tile)
    spec = ds.build_spec(n, seed=155)
    distractors = [(f"bench:other-{j}", ds.build_spec(n * distractor_ratio // 4, seed=1000 + j))
                   for j in range(4)] if distractor_ratio else []
    texts = [s[0] for s in spec] + [s[0] for _, sp in distractors for s in sp]
    emb = embeddings_for(texts, cache_path)
    t0 = time.time()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        # bulk load without the HNSW index, then build it once (same index definition
        # as core/schema.sql) — incremental HNSW inserts would dominate setup time
        conn.execute(f"DROP INDEX IF EXISTS {SCHEMA}.sem_hnsw")
        for ns in (NS_OLD, NS_NEW):
            ds.seed_namespace(conn, SCHEMA, ns, spec, emb, vtype=admin.vtype, tile=tile)
        for ns, sp in distractors:
            ds.seed_namespace(conn, SCHEMA, ns, sp, emb, vtype=admin.vtype, tile=tile)
        conn.execute(f"CREATE INDEX sem_hnsw ON {SCHEMA}.semantic "
                     f"USING hnsw (embedding {admin.vops})")
        conn.commit()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"VACUUM ANALYZE {SCHEMA}.semantic")
        total = conn.execute(f"SELECT count(*) FROM {SCHEMA}.semantic").fetchone()[0]
    print(f"  seeded {n} target facts x2 + {total - 2 * n} distractors "
          f"(semantic table = {total} rows) in {time.time() - t0:.1f}s")
    admin.conn.close()
    return total


def explain(conn, sql, params):
    with conn.cursor() as c:
        c.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        plan = c.fetchone()
        plan = plan[list(plan.keys())[0]] if isinstance(plan, dict) else plan[0]
    root = plan[0]
    nodes = []

    def walk(p):
        nodes.append(p.get("Node Type") + (f" using {p['Index Name']}" if p.get("Index Name") else "")
                     + (f" on {p['Relation Name']}" if p.get("Relation Name") else ""))
        for ch in p.get("Plans", []):
            walk(ch)
    walk(root["Plan"])
    bufs = root["Plan"].get("Shared Hit Blocks", 0) + root["Plan"].get("Shared Read Blocks", 0)
    return {"ms": root["Execution Time"], "buffers": bufs, "nodes": nodes}


class _RecordingConn:
    """Stands in for a connection so a real BrainStore method can be asked for the exact
    SQL + params it would send (then EXPLAINed separately) — no copy of the new SQL."""

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchall(self):
        return []

    def fetchone(self):
        return None


def shipped_sql(store, method, *args, **kw):
    rec = _RecordingConn()
    getattr(BrainStore(conn=rec, vtype=store.vtype), method)(*args, **kw)
    return rec.sql, rec.params


def bench_size(dsn, n, distractor_ratio, sample, cache_path, full_run, tile):
    print(f"\n=== N = {n} target facts, {384 * tile}-d vectors ===")
    total = setup(dsn, n, distractor_ratio, cache_path, tile)
    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    apply_lookup_settings(conn)          # the session settings reconcile's own conn uses
    store = BrainStore(conn=conn)
    legacy = LegacyStore(conn=conn)
    dedupe_t, _ = reconcile_thresholds()
    page = store.live_facts_page(SCHEMA, NS_NEW, limit=n)
    embs = store.fact_embeddings(SCHEMA, [f["id"] for f in page])
    step = max(1, len(page) // sample)
    anchors = [{**f, "embedding": embs[f["id"]]} for f in page[::step]][:sample]

    # --- EXPLAIN (ANALYZE, BUFFERS): the newest fact with a subject, and the newest
    # WITHOUT one (the worst case — every other fact in the namespace is "older") ------
    a = next(f for f in anchors if f["subject_entity"])
    a0 = next({**f, "embedding": embs[f["id"]]} for f in page if not f["subject_entity"])
    for label, x in (("subject", a), ("no-subj", a0)):
        e_old = explain(conn, LEGACY_DEDUPE_SQL.format(schema=SCHEMA),
                        {"id": x["id"], "ns": NS_NEW, "t": dedupe_t})
        e_new = explain(conn, *shipped_sql(store, "older_near_duplicate", SCHEMA, NS_NEW, x,
                                           dedupe_t))
        print(f"  EXPLAIN dedupe [{label}] OLD: {e_old['ms']:7.1f}ms {e_old['buffers']:7} buffers  "
              f"plan={' > '.join(e_old['nodes'])}")
        print(f"  EXPLAIN dedupe [{label}] NEW: {e_new['ms']:7.1f}ms {e_new['buffers']:7} buffers  "
              f"plan={' > '.join(e_new['nodes'])}")
    from core.service import _REVERSAL_RE
    n_rev = sum(1 for f in page if _REVERSAL_RE.search(f["statement"]))
    print(f"  facts with a reversal cue (the only ones that run the exact negation "
          f"lookup): {n_rev} of {len(page)}")
    n_old = explain(conn, LEGACY_NEAREST_SQL.format(schema=SCHEMA),
                    {"id": a["id"], "ns": NS_NEW, "k": 8})
    n_new = explain(conn, *shipped_sql(store, "nearest_live_facts_to", SCHEMA, NS_NEW, a, k=8))
    print(f"  EXPLAIN negation lookup OLD: {n_old['ms']:7.1f}ms {n_old['buffers']:7} buffers  "
          f"plan={' > '.join(n_old['nodes'])}")
    print(f"  EXPLAIN negation lookup NEW: {n_new['ms']:7.1f}ms {n_new['buffers']:7} buffers  "
          f"plan={' > '.join(n_new['nodes'])}  (exact on purpose)")

    # --- per-fact lookup latency (both lookups a fact can need), sampled -------------
    def timed(fn):
        ts = []
        for x in anchors:
            t0 = time.perf_counter()
            fn(x)
            ts.append((time.perf_counter() - t0) * 1000)
        return statistics.mean(ts), statistics.median(ts)

    for label, s in (("OLD", legacy), ("NEW", store)):   # warm caches for both first
        for x in anchors[:5]:
            s.older_near_duplicate(SCHEMA, NS_NEW, x, dedupe_t)
    old_d = timed(lambda x: legacy.older_near_duplicate(SCHEMA, NS_NEW, x, dedupe_t))
    new_d = timed(lambda x: store.older_near_duplicate(SCHEMA, NS_NEW, x, dedupe_t))
    old_n = timed(lambda x: legacy.nearest_live_facts_to(SCHEMA, NS_NEW, x, k=8))
    new_n = timed(lambda x: store.nearest_live_facts_to(SCHEMA, NS_NEW, x, k=8))
    nulls = [{**f, "embedding": embs[f["id"]]} for f in page if not f["subject_entity"]]
    nulls = nulls[::max(1, len(nulls) // sample)][:sample]
    saved, anchors[:] = list(anchors), nulls
    old_z = timed(lambda x: legacy.older_near_duplicate(SCHEMA, NS_NEW, x, dedupe_t))
    new_z = timed(lambda x: store.older_near_duplicate(SCHEMA, NS_NEW, x, dedupe_t))
    anchors[:] = saved
    print(f"  dedupe (sample) OLD mean {old_d[0]:8.2f}ms p50 {old_d[1]:8.2f}ms | "
          f"NEW mean {new_d[0]:6.2f}ms p50 {new_d[1]:6.2f}ms | {old_d[0] / new_d[0]:6.1f}x")
    print(f"  dedupe no-subj  OLD mean {old_z[0]:8.2f}ms p50 {old_z[1]:8.2f}ms | "
          f"NEW mean {new_z[0]:6.2f}ms p50 {new_z[1]:6.2f}ms | {old_z[0] / new_z[0]:6.1f}x")
    print(f"  negation lookup OLD mean {old_n[0]:8.2f}ms p50 {old_n[1]:8.2f}ms | "
          f"NEW mean {new_n[0]:6.2f}ms p50 {new_n[1]:6.2f}ms | {old_n[0] / new_n[0]:6.1f}x")
    conn.close()
    result = {"n": n, "table_rows": total, "explain_old": e_old, "explain_new": e_new,
              "dedupe_nosubj_old_ms": old_z[0], "dedupe_nosubj_new_ms": new_z[0],
              "explain_nearest_old": n_old, "explain_nearest_new": n_new,
              "dedupe_old_ms": old_d[0], "dedupe_new_ms": new_d[0],
              "nearest_old_ms": old_n[0], "nearest_new_ms": new_n[0]}

    if full_run:
        # --- full end-to-end runs + row-for-row outcome diff ---------------------------
        c_old = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
        t0 = time.perf_counter()
        r_old = legacy_reconcile(c_old, NS_OLD)
        t_old = time.perf_counter() - t0
        c_old.close()
        t0 = time.perf_counter()
        r_new = run_reconcile(dsn, NS_NEW, schema=SCHEMA, install_signal_handlers=False)
        t_new = time.perf_counter() - t0
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            s_old, s_new = ds.snapshot(c, SCHEMA, NS_OLD), ds.snapshot(c, SCHEMA, NS_NEW)
        cmp = ds.compare(s_old, s_new)
        print(f"  FULL RUN  OLD {t_old:8.1f}s ({t_old / n * 1000:7.2f} ms/fact, 1 txn)  "
              f"deduped={r_old['deduped']} closed={r_old['closed']}")
        print(f"  FULL RUN  NEW {t_new:8.1f}s ({t_new / n * 1000:7.2f} ms/fact, "
              f"{r_new['chunks']} chunks)  deduped={r_new['deduped']} closed={r_new['closed']}  "
              f"status={r_new['status']}  -> {t_old / t_new:.1f}x faster")
        print(f"  OUTCOME: {len(cmp['decision_diffs'])} of {cmp['rows']} rows differ in "
              f"close/expire/superseded_by (new left live what old closed: "
              f"{len(cmp['new_did_less'])}; new closed what old left live: "
              f"{len(cmp['new_did_more'])}); {len(cmp['restatement_diffs'])} differ only in "
              f"the restatements counter ({len(cmp['untied_restatement_diffs'])} not "
              f"explained by an exact-distance tie)")
        for k in cmp["decision_diffs"][:10]:
            print(f"     {k}: old={s_old[k]} new={s_new.get(k)}")
        result.update(full_old_s=t_old, full_new_s=t_new, old=r_old, new=r_new,
                      decision_diffs=len(cmp["decision_diffs"]))
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("MEMNOS_DSN"))
    ap.add_argument("--sizes", default="2000,5000")
    ap.add_argument("--distractor-ratio", type=int, default=2,
                    help="distractor facts in OTHER namespaces, as a multiple of N")
    ap.add_argument("--sample", type=int, default=100)
    ap.add_argument("--no-full-run", action="store_true")
    ap.add_argument("--tile", type=int, default=4,
                    help="store each 384-d local embedding repeated N times (4 = 1536-d, "
                         "production width; cosine distances are unchanged)")
    ap.add_argument("--embed-cache", default=os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "memnos_reconcile_bench_emb.pkl"))
    a = ap.parse_args()
    if not a.dsn:
        sys.exit("set MEMNOS_DSN (an isolated throwaway database!) or pass --dsn")
    for n in [int(x) for x in a.sizes.split(",")]:
        bench_size(a.dsn, n, a.distractor_ratio, a.sample, a.embed_cache, not a.no_full_run,
                   a.tile)


if __name__ == "__main__":
    main()
