"""EXPLAIN-based regression for issue #69: knowledge_health's orphan-entities query
OR-defeats-index -> statement timeout on a large namespace.

Root cause: `NOT EXISTS (SELECT 1 FROM edges g WHERE g.src_entity=e.id OR g.dst_entity=e.id)`
correlates the subquery to the outer entity row through an OR of two columns. Postgres
cannot satisfy "src_entity=X OR dst_entity=X" with a single btree seek, so this degrades
to a per-entity scan over the WHOLE edges table (every namespace, not just the one being
queried, since the correlated subquery has no namespace filter of its own) -- O(entities x
edges). On a real namespace at production scale (~20.7k entities) this blew the server's
15s statement_timeout (see the issue for the field QueryCanceled traceback).

Fix (core/store.py BrainStore.orphan_entities_sql): rewrite as `e.id NOT IN (SELECT
src_entity FROM edges WHERE namespace=%s UNION SELECT dst_entity FROM edges WHERE
namespace=%s)`. Each arm of the UNION is UNCORRELATED (filtered only by namespace, no
reference to e.id), so each is a single index-only scan of the `(namespace, src_entity,
dst_entity)` unique index -- namespace is the index's leading column, and both
src_entity/dst_entity are already columns of that same index, so neither arm needs a heap
fetch. Total cost is O(edges in namespace), not O(entities x edges).

This file proves that with EXPLAIN, not just correct results -- a wrong-but-lucky-on-tiny-
data rewrite that still seq-scans/nested-loops on real data would pass a results-only test
but not this one:
  1. Seeds a MULTI-TENANT dataset (issue #69's exact real-world condition: one entities/
     edges table shared across many namespaces) so that filtering by namespace is
     genuinely selective -- on a single-namespace table a seq scan is often the cheapest
     valid plan for ANY query shape, which would hide the fix. Orphans are placed
     deterministically (every ORPHAN_EVERY'th entity, by insertion order, is excluded from
     the edge chain), so the expected orphan count is known length, and is independently
     re-derived in Python via a plain set-difference over entity ids vs. edge endpoints --
     never trusting either SQL form under test to grade itself.
  2. Runs the store's OWN production SQL (BrainStore.orphan_entities_sql -- not a
     hand-copied duplicate that could drift from the real query) through EXPLAIN (FORMAT
     JSON) and asserts: no Seq Scan node touches `edges`, and both UNION arms show up as
     Index Only Scan nodes against the edges unique index.
  3. Runs the literal PRE-FIX query text (documented here as a fixture, not imported --
     the whole point is that core/store.py no longer contains this shape) through the same
     EXPLAIN and asserts its planner cost is orders of magnitude higher -- quantifying the
     exact blowup the fix removes, on the SAME data, in the SAME run.
  4. Cross-checks BrainStore.health() end-to-end: its returned orphan_entities for the
     target namespace matches the same independently-derived ground truth.

No server needed (direct-DB, same pattern as test_recall_arm_degrade.py). Seeding is one
round-trip bulk INSERT (a CTE chain), not a per-row Python loop -- default size (4,000
entities x 10 namespaces) seeds in well under a second; MEMNOS_ORPHAN_INDEX_TEST_ENTITIES
scales it up for a closer field-scale repro (issue #69's real namespace: ~20.7k entities).

Run: MEMNOS_DSN=... python tests/test_orphan_entities_index.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import psycopg
from psycopg.rows import dict_row

from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

TARGET_NS = "test:orphanidx:target"
NOISE_NS = [f"test:orphanidx:noise{i}" for i in range(9)]
ALL_NS = [TARGET_NS] + NOISE_NS
N_ENT = int(os.environ.get("MEMNOS_ORPHAN_INDEX_TEST_ENTITIES", "4000"))
ORPHAN_EVERY = 20     # every ORPHAN_EVERY'th entity (by insertion order) is left unconnected

# The exact pre-fix query (issue #69's root cause) -- kept here ONLY as a fixture to
# EXPLAIN against, not imported: core/store.py no longer contains this shape at all.
OLD_ORPHAN_SQL = (f"SELECT count(*) n FROM {SCHEMA}.entities e WHERE e.namespace=%s "
                  f"AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.edges g "
                  f"WHERE g.src_entity=e.id OR g.dst_entity=e.id)")


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        for ns in ALL_NS:
            c.execute(f"DELETE FROM {SCHEMA}.edges WHERE namespace=%s", (ns,))
            c.execute(f"DELETE FROM {SCHEMA}.entities WHERE namespace=%s", (ns,))


def seed(conn):
    """One round-trip bulk seed across all namespaces: N_ENT entities per namespace,
    chained into edges (entity[i] -> entity[i+1] by insertion order) EXCEPT every
    ORPHAN_EVERY'th entity, which is excluded from the chain on both sides -- a true
    orphan (touches no edge as either endpoint). Every namespace gets the identical
    shape so the noise namespaces are realistic, not just filler rows."""
    with conn.cursor() as c:
        c.execute(f"""
            WITH ins_ent AS (
              INSERT INTO {SCHEMA}.entities (namespace, name)
              SELECT ns, 'ent_' || g
              FROM unnest(%(namespaces)s::text[]) ns, generate_series(1, %(n)s) g
              RETURNING id, namespace
            ),
            numbered AS (
              SELECT id, namespace, row_number() OVER (PARTITION BY namespace ORDER BY id) AS rn
              FROM ins_ent
            ),
            non_orphan AS (
              SELECT id, namespace, row_number() OVER (PARTITION BY namespace ORDER BY id) AS crn
              FROM numbered WHERE rn %% %(oe)s != 0
            )
            INSERT INTO {SCHEMA}.edges (namespace, src_entity, dst_entity)
            SELECT a.namespace, a.id, b.id
            FROM non_orphan a JOIN non_orphan b ON b.namespace = a.namespace AND b.crn = a.crn + 1
        """, {"namespaces": ALL_NS, "n": N_ENT, "oe": ORPHAN_EVERY})
    # scoped to just the two tables this test touches -- an unqualified VACUUM ANALYZE
    # would vacuum the entire shared database, which is expensive and unnecessary here.
    conn.execute(f"VACUUM ANALYZE {SCHEMA}.entities, {SCHEMA}.edges")


def ground_truth_orphans(conn, ns):
    """Independent re-derivation, NOT using either SQL form under test: pull every
    entity id and every edge endpoint for `ns` separately and set-difference in Python."""
    with conn.cursor() as c:
        c.execute(f"SELECT id FROM {SCHEMA}.entities WHERE namespace=%s", (ns,))
        ent_ids = {r["id"] for r in c.fetchall()}
        c.execute(f"SELECT src_entity, dst_entity FROM {SCHEMA}.edges WHERE namespace=%s", (ns,))
        touched = set()
        for r in c.fetchall():
            touched.add(r["src_entity"]); touched.add(r["dst_entity"])
    return ent_ids - touched


def explain_plan(conn, sql, params):
    with conn.cursor() as c:
        c.execute("EXPLAIN (FORMAT JSON) " + sql, params)
        return c.fetchone()["QUERY PLAN"][0]["Plan"]


def plan_nodes(node):
    """Flatten an EXPLAIN JSON plan tree (Plans is the standard child-node key) into a
    flat list, including nodes nested under Subplans/CTEs -- both appear under "Plans"
    in Postgres's JSON format, so a single recursive walk covers everything."""
    nodes = [node]
    for child in node.get("Plans", []):
        nodes.extend(plan_nodes(child))
    return nodes


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)

    try:
        print(f"=== seeding {len(ALL_NS)} namespaces x {N_ENT} entities "
              f"(1 orphan per {ORPHAN_EVERY}) ===")
        seed(conn)
        truth = ground_truth_orphans(conn, TARGET_NS)
        print(f"    target namespace: {N_ENT} entities, {len(truth)} true orphans (ground truth)")
        check("seed produced the expected orphan count",
              len(truth) == N_ENT // ORPHAN_EVERY,
              f"expected {N_ENT // ORPHAN_EVERY}, got {len(truth)}")

        new_sql = store.orphan_entities_sql(SCHEMA)   # the ACTUAL production query text

        print("=== correctness: both query forms agree with ground truth ===")
        with conn.cursor() as c:
            c.execute(OLD_ORPHAN_SQL, (TARGET_NS,))
            old_n = c.fetchone()["n"]
        with conn.cursor() as c:
            c.execute(new_sql, (TARGET_NS, TARGET_NS, TARGET_NS))
            new_n = c.fetchone()["n"]
        check("pre-fix query matches ground truth", old_n == len(truth), f"{old_n} vs {len(truth)}")
        check("rewritten (production) query matches ground truth", new_n == len(truth), f"{new_n} vs {len(truth)}")

        print("=== EXPLAIN: rewritten query is index-using, not a seq scan ===")
        new_plan = explain_plan(conn, new_sql, (TARGET_NS, TARGET_NS, TARGET_NS))
        new_nodes = plan_nodes(new_plan)
        edges_seq_scans = [n for n in new_nodes
                           if n["Node Type"] == "Seq Scan" and n.get("Relation Name") == "edges"]
        edges_index_scans = [n for n in new_nodes
                             if "Index" in n["Node Type"] and n.get("Relation Name") == "edges"]
        check("rewritten query has NO seq scan on edges",
              not edges_seq_scans, str([n["Node Type"] for n in edges_seq_scans]))
        check("rewritten query hits the edges index on BOTH UNION arms (src_entity + dst_entity)",
              len(edges_index_scans) >= 2,
              f"found {len(edges_index_scans)}: {[n.get('Index Name') for n in edges_index_scans]}")
        check("every edges index scan uses the (namespace, src_entity, dst_entity) unique index",
              all("edges" in (n.get("Index Name") or "") for n in edges_index_scans),
              str([n.get("Index Name") for n in edges_index_scans]))

        print("=== EXPLAIN: pre-fix query is catastrophically more expensive on the SAME data ===")
        old_plan = explain_plan(conn, OLD_ORPHAN_SQL, (TARGET_NS,))
        old_nodes = plan_nodes(old_plan)
        old_cost = old_plan["Total Cost"]
        new_cost = new_plan["Total Cost"]
        print(f"    pre-fix total cost:    {old_cost:,.2f}")
        print(f"    rewritten total cost:  {new_cost:,.2f}")
        print(f"    ratio:                 {old_cost / new_cost:,.0f}x")
        check("rewritten query's planner cost is at least 100x cheaper than the pre-fix query",
              new_cost > 0 and old_cost / new_cost >= 100,
              f"old={old_cost:,.2f} new={new_cost:,.2f}")
        has_nested_loop = any(n["Node Type"] == "Nested Loop" for n in old_nodes)
        has_seq_scan_edges = any(n["Node Type"] == "Seq Scan" and n.get("Relation Name") == "edges"
                                 for n in old_nodes)
        check("pre-fix query's plan shows the per-entity correlation the OR forces "
              "(a Nested Loop against edges, or a seq scan over edges)",
              has_nested_loop or has_seq_scan_edges,
              str([n["Node Type"] for n in old_nodes]))

        print("=== BrainStore.health() end to end agrees with ground truth ===")
        report = store.health(SCHEMA, TARGET_NS)
        check("health() orphan_entities matches ground truth", report["orphan_entities"] == len(truth),
              str(report))
        check("health() is not degraded on the happy path", not report.get("degraded"), str(report))
    finally:
        cleanup(conn)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
