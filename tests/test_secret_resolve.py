"""Tests for POST /secret/resolve — the authenticated HTTP secret-resolve endpoint +
its audit trail (issue #114, "Secret Shield"). A sibling to /corpus/check: authorized
via pseudo-namespace grants (`secret:NAME` / `secret:*`) through the EXISTING grants
table/CLI, NOT the blanket `/admin/api/*` `'*'`-admin check that guards
GET/POST/DELETE /admin/api/secrets.

Covers (per the issue's acceptance criteria):
  - authorized success (exact `secret:NAME` grant, broad `secret:*` grant, and an
    existing `'*'`-admin principal retaining access without any narrower grant)
  - unauthorized rejection (no grant at all) -> 403, plaintext never leaked
  - nonexistent-name handling (authorized, but no such secret) -> 404
  - audit-log entries written for every outcome (success / forbidden / not-found),
    each carrying the secret NAME but never the plaintext VALUE
  - malformed requests (missing name, name containing '*') -> 400
  - regression guard: GET/POST/DELETE /admin/api/secrets never return plaintext,
    even substring-searched in the raw response body
  - the `secret:` pseudo-namespace prefix filter: a secret-resolve grant must never
    surface as a real (browsable/prunable) memory namespace via
    Control.list_namespaces() / Control.namespace_prune_candidates()

Seeds the test secret over the server's own HTTP admin route (not an in-process
Vault.set) so the value is always encrypted with the SERVER process's
MEMNOS_SECRET_KEY — the same key /secret/resolve will decrypt with — even when this
test process's own environment differs (e.g. run outside the server's env/container).

Run against a live local server (same harness as the rest of tests/):
    MEMNOS_DSN=... MEMNOS_URL=... MEMNOS_SECRET_KEY=... python tests/test_secret_resolve.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control, SECRET_NS_PREFIX

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
PASS = FAIL = 0
RUN = str(int(time.time() * 1000))     # uniquify names so audit assertions can't be fooled by old runs


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def call(method, path, token=None, body=None):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def call_json(method, path, token=None, body=None):
    status, raw = call(method, path, token, body)
    try:
        return status, json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return status, {}


def _audit_max_id(conn):
    with conn.cursor() as c:
        c.execute("SELECT COALESCE(max(id), 0) AS m FROM memnos_control.audit_log")
        return c.fetchone()["m"]


def _audit_rows_since(conn, since_id, action, namespace):
    with conn.cursor() as c:
        c.execute("SELECT id, ok, detail FROM memnos_control.audit_log "
                  "WHERE id > %s AND action=%s AND namespace=%s ORDER BY id", (since_id, action, namespace))
        return c.fetchall()


def cleanup(conn, principal_ids, secret_name):
    with conn.cursor() as c:
        for pid in principal_ids:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))
    from core.vault import Vault
    Vault.delete(conn, secret_name)


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    admin_id = Control.create_principal(conn, f"test_sr_admin_{RUN}", "service")
    Control.grant(conn, admin_id, "*")
    admin_tok = Control.mint_token(conn, admin_id, "test-secret-resolve-admin")

    # Ask the SERVER (not this process's local env) whether its vault is unlocked —
    # MEMNOS_SECRET_KEY could differ between this test process and the server process.
    st, sec = call_json("GET", "/admin/api/secrets", admin_tok)
    if st != 200 or not sec.get("unlocked"):
        print("SKIP — server vault is locked (server process has no MEMNOS_SECRET_KEY)")
        sys.exit(0)

    secret_name = f"test_sr_secret_{RUN}"
    secret_value = f"super-secret-value-{RUN}"
    principal_ids = [admin_id]

    try:
        # Seed over the server's own HTTP admin route so the ciphertext is always
        # encrypted with the SERVER's key.
        st, _ = call_json("POST", "/admin/api/secrets", admin_tok,
                          {"name": secret_name, "value": secret_value, "description": "issue #114 test"})
        check("seed secret stored via HTTP admin route", st == 200)

        exact_id = Control.create_principal(conn, f"test_sr_exact_{RUN}", "agent")
        principal_ids.append(exact_id)
        Control.grant(conn, exact_id, f"{SECRET_NS_PREFIX}{secret_name}")   # exact pseudo-namespace grant
        exact_tok = Control.mint_token(conn, exact_id, "test-secret-resolve-exact")

        wildcard_id = Control.create_principal(conn, f"test_sr_wild_{RUN}", "agent")
        principal_ids.append(wildcard_id)
        Control.grant(conn, wildcard_id, f"{SECRET_NS_PREFIX}*")            # broad pseudo-namespace grant
        wildcard_tok = Control.mint_token(conn, wildcard_id, "test-secret-resolve-wild")

        noaccess_id = Control.create_principal(conn, f"test_sr_none_{RUN}", "agent")
        principal_ids.append(noaccess_id)
        Control.grant(conn, noaccess_id, "some:other:ns")                   # unrelated real namespace only
        noaccess_tok = Control.mint_token(conn, noaccess_id, "test-secret-resolve-none")

        pseudo_ns = f"{SECRET_NS_PREFIX}{secret_name}"
        action = "secret/resolve"

        print("=== authorized success ===")
        baseline = _audit_max_id(conn)
        st, out = call_json("POST", "/secret/resolve", exact_tok, {"name": secret_name})
        check("exact grant -> 200", st == 200)
        check("returns the correct plaintext value", out.get("value") == secret_value)
        check("response echoes the name", out.get("name") == secret_name)
        rows = _audit_rows_since(conn, baseline, action, pseudo_ns)
        check("audit row written for success", len(rows) >= 1)
        check("success audit row ok=True", rows and all(r["ok"] for r in rows))
        check("success audit detail carries name, not the plaintext value",
              all(r["detail"].get("name") == secret_name for r in rows) and
              all(secret_value not in json.dumps(r["detail"]) for r in rows))

        st, out = call_json("POST", "/secret/resolve", wildcard_tok, {"name": secret_name})
        check("wildcard secret:* grant -> 200", st == 200)
        check("wildcard grant returns the correct plaintext value", out.get("value") == secret_value)

        st, out = call_json("POST", "/secret/resolve", admin_tok, {"name": secret_name})
        check("admin '*' grant retains resolve access without a narrower grant "
              "(issue #114 design note: narrower grants are additive, not a revocation) -> 200", st == 200)
        check("admin resolve returns the correct plaintext value", out.get("value") == secret_value)

        print("=== unauthorized rejection ===")
        baseline = _audit_max_id(conn)
        st, raw = call("POST", "/secret/resolve", noaccess_tok, {"name": secret_name})
        check("no grant -> 403", st == 403)
        check("plaintext not leaked in a 403 body", secret_value.encode() not in raw)
        rows = _audit_rows_since(conn, baseline, action, pseudo_ns)
        check("audit row written for forbidden", len(rows) >= 1)
        check("forbidden audit row ok=False", rows and not any(r["ok"] for r in rows))
        check("forbidden audit detail carries name, not the plaintext value",
              all(r["detail"].get("name") == secret_name for r in rows) and
              all(secret_value not in json.dumps(r["detail"]) for r in rows))

        st, _ = call_json("POST", "/secret/resolve", None, {"name": secret_name})
        check("no token -> 401", st == 401)

        print("=== nonexistent-name handling ===")
        missing_name = f"test_sr_missing_{RUN}"
        missing_ns = f"{SECRET_NS_PREFIX}{missing_name}"
        Control.grant(conn, exact_id, missing_ns)   # authorize the miss too — isolates 404 from 403
        baseline = _audit_max_id(conn)
        st, raw = call("POST", "/secret/resolve", exact_tok, {"name": missing_name})
        check("authorized + nonexistent name -> 404", st == 404)
        rows = _audit_rows_since(conn, baseline, action, missing_ns)
        check("audit row written for not-found", len(rows) >= 1)
        check("not-found audit row ok=False", rows and not any(r["ok"] for r in rows))
        Control.revoke_grant(conn, exact_id, missing_ns)

        print("=== malformed requests ===")
        st, _ = call_json("POST", "/secret/resolve", admin_tok, {})
        check("missing name -> 400", st == 400)
        st, _ = call_json("POST", "/secret/resolve", admin_tok, {"name": "*"})
        check("name == '*' -> 400 (would collide with the secret:* wildcard grant string)", st == 400)
        st, _ = call_json("POST", "/secret/resolve", admin_tok, {"name": secret_name + "*"})
        check("name containing '*' -> 400", st == 400)

        print("=== regression: /admin/api/secrets never returns plaintext ===")
        st, raw = call("GET", "/admin/api/secrets", admin_tok)
        check("GET /admin/api/secrets -> 200", st == 200)
        check("GET /admin/api/secrets body does not contain the plaintext value (substring scan)",
              secret_value.encode() not in raw)
        check("GET /admin/api/secrets body DOES list the secret's name (not a vacuous pass)",
              secret_name.encode() in raw)
        other_val = f"other-plaintext-{RUN}"
        st, raw = call("POST", "/admin/api/secrets", admin_tok,
                       {"name": secret_name, "value": other_val, "description": "overwrite check"})
        check("POST /admin/api/secrets -> 200", st == 200)
        check("POST /admin/api/secrets response does not echo the plaintext value back",
              other_val.encode() not in raw)
        # DELETE takes ?name= as a query param, not a body
        st, raw = call("DELETE", f"/admin/api/secrets?name={secret_name}", admin_tok, None)
        check("DELETE /admin/api/secrets -> 200", st == 200)
        check("DELETE /admin/api/secrets response does not contain the plaintext value",
              secret_value.encode() not in raw and other_val.encode() not in raw)
        # resolve after delete must now 404 (authorized, but the secret is gone)
        st, _ = call_json("POST", "/secret/resolve", exact_tok, {"name": secret_name})
        check("resolve after delete -> 404", st == 404)

        print("=== pseudo-namespace does not masquerade as a real memory namespace ===")
        # re-seed so there's a live secret:NAME grant to check the census queries against
        Control.grant(conn, exact_id, pseudo_ns)   # (re-)ensure the grant exists independent of the delete above
        listed = [n["name"] for n in Control.list_namespaces(conn)]
        check("list_namespaces() excludes the secret: pseudo-namespace",
              pseudo_ns not in listed, f"found {pseudo_ns!r} in list_namespaces()")
        pruned = [r["name"] for r in Control.namespace_prune_candidates(conn, empty=True)]
        check("namespace_prune_candidates(empty=True) excludes the secret: pseudo-namespace "
              "(prevents `memnos namespace prune` from silently revoking a secret grant)",
              pseudo_ns not in pruned, f"found {pseudo_ns!r} in namespace_prune_candidates()")

    finally:
        cleanup(conn, principal_ids, secret_name)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
