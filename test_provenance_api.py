"""No-AI tests for provenance wiring: every semantic fact records the raw_turn(s) it was
extracted from, exposed via /provenance ("why do you believe this?"). Verifies the store
column, the REST endpoint, AND that the production _write_fact path threads provenance
(exercised directly with a null embedder, no LLM).

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_provenance_api.py
"""
import json
import os
import sys
from datetime import datetime, timezone

import urllib.request

sys.path.insert(0, ".")
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_core@localhost:5433/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:prov"
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
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.mentions m USING {SCHEMA}.entities e "
                  f"WHERE m.entity_id=e.id AND e.namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.entities WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # seed a raw turn + a fact that points at it
    tid = store.insert_raw_turn(SCHEMA, NS, None, "user", "Ada joined Acme as a staff engineer.", now, None)
    fid = store.insert_semantic(SCHEMA, NS, "fact", "Ada works at Acme",
                                subject="Ada", predicate="works_at", obj="Acme",
                                valid_from=now, source_turn_ids=[tid])
    fid_nosrc = store.insert_semantic(SCHEMA, NS, "fact", "Sky is blue", subject="Sky")

    user_id = Control.create_principal(conn, "test-prov-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)

    print("=== provenance (evidence chain) ===")
    check("no token -> 401", call("POST", "/provenance", None, {"namespace": NS, "id": fid})[0] == 401)
    check("ungranted ns -> 403", call("POST", "/provenance", user_tok, {"namespace": "test:nope", "id": fid})[0] == 403)

    s, j = call("POST", "/provenance", user_tok, {"namespace": NS, "id": fid})
    check("provenance 200", s == 200)
    check("returns the fact", j.get("fact", {}).get("statement") == "Ada works at Acme")
    check("source_turn_ids includes the turn", tid in (j.get("source_turn_ids") or []))
    srcs = j.get("sources", [])
    check("evidence chain has the verbatim turn", any(sx["id"] == tid and "staff engineer" in sx["content"] for sx in srcs))

    s, j = call("POST", "/provenance", user_tok, {"namespace": NS, "id": fid_nosrc})
    check("fact without source -> empty evidence", s == 200 and not j.get("sources"))

    check("unknown id -> 404", call("POST", "/provenance", user_tok, {"namespace": NS, "id": 999999999})[0] == 404)
    check("id required -> 400", call("POST", "/provenance", user_tok, {"namespace": NS})[0] == 400)

    # production path: _write_fact must thread provenance (no LLM — null embedder)
    mem = MemnosMemory(store, lambda t: None, llm=None)
    tid2 = store.insert_raw_turn(SCHEMA, NS, None, "user", "Bob prefers Go for backends.", now, None)
    mem._write_fact(NS, {"statement": "Bob prefers Go", "subject": "Bob",
                         "predicate": "prefers", "object": "Go"}, now, source_turn_ids=[tid2])
    with conn.cursor() as c:
        c.execute(f"SELECT id FROM {SCHEMA}.semantic WHERE namespace=%s AND statement=%s", (NS, "Bob prefers Go"))
        bob_fid = c.fetchone()["id"]
    prov = store.provenance_of(SCHEMA, NS, bob_fid)
    check("_write_fact threads provenance", tid2 in (prov.get("source_turn_ids") or []))
    check("evidence resolves to verbatim turn", any("backends" in sx["content"] for sx in prov.get("sources", [])))

    cleanup(conn)
    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (user_id,))
        c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (user_id,))
        c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (user_id,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
