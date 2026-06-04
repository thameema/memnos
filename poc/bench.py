"""POC-5: latency at production scale.

Seeds N rows (1536-d halfvec + text + entity mentions) into one tenant/namespace,
builds HNSW, and times the full hybrid query (vector + FTS + 1-hop graph + RRF,
one round-trip) over M runs. Reports p50/p95/p99 vs the <200ms budget.
"""
import sys
import time

sys.path.insert(0, ".")
import psycopg
from memnos_poc.embedder import embed
from memnos_poc.storage import PgStorage

DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"
SCHEMA = "tenant_bench"
NS = "bench:ns"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 50000
M = int(sys.argv[2]) if len(sys.argv) > 2 else 50


def seed():
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as c:
        c.execute("DROP SCHEMA IF EXISTS tenant_bench CASCADE")
        c.execute("SELECT create_tenant_schema('bench')")
        t0 = time.perf_counter()
        # per-row distinct random vectors (g reference defeats InitPlan caching)
        c.execute(f"""
            INSERT INTO {SCHEMA}.memory(namespace, content, embedding)
            SELECT %s,
                   'document ' || g || ' acme merger m&a team project alpha ' || (g %% 200),
                   (SELECT ('[' || string_agg(random()::text, ',') || ']')::halfvec
                    FROM generate_series(1,1536) s WHERE g IS NOT NULL)
            FROM generate_series(1, %s) g
        """, (NS, N))
        c.execute(f"INSERT INTO {SCHEMA}.entity(namespace,name) "
                  f"SELECT %s, 'ent'||i FROM generate_series(1,200) i", (NS,))
        c.execute(f"INSERT INTO {SCHEMA}.mentions(memory_id, entity_id) "
                  f"SELECT m.id, ((m.id % 200) + 1) FROM {SCHEMA}.memory m")
        ins = time.perf_counter() - t0
        # distinctness sanity check
        c.execute(f"SELECT count(DISTINCT left(embedding::text,40)) d FROM "
                  f"(SELECT embedding FROM {SCHEMA}.memory LIMIT 500) x")
        distinct = c.fetchone()[0]
        c.execute(f"ANALYZE {SCHEMA}.memory")
    conn.close()
    print(f"seeded N={N:,} rows in {ins:.1f}s  (distinct vectors in 500-sample: {distinct})")


def bench():
    s = PgStorage(DSN)
    qvec = embed("acme merger m&a team project alpha 42")
    qtext = "acme merger team project"
    # warm-up
    for _ in range(3):
        s.hybrid_search(SCHEMA, NS, qtext, qvec, k=20, top_k=10)
    lat = []
    for _ in range(M):
        t = time.perf_counter()
        s.hybrid_search(SCHEMA, NS, qtext, qvec, k=20, top_k=10)
        lat.append((time.perf_counter() - t) * 1000)
    lat.sort()
    p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]
    print(f"\nhybrid query latency over {M} runs (N={N:,}):")
    print(f"  p50 = {p(0.50):6.1f} ms")
    print(f"  p95 = {p(0.95):6.1f} ms")
    print(f"  p99 = {p(0.99):6.1f} ms")
    print(f"  min = {lat[0]:6.1f} ms   max = {lat[-1]:6.1f} ms")
    budget = 200
    print(f"\n  budget <{budget}ms  →  {'PASS ✅' if p(0.95) < budget else 'FAIL ❌'} (p95)")


if __name__ == "__main__":
    seed()
    bench()
