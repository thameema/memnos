"""No-AI tests for the memory-layer parity API (Batch 1): graph read (get_entity,
get_related, graph_query), memory CRUD parity (memory_search, memory_write,
memory_delete) and the context endpoint. Pure HTTP + control-plane + direct graph
seeding — no LLM, no embeddings required (graph reads are pure SQL).

Run against a live local server:
    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_memory_api.py

Seeds a small graph (Ada—Acme, Ada—Bob) in a throwaway namespace, exercises every new
endpoint + the ACL boundary, then cleans up. Exits non-zero on any failure.
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
NS = "test:memapi"
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
    store.create_schema("memnos")           # idempotent; tables already exist
    cleanup(conn)                            # in case a prior run left rows

    # --- seed a tiny knowledge graph directly (no LLM/embeddings) ---
    ada = store.upsert_entity(SCHEMA, NS, "Ada")
    bob = store.upsert_entity(SCHEMA, NS, "Bob")
    acme = store.upsert_entity(SCHEMA, NS, "Acme")
    store.bump_edge(SCHEMA, NS, ada, acme, 3.0)
    store.bump_edge(SCHEMA, NS, ada, bob, 1.0)
    s1 = store.insert_semantic(SCHEMA, NS, "proposition", "Ada works at Acme",
                               subject="Ada", predicate="works_at", obj="Acme",
                               valid_from="2026-01-01")
    s2 = store.insert_semantic(SCHEMA, NS, "proposition", "Ada knows Bob",
                               subject="Ada", predicate="knows", obj="Bob",
                               valid_from="2026-02-01")
    store.add_mention(SCHEMA, ada, s1, "semantic"); store.add_mention(SCHEMA, acme, s1, "semantic")
    store.add_mention(SCHEMA, ada, s2, "semantic"); store.add_mention(SCHEMA, bob, s2, "semantic")

    # --- principals/tokens/grants ---
    admin_id = Control.create_principal(conn, "test-memapi-admin", "service")
    Control.grant(conn, admin_id, "*")
    admin_tok = Control.mint_token(conn, admin_id, "test")
    user_id = Control.create_principal(conn, "test-memapi-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)

    print("=== memory-layer parity API (Batch 1) ===")

    # ACL boundary
    check("no token -> 401", call("POST", "/entity", None, {"namespace": NS, "name": "Ada"})[0] == 401)
    check("ungranted ns -> 403", call("POST", "/entity", user_tok, {"namespace": "test:nope", "name": "Ada"})[0] == 403)

    # get_entity
    s, j = call("POST", "/entity", user_tok, {"namespace": NS, "name": "Ada"})
    check("get_entity 200", s == 200)
    check("get_entity returns Ada", j.get("entity", {}).get("name") == "Ada")
    rel = {r["name"] for r in j.get("related", [])}
    check("get_entity related has Acme+Bob", {"Acme", "Bob"} <= rel)
    check("get_entity Acme weight highest", j["related"][0]["name"] == "Acme")
    facts = {f["content"] for f in j.get("facts", [])}
    check("get_entity facts include both", {"Ada works at Acme", "Ada knows Bob"} <= facts)
    check("get_entity case-insensitive", call("POST", "/entity", user_tok, {"namespace": NS, "name": "ada"})[1].get("entity", {}).get("name") == "Ada")
    check("get_entity unknown -> 404", call("POST", "/entity", user_tok, {"namespace": NS, "name": "Nobody"})[0] == 404)

    # get_related
    s, j = call("POST", "/related", user_tok, {"namespace": NS, "name": "Ada"})
    check("get_related 200 + neighbours", s == 200 and {r["name"] for r in j["related"]} == {"Acme", "Bob"})

    # graph_query (N-hop expand -> facts)
    s, j = call("POST", "/graph", user_tok, {"namespace": NS, "entities": ["Ada"], "hops": 2})
    check("graph_query returns facts", s == 200 and len(j.get("facts", [])) >= 2)
    check("graph_query needs entities -> 400", call("POST", "/graph", user_tok, {"namespace": NS})[0] == 400)

    # memory_search / context (server computes embeddings; just assert shape)
    s, j = call("POST", "/memory/search", user_tok, {"namespace": NS, "query": "Ada"})
    check("memory_search 200 + list", s == 200 and isinstance(j.get("memories"), list))
    s, j = call("POST", "/memory/context", user_tok, {"namespace": NS, "query": "Ada"})
    check("memory/context 200 + str", s == 200 and isinstance(j.get("context"), str))

    # memory_delete (expire by id) — read grant not enough; needs write (user has both)
    s, j = call("POST", "/memory/delete", user_tok, {"namespace": NS, "id": s1})
    check("memory_delete 200", s == 200 and j.get("deleted") == s1)
    s, j = call("POST", "/entity", user_tok, {"namespace": NS, "name": "Ada"})
    check("deleted fact gone from entity", "Ada works at Acme" not in {f["content"] for f in j.get("facts", [])})
    check("memory_delete unknown id -> 404", call("POST", "/memory/delete", user_tok, {"namespace": NS, "id": 999999999})[0] == 404)

    # memory_write (alias remember) — works even with no LLM (stores raw turn)
    s, j = call("POST", "/memory/write", user_tok, {"namespace": NS, "text": "Ada was promoted to staff engineer."})
    check("memory_write 200 + turn_id", s == 200 and j.get("turn_id"))

    # write-op ACL: a read-only token cannot delete
    ro_id = Control.create_principal(conn, "test-memapi-ro", "agent")
    ro_tok = Control.mint_token(conn, ro_id, "test")
    Control.grant(conn, ro_id, NS, can_read=True, can_write=False)
    check("read-only token: memory_delete -> 403", call("POST", "/memory/delete", ro_tok, {"namespace": NS, "id": s2})[0] == 403)
    check("read-only token: get_entity 200", call("POST", "/entity", ro_tok, {"namespace": NS, "name": "Ada"})[0] == 200)

    # --- cleanup ---
    cleanup(conn)
    for pid in (admin_id, user_id, ro_id):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
