"""No-AI governance tests for the management console API. Pure HTTP + control-plane —
no LLM, no embeddings. Run against a live local server:

    python memnos_server.py &        # (or the launchd service)
    python test_admin_api.py

Mints its own admin + test tokens via the control plane, exercises every admin route +
the ACL boundary, then cleans up. Exits non-zero on any failure.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, ".")
import psycopg
from psycopg.rows import dict_row
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_core@localhost:5433/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:admin-api"
PASS = FAIL = 0


def call(method, path, token=None, body=None):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    # bootstrap an admin + a non-admin principal directly (control plane)
    admin_id = Control.create_principal(conn, "test-admin", "service")
    Control.grant(conn, admin_id, "*")
    admin_tok = Control.mint_token(conn, admin_id, "test")
    user_id = Control.create_principal(conn, "test-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")

    print("=== admin console governance tests ===")
    # auth gate
    check("no token -> 401", call("GET", "/admin/api/namespaces")[0] == 401)
    check("non-admin token -> 403", call("GET", "/admin/api/namespaces", user_tok)[0] == 403)
    check("admin token -> 200", call("GET", "/admin/api/namespaces", admin_tok)[0] == 200)

    # namespace lifecycle
    check("create namespace", call("POST", "/admin/api/namespaces", admin_tok, {"name": NS, "description": "t"})[0] == 200)
    s, j = call("GET", "/admin/api/namespaces", admin_tok)
    check("namespace appears in list", any(n["name"] == NS for n in j.get("namespaces", [])))

    # grant the user, verify ACL on the memory API
    call("POST", "/admin/api/grants", admin_tok, {"principal_id": user_id, "namespace": NS})
    check("granted ns: recall 200", call("POST", "/recall", user_tok, {"namespace": NS, "query": "x"})[0] == 200)
    check("ungranted ns: recall 403", call("POST", "/recall", user_tok, {"namespace": "test:nope", "query": "x"})[0] == 403)
    # revoke grant -> now forbidden
    call("DELETE", f"/admin/api/grants?principal={user_id}&namespace={NS}", admin_tok)
    check("revoked grant: recall 403", call("POST", "/recall", user_tok, {"namespace": NS, "query": "x"})[0] == 403)

    # token mint + revoke via API
    s, j = call("POST", "/admin/api/tokens", admin_tok, {"principal_id": user_id, "label": "t2"})
    new_tok = j.get("token", "")
    check("mint token returns plaintext once", new_tok.startswith("mnk_"))
    s, j = call("GET", f"/admin/api/tokens?principal={user_id}", admin_tok)
    tid = next((t["id"] for t in j.get("tokens", []) if t["label"] == "t2"), None)
    check("token listed (metadata only, no secret)", tid is not None and "token" not in (j["tokens"][0]))
    call("POST", "/admin/api/tokens/revoke", admin_tok, {"id": tid})
    check("revoked token -> 401 on use", call("POST", "/recall", new_tok, {"namespace": NS, "query": "x"})[0] == 401)

    # observability shape
    check("provider endpoint shape", set(call("GET", "/admin/api/provider", admin_tok)[1]) >= {"mode", "dim", "key_present"})
    check("health endpoint", "findings" in call("GET", "/admin/api/health", admin_tok)[1])
    check("stats endpoint", "ops" in call("GET", "/admin/api/stats", admin_tok)[1])

    # delete namespace (+purge)
    check("delete namespace", call("DELETE", f"/admin/api/namespaces?name={NS}&purge=1", admin_tok)[0] == 200)
    s, j = call("GET", "/admin/api/namespaces", admin_tok)
    check("namespace gone from list", not any(n["name"] == NS for n in j.get("namespaces", [])))

    # cleanup principals/tokens created by the test
    with conn.cursor() as c:
        for pid in (admin_id, user_id):
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
        c.execute("DELETE FROM memnos_control.principals WHERE name IN ('test-admin','test-user')")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
