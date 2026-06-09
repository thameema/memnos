"""No-AI tests for Batch 2: community_search, contradiction detection, knowledge health.
Seeds a graph with a connected component, an orphan entity, and a deliberate
subject+predicate contradiction, then exercises the endpoints. Pure SQL features, no LLM.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_knowledge_api.py
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:knowapi"
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
        c.execute(f"SELECT id FROM {SCHEMA}.entities WHERE namespace=%s", (NS,))
        eids = [r["id"] for r in c.fetchall()]
        if eids:
            c.execute(f"DELETE FROM {SCHEMA}.mentions WHERE entity_id = ANY(%s)", (eids,))
        for t in ("edges", "semantic", "entities"):
            c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (NS,))


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
