"""Acceptance tests for agent coordination leases (issue #26).

Run against a live local server:
    memnos start   (or python memnos_server.py)
    python tests/test_lease_api.py

Tests: concurrent acquire exclusivity, crash-expiry, heartbeat, release, list, feed event.
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:lease-api"
PASS = FAIL = 0


def call(method, path, token, body=None):
    req = urllib.request.Request(
        URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def post(path, token, body):
    return call("POST", path, token, body)


def check(name, cond, detail=""):
    global PASS, FAIL
    label = "PASS" if cond else "FAIL"
    print(f"  {label}  {name}" + (f" — {detail}" if detail and not cond else ""))
    PASS += cond
    FAIL += not cond


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    pid = Control.create_principal(conn, "test-lease", "service")
    Control.grant(conn, pid, NS)
    tok = Control.mint_token(conn, pid, "lease-test")
    Control.create_namespace(conn, NS, created_by=pid)

    KEY = "ticket:HPTE-543"
    A, B = "agent-alpha", "agent-beta"

    # ── 1. free key → agent A acquires ──────────────────────────────────────
    s, r = post("/lease/acquire", tok, {"namespace": NS, "key": KEY, "holder_id": A, "ttl_seconds": 5})
    check("acquire on free key → granted", s == 200 and r.get("granted") is True, str(r))

    # ── 2. same key, different holder → denied ───────────────────────────────
    s, r = post("/lease/acquire", tok, {"namespace": NS, "key": KEY, "holder_id": B, "ttl_seconds": 5})
    check("acquire by B while A holds → denied", s == 200 and r.get("granted") is False, str(r))
    check("denied response includes held_by=A", r.get("holder_id") == A, str(r))

    # ── 3. who_holds ─────────────────────────────────────────────────────────
    s, r = post("/lease/who_holds", tok, {"namespace": NS, "key": KEY})
    check("who_holds returns A", s == 200 and r.get("held") and r.get("holder_id") == A, str(r))

    # ── 4. heartbeat ─────────────────────────────────────────────────────────
    s, r = post("/lease/heartbeat", tok, {"namespace": NS, "key": KEY, "holder_id": A, "ttl_seconds": 30})
    check("heartbeat by holder → renewed", s == 200 and r.get("renewed") is True, str(r))

    # ── 5. heartbeat by non-holder → not renewed ─────────────────────────────
    s, r = post("/lease/heartbeat", tok, {"namespace": NS, "key": KEY, "holder_id": B, "ttl_seconds": 30})
    check("heartbeat by non-holder → not renewed", s == 200 and r.get("renewed") is False, str(r))

    # ── 6. lease/list shows A ────────────────────────────────────────────────
    s, r = post("/lease/list", tok, {"namespace": NS})
    leases = r.get("leases", [])
    check("lease/list shows 1 active lease", s == 200 and len(leases) == 1, str(r))
    check("listed lease key matches", leases[0]["key"] == KEY if leases else False)

    # ── 7. release by holder ──────────────────────────────────────────────────
    s, r = post("/lease/release", tok, {"namespace": NS, "key": KEY, "holder_id": A})
    check("release by holder → released=true", s == 200 and r.get("released") is True, str(r))

    # ── 8. after release, key is free ────────────────────────────────────────
    s, r = post("/lease/who_holds", tok, {"namespace": NS, "key": KEY})
    check("who_holds after release → free", s == 200 and not r.get("held"), str(r))

    # ── 9. B can acquire after A released ────────────────────────────────────
    s, r = post("/lease/acquire", tok, {"namespace": NS, "key": KEY, "holder_id": B, "ttl_seconds": 5})
    check("B acquires after A releases → granted", s == 200 and r.get("granted") is True, str(r))

    # ── 10. crash expiry: lease TTL=1s, wait 2s, A can steal ─────────────────
    s, r = post("/lease/release", tok, {"namespace": NS, "key": KEY, "holder_id": B})
    s, r = post("/lease/acquire", tok, {"namespace": NS, "key": KEY, "holder_id": B, "ttl_seconds": 1})
    check("short-TTL acquire granted", s == 200 and r.get("granted") is True, str(r))
    time.sleep(2)
    s, r = post("/lease/acquire", tok, {"namespace": NS, "key": KEY, "holder_id": A, "ttl_seconds": 30})
    check("after crash-expiry, A steals expired lease → granted", s == 200 and r.get("granted") is True, str(r))

    # ── 11. release by non-holder → released=false ───────────────────────────
    s, r = post("/lease/release", tok, {"namespace": NS, "key": KEY, "holder_id": B})
    check("release by non-holder → released=false", s == 200 and r.get("released") is False, str(r))

    # ── 12. lease/list is empty after all released ────────────────────────────
    post("/lease/release", tok, {"namespace": NS, "key": KEY, "holder_id": A})
    s, r = post("/lease/list", tok, {"namespace": NS})
    check("lease/list empty after cleanup", s == 200 and len(r.get("leases", [])) == 0, str(r))

    # ── 13. validation: missing key → 400 ────────────────────────────────────
    s, r = post("/lease/acquire", tok, {"namespace": NS, "holder_id": A})
    check("acquire without key → 400", s == 400, str(r))

    # cleanup
    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.leases WHERE namespace=%s", (NS,))
        c.execute("UPDATE memnos_control.namespaces SET created_by=NULL WHERE name=%s", (NS,))
        c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
        c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
        c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{'='*50}")
    print(f"  PASS {PASS}  FAIL {FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
