"""No-AI tests for GROUNDED RECALL (0.1.6) — knowledge namespaces + namespace links.
A link X->K is POLICY ("recall on X should also search K"); the caller's READ GRANT on K
is PERMISSION. Both are required: linked+granted namespaces contribute results (tagged
with their source namespace, surfaced in `grounded_in`); linked-but-ungranted namespaces
are skipped VISIBLY (`links_skipped`). No links => responses unchanged (regression).
Also covers the admin link CRUD API and the `memnos namespace set/link/links/unlink` CLI.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_grounded_recall.py
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NSX, K1, K2, NOL = "test:gx", "test:gk1", "test:gk2", "test:gnol"
PASS = FAIL = 0


def call(path, token=None, body=None, method="POST"):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def cli(*args):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "memnos_cli.py"), *args],
                       capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, MEMNOS_DSN=DSN))
    return r.returncode, (r.stdout + r.stderr)


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        for ns in (NSX, K1, K2, NOL):
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (ns,))
            c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.namespace_links WHERE src_ns=%s OR dst_ns=%s",
                      (ns, ns))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (ns,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # distinctive shared FTS token 'quibblet' in each namespace
    store.insert_raw_turn(SCHEMA, NSX, None, "user", "primary quibblet note about Xanadu", now, None)
    store.insert_raw_turn(SCHEMA, K1, None, "user", "knowledge quibblet doctrine of Kappa", now, None)
    store.insert_raw_turn(SCHEMA, K2, None, "user", "restricted quibblet secret of Zeta", now, None)
    store.insert_raw_turn(SCHEMA, NOL, None, "user", "lonely quibblet island of Theta", now, None)

    user_id = Control.create_principal(conn, "gr-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "t")
    Control.grant(conn, user_id, NSX, can_read=True, can_write=True)
    Control.grant(conn, user_id, K1, can_read=True, can_write=False)
    Control.grant(conn, user_id, NOL, can_read=True, can_write=False)
    # NOTE: NOT granted K2 — its link must be skipped (and visibly reported)
    admin_id = Control.create_principal(conn, "gr-admin", "user")
    admin_tok = Control.mint_token(conn, admin_id, "t")
    Control.grant(conn, admin_id, "*", can_read=True, can_write=True)

    print("=== namespace kind (control plane + CLI) ===")
    rc, out = cli("namespace", "set", K1, "--kind", "knowledge")
    check("CLI: namespace set --kind knowledge", rc == 0 and "knowledge" in out)
    kinds = {n["name"]: n["kind"] for n in Control.list_namespaces(conn)}
    check("kind recorded in registry", kinds.get(K1) == "knowledge")
    check("unset namespaces default to 'memory'", kinds.get(NOL, "memory") == "memory")

    print("=== link CRUD (CLI + admin API) ===")
    rc, out = cli("namespace", "link", NSX, K1)
    check("CLI: namespace link", rc == 0 and "linked" in out)
    s, j = call("/admin/api/namespaces/links", admin_tok, {"src": NSX, "dst": K2})
    check("admin API: POST link", s == 200 and j.get("ok"))
    s, j = call(f"/admin/api/namespaces/links?ns={NSX}", admin_tok, method="GET")
    dsts = {l["dst_ns"] for l in j.get("links", [])}
    check("admin API: GET lists both links", s == 200 and dsts == {K1, K2})
    rc, out = cli("namespace", "links", NSX)
    check("CLI: namespace links lists them", rc == 0 and K1 in out and K2 in out)
    s, j = call("/admin/api/namespaces/links", user_tok, {"src": NSX, "dst": NOL})
    check("link CRUD is admin-only (403 for non-admin)", s == 403)
    s, j = call("/admin/api/namespaces/links", admin_tok, {"src": NSX, "dst": NSX})
    check("self-link rejected", s == 400)

    print("=== grounded recall (link=policy AND grant=permission) ===")
    s, j = call("/recall", user_tok, {"namespace": NSX, "query": "quibblet"})
    check("recall 200", s == 200)
    check("grounded_in = the linked+granted namespace", j.get("grounded_in") == [K1])
    check("links_skipped = the linked-but-UNGRANTED namespace", j.get("links_skipped") == [K2])
    mems = j.get("memories", [])
    txt = " ".join(m.get("content", "") for m in mems)
    check("primary namespace content present", "Xanadu" in txt)
    check("grounded knowledge content present", "Kappa" in txt)
    check("ungranted linked content EXCLUDED", "Zeta" not in txt)
    k1rows = [m for m in mems if "Kappa" in m["content"]]
    check("grounded rows tagged with source namespace",
          k1rows and all(m.get("namespace") == K1 for m in k1rows))
    xrows = [m for m in mems if "Xanadu" in m["content"]]
    check("primary rows stay untagged", xrows and all("namespace" not in m for m in xrows))
    check("context tags the knowledge namespace", f"[{K1}]" in j.get("context", ""))

    # granting K2 turns its skip into a ground
    Control.grant(conn, user_id, K2, can_read=True, can_write=False)
    s, j = call("/recall", user_tok, {"namespace": NSX, "query": "quibblet"})
    check("after granting K2 it grounds too", sorted(j.get("grounded_in", [])) == [K1, K2])
    check("no more skipped links", j.get("links_skipped") == [])
    check("K2 content now present", "Zeta" in " ".join(m.get("content", "") for m in j.get("memories", [])))

    print("=== no links => behavior unchanged (regression) ===")
    s, j = call("/recall", user_tok, {"namespace": NOL, "query": "quibblet"})
    check("recall on unlinked namespace 200", s == 200)
    check("no grounded_in / links_skipped keys", "grounded_in" not in j and "links_skipped" not in j)
    check("scoped to its own namespace only",
          "Theta" in " ".join(m.get("content", "") for m in j.get("memories", []))
          and "Kappa" not in " ".join(m.get("content", "") for m in j.get("memories", [])))

    print("=== unlink ===")
    rc, out = cli("namespace", "unlink", NSX, K1)
    check("CLI: namespace unlink", rc == 0 and "unlinked" in out)
    s, j = call(f"/admin/api/namespaces/links?src={NSX}&dst={K2}", admin_tok, method="DELETE")
    check("admin API: DELETE link", s == 200 and j.get("removed") is True)
    s, j = call("/recall", user_tok, {"namespace": NSX, "query": "quibblet"})
    check("all links removed => plain recall again", "grounded_in" not in j
          and "Kappa" not in " ".join(m.get("content", "") for m in j.get("memories", [])))

    cleanup(conn)
    with conn.cursor() as c:
        for pid in (user_id, admin_id):
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
