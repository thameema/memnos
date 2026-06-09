"""No-AI tests for file ingest: a document's text is chunked and each chunk stored as a
searchable memory, then recall finds it. Tests the text path, the base64 path, chunking,
and ACL. No LLM (extract defaults off; chunks stored as raw turns).

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_ingest_api.py
"""
import base64
import json
import os
import sys
import urllib.request

sys.path.insert(0, ".")
import psycopg
from psycopg.rows import dict_row
from memnos_brain.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_core@localhost:5433/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:ingest"
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

DOC = """# Onboarding Notes

The mascot of the data team is a creature named Zorblax, used in examples.

Deployments go out on Tuesdays after the on-call handoff and a green CI run.

All database access must go through the repository layer, never raw connections.

Secrets live in the encrypted vault and are referenced, never inlined in code.
"""


def call(method, path, token=None, body=None):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def count_turns(conn, fname):
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) n FROM {SCHEMA}.raw_turns WHERE namespace=%s AND session_id=%s", (NS, fname))
        return c.fetchone()["n"]


def cleanup(conn):
    with conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup(conn)

    user_id = Control.create_principal(conn, "test-ingest-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)
    ro_id = Control.create_principal(conn, "test-ingest-ro", "agent")
    ro_tok = Control.mint_token(conn, ro_id, "test")
    Control.grant(conn, ro_id, NS, can_read=True, can_write=False)

    print("=== file ingest ===")
    check("no token -> 401", call("POST", "/ingest/file", None, {"namespace": NS, "filename": "x.md", "text": DOC})[0] == 401)
    check("read-only token -> 403", call("POST", "/ingest/file", ro_tok, {"namespace": NS, "filename": "x.md", "text": DOC})[0] == 403)
    check("empty -> 400", call("POST", "/ingest/file", user_tok, {"namespace": NS, "filename": "x.md", "text": "  "})[0] == 400)

    # text path with small chunk size -> multiple chunks
    s, j = call("POST", "/ingest/file", user_tok, {"namespace": NS, "filename": "onboarding.md", "text": DOC, "chunk_size": 120})
    check("ingest 200", s == 200)
    check("chunked into >1 pieces", j.get("chunks", 0) > 1)
    check("turn_ids match chunk count", len(j.get("turn_ids", [])) == j.get("chunks"))
    check("chunks persisted as raw_turns", count_turns(conn, "onboarding.md") == j["chunks"])

    # ingested content is searchable
    s, j = call("POST", "/recall", user_tok, {"namespace": NS, "query": "Zorblax mascot"})
    check("recall finds ingested content", s == 200 and any("Zorblax" in m.get("content", "") for m in j.get("memories", [])))

    # short text -> single chunk
    s, j = call("POST", "/ingest/file", user_tok, {"namespace": NS, "filename": "tiny.txt", "text": "just one line"})
    check("short doc -> 1 chunk", j.get("chunks") == 1)

    # base64 path (decode utf-8 text)
    b64 = base64.b64encode("Hello from base64.\n\nA second paragraph here.".encode()).decode()
    s, j = call("POST", "/ingest/file", user_tok, {"namespace": NS, "filename": "note.txt", "content_b64": b64})
    check("base64 path 200 + chunks", s == 200 and j.get("chunks", 0) >= 1)

    cleanup(conn)
    for pid in (user_id, ro_id):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
