"""No-AI tests for /reconcile — checking an external claim (e.g. a local note) against
memnos to flag stale local memory when memnos holds a different current value. The agent
supplies the parsed subject/predicate; memnos does the deterministic check. No LLM.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_reconcile_api.py
"""
import json
import os
import sys
from datetime import datetime, timezone

import urllib.request

sys.path.insert(0, ".")
import psycopg
from psycopg.rows import dict_row
from memnos_brain.control import Control
from memnos_brain.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_poc@localhost:5433/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:reconcile"
SCHEMA = "tenant_memnos"
PASS = FAIL = 0


def call(path, token=None, body=None):
    req = urllib.request.Request(URL + path, method="POST",
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


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)

    # memnos holds the NEWER truth: TAP now uses its own component (not the paid plugin)
    store.insert_semantic(SCHEMA, NS, "fact", "TAP uses com_tia, its own component",
                          subject="TAP", predicate="uses", obj="com_tia",
                          valid_from=datetime(2026, 6, 8, tzinfo=timezone.utc))

    user_id = Control.create_principal(conn, "test-rec-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)

    print("=== reconcile (local-claim vs memnos staleness) ===")
    check("no token -> 401", call("/reconcile", None, {"namespace": NS, "statement": "x"})[0] == 401)
    check("ungranted ns -> 403", call("/reconcile", user_tok, {"namespace": "test:nope", "statement": "x"})[0] == 403)
    check("no statement -> 400", call("/reconcile", user_tok, {"namespace": NS})[0] == 400)

    # STALE local claim: local note says TAP uses the paid plugin; memnos says com_tia
    s, j = call("/reconcile", user_tok, {"namespace": NS,
                "statement": "TAP uses the paid ChatGPT assistant plugin",
                "subject": "TAP", "predicate": "uses"})
    check("reconcile 200", s == 200)
    check("flagged STALE (local contradicts memnos)", j.get("stale") is True)
    check("conflict surfaces the memnos current value", any("com_tia" in (c.get("object") or "") for c in j.get("conflicts", [])))
    check("conflict carries the date (valid_from)", j.get("conflicts") and j["conflicts"][0].get("valid_from"))

    # NON-stale: local claim already reflects the memnos value
    s, j = call("/reconcile", user_tok, {"namespace": NS,
                "statement": "TAP uses com_tia for its assistant",
                "subject": "TAP", "predicate": "uses"})
    check("agreeing claim -> not stale", j.get("stale") is False)

    # unknown subject -> nothing to contradict
    s, j = call("/reconcile", user_tok, {"namespace": NS,
                "statement": "Sirath uses Azure", "subject": "Sirath", "predicate": "uses"})
    check("unknown subject -> not stale, no matches", j.get("stale") is False and not j.get("matches"))

    cleanup(conn)
    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (user_id,))
        c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (user_id,))
        c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (user_id,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
