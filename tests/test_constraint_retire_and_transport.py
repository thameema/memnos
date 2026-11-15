"""issue #152 (standalone constraint retirement + pinned listing) and issue #39
(agent-setup / claude-setup default --transport http). Needs Postgres, no LLM:

    MEMNOS_DSN=postgresql://memnos:memnos@localhost:5432/memnos python tests/test_constraint_retire_and_transport.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
NS = "test:cret:a"
NS2 = "test:cret:b"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def cli(*args, home):
    env = dict(os.environ, MEMNOS_DSN=DSN, HOME=home)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "memnos_cli.py"), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, r.stdout + r.stderr


def cleanup(conn):
    with conn.cursor() as c:
        for ns in (NS, NS2):
            for t in ("semantic", "episodic", "raw_turns"):
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.audit_log WHERE namespace=%s", (ns,))
        c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals p "
                  "WHERE t.principal_id=p.id AND p.name='cret-admin'")
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals p "
                  "WHERE g.principal_id=p.id AND p.name='cret-admin'")
        c.execute("DELETE FROM memnos_control.principals WHERE name='cret-admin'")


def live(conn, tbl, i):
    with conn.cursor() as c:
        c.execute(f"SELECT constraint_retired_at, constraint_retired_by FROM {SCHEMA}.{tbl} WHERE id=%s", (i,))
        r = c.fetchone()
    return r["constraint_retired_at"] is None, r["constraint_retired_by"]


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)
    now = datetime.now(timezone.utc)
    mk = lambda ns, text, subj: store.insert_raw_turn(SCHEMA, ns, None, "user", text, now, None,
                                                      memory_type="constraint", constraint_subject=subj)

    print("=== BrainStore.retire_constraint_by_id ===")
    t1, t2, t3 = mk(NS, "never force push", "push"), mk(NS, "always sign commits", "sign"), mk(NS, "no tabs", "tabs")
    r = store.retire_constraint_by_id(SCHEMA, NS, t1, retired_by="t")
    check("int id retires the raw_turn", [(x["kind"], x["id"]) for x in r] == [("turn", t1)]
          and r[0]["content"] == "never force push" and r[0]["constraint_subject"] == "push")
    check("row marked retired_by", live(conn, "raw_turns", t1) == (False, "t"))
    check("second retire is a no-op", store.retire_constraint_by_id(SCHEMA, NS, t1) == [])
    r = store.retire_constraint_by_id(SCHEMA, NS, f"turn:{t2}")
    check("'turn:<id>' string retires", [x["id"] for x in r] == [t2])
    r = store.retire_constraint_by_id(SCHEMA, NS, str(t3))
    check("digit string retires", [x["id"] for x in r] == [t3])
    fid = store.insert_semantic(SCHEMA, NS, "fact", "fact rule", memory_type="constraint", observed_at=now)
    r = store.retire_constraint_by_id(SCHEMA, NS, f"fact:{fid}")
    check("'fact:<id>' retires the semantic row", [(x["kind"], x["id"]) for x in r] == [("fact", fid)])
    t4 = mk(NS, "kind-scoped", "ks")
    check("wrong kind does not touch the turn", store.retire_constraint_by_id(SCHEMA, NS, f"fact:{t4}") == []
          and live(conn, "raw_turns", t4)[0])
    check("wrong namespace does not retire", store.retire_constraint_by_id(SCHEMA, NS2, t4) == []
          and live(conn, "raw_turns", t4)[0])
    plain = store.insert_raw_turn(SCHEMA, NS, None, "user", "just a note", now, None)
    check("non-constraint rows are never retired", store.retire_constraint_by_id(SCHEMA, NS, plain) == [])
    for bad in ("nope", "bogus:1"):
        try:
            store.retire_constraint_by_id(SCHEMA, NS, bad); ok = False
        except ValueError:
            ok = True
        check(f"bad id {bad!r} -> ValueError", ok)
    check("list_pinned_constraints shows only live", [x["id"] for x in store.list_pinned_constraints(SCHEMA, NS)] == [t4])

    print("=== CLI: constraint pins / retire ===")
    home = tempfile.mkdtemp()
    a, b = mk(NS2, "rule A", "subj-a"), mk(NS2, "rule B", "subj-b")
    rc, out = cli("constraint", "pins", NS2, home=home)
    check("pins lists kind:id + subject", rc == 0 and f"turn:{a}" in out and "subject=subj-a" in out and f"turn:{b}" in out)
    rc, out = cli("constraint", "retire", f"turn:{a}", "--namespace", NS2, home=home)
    check("retire kind:id", rc == 0 and f"retired turn:{a}" in out and not live(conn, "raw_turns", a)[0])
    rc, out = cli("constraint", "retire", f"turn:{a}", "--namespace", NS2, home=home)
    check("retire again -> nonzero", rc != 0 and "no live pinned constraint" in out)
    rc, out = cli("constraint", "retire", "--subject", " SUBJ-B ", "--namespace", NS2, home=home)
    check("retire --subject (normalized)", rc == 0 and not live(conn, "raw_turns", b)[0])
    rc, out = cli("constraint", "pins", NS2, home=home)
    check("pins empty afterwards", rc == 0 and "no live pinned constraints" in out)
    with conn.cursor() as c:
        c.execute("SELECT count(*) AS n FROM memnos_control.audit_log WHERE action='constraint.retire' AND namespace=%s", (NS2,))
        check("constraint.retire audited twice", c.fetchone()["n"] == 2)
    rc, out = cli("constraint", "retire", "--namespace", NS2, home=home)
    check("retire needs target or --subject", rc != 0)

    print("=== /memory/delete retires a raw-turn constraint (was 404) ===")
    admin = Control.create_principal(conn, "cret-admin", "service")
    Control.grant(conn, admin, "*")
    tok = Control.mint_token(conn, admin, "t")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "memnos_server.py")],
                           env=dict(os.environ, MEMNOS_DSN=DSN, MEMNOS_PORT=str(port), HOME=home),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def call(body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/memory/delete", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
        try:
            r = urllib.request.urlopen(req, timeout=25)
            return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except OSError:
            return None, {}
    try:
        rt = mk(NS, "raw-turn rule", "rt")
        fc = store.insert_semantic(SCHEMA, NS, "fact", "semantic rule", memory_type="constraint", observed_at=now)
        s = None
        for _ in range(60):
            s, j = call({"namespace": NS, "id": rt})
            if s is not None:
                break
            time.sleep(1)
        check("raw-turn constraint -> 200 (not 404)", s == 200 and j.get("statement") == "raw-turn rule"
              and j["constraints_retired"][0]["kind"] == "turn")
        check("raw_turn row retired by memory_delete:<principal>", live(conn, "raw_turns", rt)[1].startswith("memory_delete:"))
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM memnos_control.audit_log WHERE action='constraint.retire' AND namespace=%s "
                      "AND detail->>'by'='memory_delete'", (NS,))
            check("audited constraint.retire", c.fetchone() is not None)
        check("same id again -> 404", call({"namespace": NS, "id": rt})[0] == 404)
        s, j = call({"namespace": NS, "constraint_id": fc})
        check("semantic constraint: 200 + expired", s == 200 and j["deleted"] == fc)
        check("semantic constraint also retired", live(conn, "semantic", fc) == (False, "memory_delete"))
    finally:
        srv.terminate(); srv.wait(timeout=20)

    print("=== #39: transport defaults ===")
    import memnos_cli
    p = memnos_cli.build_parser()
    check("agent-setup defaults to http", p.parse_args(["agent-setup", "claude-code"]).transport == "http")
    check("claude-setup defaults to http", p.parse_args(["claude-setup"]).transport == "http")
    check("agent-setup --transport stdio opt-in", p.parse_args(["agent-setup", "claude-code", "--transport", "stdio"]).transport == "stdio")
    check("claude-setup --transport stdio opt-in", p.parse_args(["claude-setup", "--transport", "stdio"]).transport == "stdio")
    h = tempfile.mkdtemp()
    os.makedirs(os.path.join(h, ".claude"))
    rc, out = cli("agent-setup", "claude-code", "--force", home=h)
    mcp = json.load(open(os.path.join(h, ".claude.json")))["mcpServers"]["memnos"]
    check("default claude-code wiring is http", rc == 0 and mcp.get("type") == "http" and "/mcp" in mcp["url"])
    h = tempfile.mkdtemp()
    os.makedirs(os.path.join(h, ".claude"))
    rc, out = cli("agent-setup", "claude-code", "--force", "--transport", "stdio", home=h)
    mcp = json.load(open(os.path.join(h, ".claude.json")))["mcpServers"]["memnos"]
    check("explicit stdio wiring keeps a command", rc == 0 and mcp.get("command") and mcp.get("type") != "http")
    h = tempfile.mkdtemp()
    rc, out = cli("agent-setup", "codex", "--force", home=h)
    check("default http does NOT break non-HTTP agents (codex falls back)", rc == 0)
    rc, out = cli("agent-setup", "codex", "--force", "--transport", "http", home=h)
    check("explicit http on codex still errors", rc != 0 and "only supported" in out)

    cleanup(conn)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
