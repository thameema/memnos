"""No-AI tests for WIDE recall — an agent with a default namespace widening the search
across every namespace its key is permitted to read (ACL-bounded). Seeds three namespaces;
the token is granted two of them; wide recall returns memories from both granted ones (each
tagged with its namespace) and excludes the ungranted one. Default recall stays scoped.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_wide_recall_api.py
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
SCHEMA = "tenant_memnos"
WA, WB, WC = "test:wa", "test:wb", "test:wc"
PASS = FAIL = 0


def call(path, token=None, body=None):
    req = urllib.request.Request(URL + path, method="POST",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        for ns in (WA, WB, WC):
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (ns,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # distinctive shared FTS token 'zorptron' in each namespace
    store.insert_raw_turn(SCHEMA, WA, None, "user", "alpha zorptron lives in Atlantis", now, None)
    store.insert_raw_turn(SCHEMA, WB, None, "user", "beta zorptron works at Borealis", now, None)
    store.insert_raw_turn(SCHEMA, WC, None, "user", "gamma zorptron flies to Cathay", now, None)

    user_id = Control.create_principal(conn, "wide-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "t")
    Control.grant(conn, user_id, WA, can_read=True, can_write=True)   # default
    Control.grant(conn, user_id, WB, can_read=True, can_write=False)  # widen target
    # NOTE: NOT granted WC

    print("=== wide recall (default ns + widen across permitted) ===")
    # default recall: only the primary namespace
    s, j = call("/recall", user_tok, {"namespace": WA, "query": "zorptron"})
    txt = " ".join(m.get("content", "") for m in j.get("memories", []))
    check("default recall scoped to WA only", "Atlantis" in txt and "Borealis" not in txt)

    # WIDE recall: across WA + WB (granted), NOT WC
    s, j = call("/recall", user_tok, {"namespace": WA, "query": "zorptron", "scope": "all"})
    check("wide recall 200", s == 200)
    nss = set(j.get("namespaces_searched", []))
    check("searched exactly the readable namespaces", WA in nss and WB in nss and WC not in nss)
    wtxt = " ".join(m.get("content", "") for m in j.get("memories", []))
    check("wide returns WA content", "Atlantis" in wtxt)
    check("wide returns WB content (other namespace)", "Borealis" in wtxt)
    check("wide EXCLUDES ungranted WC", "Cathay" not in wtxt)
    tags = {m.get("namespace") for m in j.get("memories", [])}
    check("results tagged with source namespace", tags and tags <= {WA, WB})
    check("context tags the namespace", "[test:wb]" in j.get("context", "") or "[test:wa]" in j.get("context", ""))

    # granting WC widens to include it
    Control.grant(conn, user_id, WC, can_read=True, can_write=False)
    s, j = call("/recall", user_tok, {"namespace": WA, "query": "zorptron", "scope": "all"})
    check("after granting WC, wide now includes it", "Cathay" in " ".join(m.get("content", "") for m in j.get("memories", [])))

    cleanup(conn)
    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (user_id,))
        c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (user_id,))
        c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (user_id,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
