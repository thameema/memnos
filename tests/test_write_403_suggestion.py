"""Test that write-403 responses include writable namespace suggestions.

Run against a live local server:
    memnos start   (or python memnos_server.py)
    python tests/test_write_403_suggestion.py
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS_A = "test:403-suggest-a"
NS_B = "test:403-suggest-b"
PASS = FAIL = 0


def post(path, token, body):
    req = urllib.request.Request(
        URL + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, detail=""):
    global PASS, FAIL
    label = "PASS" if cond else "FAIL"
    print(f"  {label}  {name}" + (f" -- {detail}" if detail and not cond else ""))
    PASS += cond
    FAIL += not cond


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    pid = Control.create_principal(conn, "test-403-suggestion", "service")
    Control.grant(conn, pid, NS_A, can_read=True, can_write=True)
    tok = Control.mint_token(conn, pid, "403-suggest-test")

    # 1. Write to NS_B (no grant) via /remember -> 403 with writable_namespaces
    s, r = post("/remember", tok, {"namespace": NS_B, "text": "hello world"})
    check("write to forbidden namespace returns 403", s == 403, str(r))
    check("write-403 body contains writable_namespaces", "writable_namespaces" in r, str(r))
    check("writable_namespaces includes NS_A", NS_A in r.get("writable_namespaces", []), str(r))
    check("writable_namespaces does NOT include NS_B", NS_B not in r.get("writable_namespaces", []), str(r))
    check("write-403 body contains hint", "hint" in r, str(r))

    # 2. Read 403 must NOT include writable_namespaces (no info leak on read rejections)
    s, r = post("/recall", tok, {"namespace": NS_B, "query": "hello"})
    check("read to forbidden namespace returns 403", s == 403, str(r))
    check("read-403 does NOT leak writable_namespaces", "writable_namespaces" not in r, str(r))

    # cleanup
    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
        c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
        c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))
    conn.close()

    print(f"\n{'='*50}")
    print(f"  PASS {PASS}  FAIL {FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
