"""Pagination + windowing contract tests for the admin console API (large-volume UX).

Covers the scale fixes: audit limit/offset pagination + approximate total, usage/stats
hour windows, and the /recall response still carrying a context consistent with its
memories (context is now rendered from the same rows — one retrieval pass).

Run against a live local server (like test_admin_api.py):
    python memnos_server.py &
    python tests/test_admin_pagination.py
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
NS = "test:admin-pagination"
ACTION = "pagetest_op"
PASS = FAIL = 0


def call(method, path, token=None, body=None):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=30)
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
    admin_id = Control.create_principal(conn, "test-page-admin", "service")
    Control.grant(conn, admin_id, "*")
    admin_tok = Control.mint_token(conn, admin_id, "test")

    print("=== admin pagination / windowing tests ===")
    # seed 250 distinctive audit rows directly (newest-first pagination over them)
    for i in range(250):
        Control.audit(conn, admin_id, ACTION, NS, True, {"i": i}, latency_ms=i, status=200)

    # --- audit pagination contract ---
    s, j = call("GET", "/admin/api/audit", admin_tok)
    check("audit default page: HTTP 200", s == 200)
    check("audit default limit is 100", j.get("limit") == 100 and len(j.get("audit", [])) == 100)
    check("audit response carries total", isinstance(j.get("total"), int) and j["total"] >= 0)
    check("audit total flagged as estimate", j.get("total_estimated") is True)
    check("audit response echoes offset", j.get("offset") == 0)

    s, p1 = call("GET", "/admin/api/audit?limit=50&offset=0", admin_tok)
    s, p2 = call("GET", "/admin/api/audit?limit=50&offset=50", admin_tok)
    check("audit limit honored", len(p1["audit"]) == 50 and len(p2["audit"]) == 50)
    ids1 = {(r["ts"], r.get("latency_ms")) for r in p1["audit"]}
    ids2 = {(r["ts"], r.get("latency_ms")) for r in p2["audit"]}
    check("audit offset returns a different page", ids1.isdisjoint(ids2))
    # our 250 rows are the newest: page1+page2 latencies descend (newest first);
    # tolerate same-microsecond ts ties + the odd interleaved event from the server itself
    lats = [r["latency_ms"] for r in p1["audit"] + p2["audit"] if r["action"] == ACTION]
    inversions = sum(1 for a, b in zip(lats, lats[1:]) if a < b)
    check("audit ordered newest-first", len(lats) >= 90 and inversions <= 2)

    s, j = call("GET", "/admin/api/audit?limit=999999", admin_tok)
    check("audit limit clamped to 1000", s == 200 and j["limit"] == 1000 and len(j["audit"]) <= 1000)
    s, j = call("GET", "/admin/api/audit?limit=1&offset=-5", admin_tok)
    check("audit negative offset clamped to 0", s == 200 and j["offset"] == 0 and len(j["audit"]) == 1)

    # --- stats windowing ---
    s, j = call("GET", "/admin/api/stats", admin_tok)
    check("stats default window 24h", s == 200 and j.get("window_hours") == 24)
    row = next((o for o in j.get("ops", []) if o["action"] == ACTION), None)
    check("stats aggregates the seeded op", row is not None and row["calls"] >= 250)
    check("stats p50 within seeded latency range", row is not None and 0 <= int(row["p50_ms"]) <= 249)
    s, j = call("GET", "/admin/api/stats?hours=1", admin_tok)
    check("stats hours param honored", s == 200 and j.get("window_hours") == 1)
    s, j = call("GET", "/admin/api/stats?hours=99999", admin_tok)
    check("stats hours clamped to 720", s == 200 and j.get("window_hours") == 720)

    # --- usage rollup correctness + optional window ---
    Control.record_usage(conn, admin_id, NS, ACTION, "test-model", 100, 10, 0.5)
    Control.record_usage(conn, admin_id, NS, ACTION, "test-model", 100, 10, 0.25)
    s, j = call("GET", "/admin/api/usage", admin_tok)
    check("usage all-time by default", s == 200 and "by_op" in j)
    u = j.get("by_op", {}).get(ACTION)
    check("usage rollup sums op rows", u is not None and u["n"] >= 2
          and abs(float(u["usd"]) - 0.75) < 1e-6 and int(u["tokens_in"]) >= 200)
    s, j = call("GET", "/admin/api/usage?days=1", admin_tok)
    u = j.get("by_op", {}).get(ACTION)
    check("usage hours window honored", s == 200 and j.get("period_days") == 1
          and u is not None and u["n"] >= 2)

    # --- /recall contract: memories + context from the SAME rows ---
    Control.create_namespace(conn, NS, created_by=admin_id)
    call("POST", "/remember", admin_tok, {"namespace": NS, "text": "The pagination probe stores this fact."})
    s, j = call("POST", "/recall", admin_tok, {"namespace": NS, "query": "pagination probe"})
    check("recall returns memories + context", s == 200 and "memories" in j and "context" in j)
    if j.get("memories"):
        check("recall context built from returned memories",
              all(m["content"] in j["context"] for m in j["memories"][:3] if m.get("content")))
    else:
        check("recall context built from returned memories", j.get("context") == "")

    # cleanup
    call("DELETE", f"/admin/api/namespaces?name={NS}&purge=1", admin_tok)
    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.audit_log WHERE action=%s", (ACTION,))
        c.execute("DELETE FROM memnos_control.usage_ledger WHERE op=%s", (ACTION,))
        c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (admin_id,))
        c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (admin_id,))
        c.execute("DELETE FROM memnos_control.principals WHERE name='test-page-admin'")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
