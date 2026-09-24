"""No-AI tests for Batch 2: community_search, contradiction detection, knowledge health.
Seeds a graph with a connected component, an orphan entity, and a deliberate
subject+predicate contradiction, then exercises the endpoints. Pure SQL features, no LLM.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_knowledge_api.py
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:knowapi"
BIG_NS = "test:knowapi-big"     # issue #158: large sparse component
SCHEMA = "tenant_memnos"
PASS = FAIL = 0


def call(method, path, token=None, body=None):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        for ns in (NS, BIG_NS):
            c.execute(f"SELECT id FROM {SCHEMA}.entities WHERE namespace=%s", (ns,))
            eids = [r["id"] for r in c.fetchall()]
            if eids:
                c.execute(f"DELETE FROM {SCHEMA}.mentions WHERE entity_id = ANY(%s)", (eids,))
            for t in ("edges", "semantic", "entities"):
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (ns,))


N_BIG = 20000


def seed_big_component(conn, store):
    """issue #158 regression graph, shaped like the live one: a seed entity inside one
    huge sparse connected component (> 40K nodes), glued together by a hub literal.
      Seed -> Zed(5), Mid(3), Alpha(1)   weight order inverts alphabetical order
      Seed -> '✅'(10)                   symbol-only junk entity, strongest edge
      Seed -> user(1) -> spoke_0..spoke_19999   hub (degree 20001), must not be expanded
      Seed -> chain_0(0.5) -> chain_1 -> ... -> chain_19999   long sparse chain
      Mid  -> Deep(4)                    a genuine 2-hop neighbour
    Bulk-built with generate_series (20K+ round-trips would dominate test time)."""
    names = ["Seed", "Zed", "Mid", "Alpha", "\u2705", "user", "Deep"]
    ids = {n: store.upsert_entity(SCHEMA, BIG_NS, n) for n in names}
    for a, b, w in (("Seed", "Zed", 5.0), ("Seed", "Mid", 3.0), ("Seed", "Alpha", 1.0),
                    ("Seed", "\u2705", 10.0), ("Seed", "user", 1.0), ("Mid", "Deep", 4.0)):
        store.bump_edge(SCHEMA, BIG_NS, ids[a], ids[b], w)
    with conn.cursor() as c:
        c.execute(f"""INSERT INTO {SCHEMA}.entities(namespace, name)
                      SELECT %(ns)s, p || g FROM generate_series(0, %(n)s - 1) g,
                             unnest(ARRAY['spoke_', 'chain_']) p""", {"ns": BIG_NS, "n": N_BIG})
        c.execute(f"""INSERT INTO {SCHEMA}.edges(namespace, src_entity, dst_entity, weight)
                      SELECT %(ns)s, %(hub)s, e.id, 1.0 FROM {SCHEMA}.entities e
                      WHERE e.namespace=%(ns)s AND e.name LIKE 'spoke\\_%%'""",
                  {"ns": BIG_NS, "hub": ids["user"]})
        c.execute(f"""INSERT INTO {SCHEMA}.edges(namespace, src_entity, dst_entity, weight)
                      SELECT %(ns)s, a.id, b.id, 1.0
                      FROM generate_series(0, %(n)s - 2) g
                      JOIN {SCHEMA}.entities a ON a.namespace=%(ns)s AND a.name = 'chain_' || g
                      JOIN {SCHEMA}.entities b ON b.namespace=%(ns)s AND b.name = 'chain_' || (g + 1)""",
                  {"ns": BIG_NS, "n": N_BIG})
        c.execute(f"""INSERT INTO {SCHEMA}.edges(namespace, src_entity, dst_entity, weight)
                      SELECT %(ns)s, %(seed)s, id, 0.5 FROM {SCHEMA}.entities
                      WHERE namespace=%(ns)s AND name='chain_0'""", {"ns": BIG_NS, "seed": ids["Seed"]})
        c.execute(f"ANALYZE {SCHEMA}.edges")
        c.execute(f"SELECT count(*) AS n FROM {SCHEMA}.edges WHERE namespace=%s", (BIG_NS,))
        return c.fetchone()["n"]


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)

    # connected component: Ada-Acme-Bob
    ada = store.upsert_entity(SCHEMA, NS, "Ada")
    bob = store.upsert_entity(SCHEMA, NS, "Bob")
    acme = store.upsert_entity(SCHEMA, NS, "Acme")
    zara = store.upsert_entity(SCHEMA, NS, "Zara")     # orphan (no edges)
    store.bump_edge(SCHEMA, NS, ada, acme, 2.0)
    store.bump_edge(SCHEMA, NS, acme, bob, 1.0)        # Bob reachable via Acme (2 hops)
    # deliberate contradiction: two current lives_in values
    store.insert_semantic(SCHEMA, NS, "proposition", "Ada lives in Austin",
                          subject="Ada", predicate="lives_in", obj="Austin", valid_from="2026-01-01")
    store.insert_semantic(SCHEMA, NS, "proposition", "Ada lives in Seattle",
                          subject="Ada", predicate="lives_in", obj="Seattle", valid_from="2026-03-01")
    # a cleanly superseded fact (not a contradiction)
    sid = store.insert_semantic(SCHEMA, NS, "proposition", "Ada uses Vim",
                                subject="Ada", predicate="uses", obj="Vim", valid_from="2025-01-01")
    with conn.cursor() as c:
        c.execute(f"UPDATE {SCHEMA}.semantic SET valid_to='2026-01-01' WHERE id=%s", (sid,))

    admin_id = Control.create_principal(conn, "test-know-admin", "service")
    Control.grant(conn, admin_id, "*")
    user_id = Control.create_principal(conn, "test-know-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)

    print("=== knowledge API (Batch 2) ===")
    check("no token -> 401", call("POST", "/community", None, {"namespace": NS, "name": "Ada"})[0] == 401)
    check("ungranted ns -> 403", call("POST", "/community", user_tok, {"namespace": "test:nope", "name": "Ada"})[0] == 403)

    # community
    s, j = call("POST", "/community", user_tok, {"namespace": NS, "name": "Ada"})
    check("community 200", s == 200)
    check("community has Acme + Bob (connected component)", {"Acme", "Bob"} <= set(j.get("community", [])))
    check("community excludes orphan Zara", "Zara" not in j.get("community", []))
    check("community unknown -> 404", call("POST", "/community", user_tok, {"namespace": NS, "name": "Nobody"})[0] == 404)
    # good case stays intact: small, well-connected component, ranked by hop then weight
    check("community ranked: Acme (hop 1) before Bob (hop 2)", j.get("community", [])[:2] == ["Acme", "Bob"])
    check("community members carry hop + weight",
          [(m["name"], m["hop"]) for m in j.get("members", [])][:2] == [("Acme", 1), ("Bob", 2)])
    check("community limit=1 honoured",
          len(call("POST", "/community", user_tok, {"namespace": NS, "name": "Ada", "limit": 1})[1].get("community", [])) == 1)
    check("community bad limit -> 400",
          call("POST", "/community", user_tok, {"namespace": NS, "name": "Ada", "limit": "x"})[0] == 400)

    # --- issue #158: large sparse component must be bounded, fast, and ranked ---
    n_edges = seed_big_component(conn, store)
    check(f"big component built ({n_edges} edges)", n_edges >= 2 * N_BIG)
    Control.grant(conn, user_id, BIG_NS, can_read=True, can_write=True)
    best = None
    for _ in range(2):                       # best of 2: dodge a cold buffer cache
        t0 = time.perf_counter()
        res = store.community(SCHEMA, BIG_NS, "Seed", max_nodes=20)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    print(f"    store.community on {n_edges}-edge component: {best*1000:.1f} ms")
    check("big community < 1s", best < 1.0)
    got = res["community"]
    check("big community bounded by limit (<= 20)", len(got) <= 20)
    check("big community ranked by weight, not alphabet (Zed, Mid first)", got[:2] == ["Zed", "Mid"])
    check("weight tie broken by recency, not name (user edge newer than Alpha's)",
          got[2:4] == ["user", "Alpha"])
    check("hub is a direct member", "user" in got)
    check("hub not expanded through (no spoke_*)", not any(n.startswith("spoke_") for n in got))
    check("symbol-only junk entity filtered", "\u2705" not in got)
    check("2-hop neighbour Deep present, after all hop-1 members",
          "Deep" in got and all(m["hop"] == 1 for m in res["members"][:got.index("Deep")]))
    check("chain not walked beyond 2 hops", not ({"chain_2", "chain_3"} & set(got)))
    t0 = time.perf_counter()
    s, j = call("POST", "/community", user_tok, {"namespace": BIG_NS, "name": "Seed"})
    dt = time.perf_counter() - t0
    check(f"HTTP big community 200 in < 2s ({dt*1000:.0f} ms)", s == 200 and dt < 2.0)
    check("HTTP big community default cap (<= 50)", len(j.get("community", [])) <= 50)

    # contradictions
    s, j = call("POST", "/contradictions", user_tok, {"namespace": NS})
    check("contradictions 200", s == 200)
    cons = j.get("contradictions", [])
    lives = [c for c in cons if c["subject"] == "Ada" and c["predicate"] == "lives_in"]
    check("contradiction detected for Ada/lives_in", bool(lives))
    check("both objects present", lives and {"Austin", "Seattle"} <= set(lives[0]["objects"]))
    check("superseded fact NOT a contradiction", not any(c["predicate"] == "uses" for c in cons))

    # knowledge health
    s, j = call("POST", "/knowledge/health", user_tok, {"namespace": NS})
    check("health 200", s == 200)
    check("score reduced (<100)", j.get("score", 100) < 100)
    check("contradiction_groups >= 1", j.get("contradiction_groups", 0) >= 1)
    check("orphan_entities >= 1 (Zara)", j.get("orphan_entities", 0) >= 1)
    check("facts_superseded >= 1", j.get("facts_superseded", 0) >= 1)

    cleanup(conn)
    for pid in (admin_id, user_id):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
