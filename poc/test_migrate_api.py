"""No-AI tests for namespace copy/move. Seeds a source namespace with raw turns + facts,
copies a filtered subset into a destination (graph rebuilt, source untouched), moves a whole
namespace (source emptied), and checks the read-source/write-dest ACL. No LLM.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_migrate_api.py
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, ".")
import psycopg
from psycopg.rows import dict_row
from memnos_brain.control import Control
from memnos_brain.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_poc@localhost:5433/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"
SRC, DST, DST2 = "test:src", "test:dst", "test:dst2"
PASS = FAIL = 0


def call(path, token=None, body=None, method="POST"):
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


def turns(conn, ns):
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) n FROM {SCHEMA}.raw_turns WHERE namespace=%s", (ns,))
        return c.fetchone()["n"]


def cleanup(conn):
    with conn.cursor() as c:
        for ns in (SRC, DST, DST2):
            c.execute(f"DELETE FROM {SCHEMA}.mentions m USING {SCHEMA}.entities e WHERE m.entity_id=e.id AND e.namespace=%s", (ns,))
            for t in ("edges", "semantic", "entities", "raw_turns"):
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (ns,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)

    # seed SRC: 2 TAP memories + 2 unrelated
    from datetime import datetime, timezone
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.insert_raw_turn(SCHEMA, SRC, None, "user", "TAP scraper runs nightly via cron.", now, None)
    store.insert_raw_turn(SCHEMA, SRC, None, "user", "Unrelated note about cats.", now, None)
    store.insert_semantic(SCHEMA, SRC, "fact", "TAP uses OpenAI for extraction",
                          subject="TAP", predicate="uses", obj="OpenAI")
    store.insert_semantic(SCHEMA, SRC, "fact", "Cats are fluffy", subject="Cats", predicate="are", obj="fluffy")

    # principals
    full = Control.create_principal(conn, "mig-full", "agent"); full_tok = Control.mint_token(conn, full, "t")
    Control.grant(conn, full, SRC); Control.grant(conn, full, DST); Control.grant(conn, full, DST2)
    wonly = Control.create_principal(conn, "mig-wonly", "agent"); wonly_tok = Control.mint_token(conn, wonly, "t")
    Control.grant(conn, wonly, DST, can_read=True, can_write=True)   # no grant on SRC
    ronly = Control.create_principal(conn, "mig-ronly", "agent"); ronly_tok = Control.mint_token(conn, ronly, "t")
    Control.grant(conn, ronly, SRC, can_read=True, can_write=False)  # no write on DST

    print("=== namespace copy / move ===")
    # ACL
    check("no read on source -> 403", call("/namespace/copy", wonly_tok, {"namespace": DST, "src": SRC})[0] == 403)
    check("no write on dest -> 403", call("/namespace/copy", ronly_tok, {"namespace": DST, "src": SRC})[0] == 403)
    check("src==dst -> 400", call("/namespace/copy", full_tok, {"namespace": SRC, "src": SRC})[0] == 400)
    check("missing src -> 400", call("/namespace/copy", full_tok, {"namespace": DST})[0] == 400)

    # COPY with a 'like' filter -> only TAP rows, source untouched
    s, j = call("/namespace/copy", full_tok, {"namespace": DST, "src": SRC, "like": "TAP"})
    check("copy 200", s == 200 and j.get("mode") == "copy")
    check("copied only matching (1 turn, 1 fact)", j.get("raw_turns") == 1 and j.get("facts") == 1)
    check("source still intact (2 turns)", turns(conn, SRC) == 2)
    check("dest has the TAP turn", turns(conn, DST) == 1)
    # graph rebuilt in dest
    s, j = call("/entity", full_tok, {"namespace": DST, "name": "TAP"})
    check("dest graph rebuilt (entity TAP)", s == 200 and j.get("entity", {}).get("name") == "TAP")
    s, j = call("/recall", full_tok, {"namespace": DST, "query": "TAP scraper cron"})
    check("dest recall finds copied memory", s == 200 and any("scraper" in m.get("content", "") for m in j.get("memories", [])))

    # MOVE whole namespace -> source emptied
    s, j = call("/namespace/copy", full_tok, {"namespace": DST2, "src": SRC, "mode": "move"})
    check("move 200", s == 200 and j.get("mode") == "move")
    check("move relocated all (2 turns, 2 facts)", j.get("raw_turns") == 2 and j.get("facts") == 2)
    check("source emptied after move", turns(conn, SRC) == 0)
    check("dest2 has everything", turns(conn, DST2) == 2)
    s, j = call("/entity", full_tok, {"namespace": DST2, "name": "Cats"})
    check("moved graph present in dest2", s == 200 and j.get("entity", {}).get("name") == "Cats")

    cleanup(conn)
    for pid in (full, wonly, ronly):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
