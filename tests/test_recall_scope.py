"""Test that /recall responses include a scope metadata block (issue #16 Part 1).

Run against a live local server:
    memnos start   (or python memnos_server.py)
    python tests/test_recall_scope.py
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
NS_A = "test:recall-scope-a"
NS_B = "test:recall-scope-b"
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

    # create a principal with READ access to both NS_A and NS_B
    pid = Control.create_principal(conn, "test-recall-scope", "service")
    Control.grant(conn, pid, NS_A, can_read=True, can_write=True)
    Control.grant(conn, pid, NS_B, can_read=True, can_write=True)
    tok = Control.mint_token(conn, pid, "recall-scope-test")

    # seed NS_B with a memory so it shows up as a readable namespace with data
    s, r = post("/remember", tok, {"namespace": NS_B, "text": "scope test seed memory in NS_B"})
    check("seed NS_B succeeds", s == 200, str(r))

    # recall from NS_A (no data there, but the scope block should still be present)
    s, r = post("/recall", tok, {"namespace": NS_A, "query": "anything"})
    check("recall from NS_A returns 200", s == 200, str(r))
    check("response contains scope block", "scope" in r, str(r))

    scope = r.get("scope", {})
    check("scope.query_namespace == NS_A", scope.get("query_namespace") == NS_A, str(scope))
    check("scope.namespaces_searched is a list", isinstance(scope.get("namespaces_searched"), list), str(scope))
    check("NS_A is in namespaces_searched",
          NS_A in scope.get("namespaces_searched", []), str(scope))
    check("scope.facts_found is an int", isinstance(scope.get("facts_found"), int), str(scope))
    check("NS_B appears in other_readable_namespaces",
          NS_B in scope.get("other_readable_namespaces", []), str(scope))
    check("NS_A does NOT appear in other_readable_namespaces",
          NS_A not in scope.get("other_readable_namespaces", []), str(scope))
    check("hint is present when other_readable_namespaces is non-empty",
          "hint" in scope, str(scope))
    check("hint contains NS_A (query namespace)",
          NS_A in scope.get("hint", ""), str(scope))
    check("hint contains NS_B (other readable)",
          NS_B in scope.get("hint", ""), str(scope))

    # when there are results, facts_found should match
    s2, r2 = post("/recall", tok, {"namespace": NS_B, "query": "scope test seed"})
    check("recall from NS_B returns 200", s2 == 200, str(r2))
    scope2 = r2.get("scope", {})
    check("facts_found reflects actual result count",
          scope2.get("facts_found") == len(r2.get("memories", [])), str(scope2))
    check("NS_B NOT in other_readable for NS_B recall",
          NS_B not in scope2.get("other_readable_namespaces", []), str(scope2))

    # /memory/search should NOT have a scope block (scope is /recall only)
    s3, r3 = post("/memory/search", tok, {"namespace": NS_A, "query": "anything"})
    check("/memory/search returns 200", s3 == 200, str(r3))
    check("/memory/search does NOT have scope block", "scope" not in r3, str(r3))

    # cleanup
    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace IN (%s,%s)", (NS_A, NS_B))
        c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
        c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
        c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))
    conn.close()

    print(f"\n{'='*50}")
    print(f"  PASS {PASS}  FAIL {FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
