"""No-AI tests for Batch 4: corpus ingestion + corpus_check + corpus_list. Ingests a small
architecture doc, verifies normative constraints (SHALL/MUST/...) are extracted, that
corpus_check surfaces the relevant one for a code snippet, and that re-ingest updates the
source. Pure FTS, no LLM/embeddings.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_corpus_api.py
"""
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
NS = "test:corpusapi"
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

DOC = """
# Data Access LLD
- All database queries MUST go through the ORM layer; direct SQL is PROHIBITED.
- PHI data SHALL be encrypted at rest using AES-256.
- This paragraph is plain narration with no normative keyword, so it is excluded.
- Services SHOULD emit structured logs for every request.
"""


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
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s AND kind='constraint'", (NS,))
        c.execute("DELETE FROM memnos_control.corpus_sources WHERE namespace=%s", (NS,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup(conn)

    admin_id = Control.create_principal(conn, "test-corpus-admin", "service")
    user_id = Control.create_principal(conn, "test-corpus-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)
    ro_id = Control.create_principal(conn, "test-corpus-ro", "agent")
    ro_tok = Control.mint_token(conn, ro_id, "test")
    Control.grant(conn, ro_id, NS, can_read=True, can_write=False)

    print("=== corpus API (Batch 4) ===")
    check("no token -> 401", call("POST", "/corpus/list", None, {"namespace": NS})[0] == 401)
    check("read-only token: ingest -> 403", call("POST", "/corpus/ingest", ro_tok, {"namespace": NS, "name": "x", "text": DOC})[0] == 403)

    # ingest
    s, j = call("POST", "/corpus/ingest", user_tok, {"namespace": NS, "name": "data-access-lld", "text": DOC, "kind": "lld"})
    check("ingest 200", s == 200)
    check("extracted 3 constraints (MUST/SHALL/SHOULD, narration skipped)", j.get("constraints") == 3)

    # list
    s, j = call("POST", "/corpus/list", user_tok, {"namespace": NS})
    check("corpus_list 200", s == 200)
    src = [x for x in j.get("sources", []) if x["name"] == "data-access-lld"]
    check("source listed with count 3", src and src[0]["constraint_count"] == 3 and src[0]["kind"] == "lld")

    # check: a raw-SQL snippet should surface the ORM/SQL constraint
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": "cursor.execute('SELECT * FROM patients')  # direct database query"})
    cons = [c["content"] for c in j.get("constraints", [])]
    check("corpus_check 200", s == 200)
    check("surfaces the ORM/SQL constraint", any("ORM" in c for c in cons))

    # check: an encryption snippet surfaces the AES constraint
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": "store PHI encrypted at rest with AES"})
    check("surfaces the encryption constraint", any("AES-256" in c["content"] for c in j.get("constraints", [])))

    check("corpus_check needs snippet -> 400", call("POST", "/corpus/check", user_tok, {"namespace": NS})[0] == 400)

    # re-ingest updates (not duplicates) the source row
    cleanup_constraints_only = None
    s, j = call("POST", "/corpus/ingest", user_tok, {"namespace": NS, "name": "data-access-lld", "text": DOC, "kind": "lld", "git_sha": "abc123"})
    s, j = call("POST", "/corpus/list", user_tok, {"namespace": NS})
    rows = [x for x in j.get("sources", []) if x["name"] == "data-access-lld"]
    check("re-ingest keeps a single source row", len(rows) == 1 and rows[0]["git_sha"] == "abc123")

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
