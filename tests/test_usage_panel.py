"""Tests for the cost/token usage panel (issue #14).

No LLM or embeddings required. Run against a live local server:

    python memnos_server.py &
    python tests/test_usage_panel.py

Inserts mock usage_ledger rows directly via psycopg, exercises
Control.usage_summary, /admin/api/usage, /admin/api/budget, and
budget threshold detection. Exits non-zero on any failure.
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


def insert_row(conn, namespace, op, tokens_in, tokens_out, cost_usd, principal_id=None):
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO memnos_control.usage_ledger"
            "(principal_id, namespace, op, model, tokens_in, tokens_out, cost_usd) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (principal_id, namespace, op, "test-model", tokens_in, tokens_out, cost_usd))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    admin_id = Control.create_principal(conn, "test-usage-panel-admin", "service")
    Control.grant(conn, admin_id, "*")
    admin_tok = Control.mint_token(conn, admin_id, "usage-panel-test")
    user_id = Control.create_principal(conn, "test-usage-panel-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "usage-panel-test-user")

    ns_a = "test:usage-ns-a"
    ns_b = "test:usage-ns-b"

    print("=== usage panel: insert mock ledger rows ===")
    insert_row(conn, ns_a, "extract",     100, 50, 0.002, admin_id)
    insert_row(conn, ns_a, "extract",     200, 80, 0.004, admin_id)
    insert_row(conn, ns_a, "consolidate", 300, 60, 0.006, admin_id)
    insert_row(conn, ns_b, "embed",       400,  0, 0.001, admin_id)
    insert_row(conn, ns_b, "answer",      150, 90, 0.003, admin_id)

    expected_total_usd    = 0.002 + 0.004 + 0.006 + 0.001 + 0.003
    expected_total_tokens = (100+50) + (200+80) + (300+60) + (400+0) + (150+90)

    print("=== Control.usage_summary ===")
    s = Control.usage_summary(conn, period_days=30)
    check("total_usd >= inserted total",    s["total_usd"] >= expected_total_usd - 0.0001)
    check("total_tokens >= inserted total", s["total_tokens"] >= expected_total_tokens)
    check("by_op has extract",              "extract" in s["by_op"])
    check("by_op extract calls >= 2",      s["by_op"]["extract"]["n"] >= 2)
    check("by_namespace has ns_a",          ns_a in s["by_namespace"])
    check("by_namespace has ns_b",          ns_b in s["by_namespace"])
    check("by_day is a list",               isinstance(s["by_day"], list))
    check("by_day entry has date field",    len(s["by_day"]) == 0 or "date" in s["by_day"][0])

    print("=== Control.usage_summary with namespace filter ===")
    s_ns = Control.usage_summary(conn, period_days=30, namespace=ns_a)
    check("filtered: total_tokens from ns_a only",
          s_ns["total_tokens"] == (100+50) + (200+80) + (300+60))
    check("filtered: ns_b absent from by_namespace", ns_b not in s_ns["by_namespace"])

    print("=== /admin/api/usage endpoint ===")
    sc, j = call("GET", "/admin/api/usage", admin_tok)
    check("/admin/api/usage admin -> 200",       sc == 200)
    check("response has total_usd",              "total_usd" in j)
    check("response has total_tokens",           "total_tokens" in j)
    check("response has by_op",                  "by_op" in j)
    check("response has by_namespace",           "by_namespace" in j)
    check("response has by_day",                 "by_day" in j)
    check("/admin/api/usage no-token -> 401",    call("GET", "/admin/api/usage")[0] == 401)
    check("/admin/api/usage non-admin -> 403",   call("GET", "/admin/api/usage", user_tok)[0] == 403)

    print("=== /admin/api/usage?namespace= filter ===")
    sc2, j2 = call("GET", f"/admin/api/usage?namespace={ns_b}", admin_tok)
    check("namespace filter 200",                sc2 == 200)
    check("filtered result has by_namespace",    "by_namespace" in j2)

    print("=== /admin/api/budget endpoint ===")
    sc3, j3 = call("GET", "/admin/api/budget", admin_tok)
    check("/admin/api/budget -> 200",            sc3 == 200)
    check("budget has daily_ok",                 "daily_ok" in j3)
    check("budget has monthly_ok",               "monthly_ok" in j3)
    check("budget has exceeded",                 "exceeded" in j3)

    print("=== budget threshold detection (direct, no env) ===")
    bs_under = Control.budget_status(conn, daily_usd=9999.0, monthly_usd=9999.0)
    check("under threshold: daily_ok",           bs_under["daily_ok"] is True)
    check("under threshold: monthly_ok",         bs_under["monthly_ok"] is True)
    check("under threshold: exceeded False",     bs_under["exceeded"] is False)

    bs_over = Control.budget_status(conn, daily_usd=0.000001, monthly_usd=9999.0)
    check("daily exceeded: daily_ok False",      bs_over["daily_ok"] is False)
    check("daily exceeded: exceeded True",       bs_over["exceeded"] is True)

    bs_monthly = Control.budget_status(conn, daily_usd=9999.0, monthly_usd=0.000001)
    check("monthly exceeded: monthly_ok False",  bs_monthly["monthly_ok"] is False)
    check("monthly exceeded: exceeded True",     bs_monthly["exceeded"] is True)

    bs_none = Control.budget_status(conn, daily_usd=None, monthly_usd=None)
    check("no thresholds: daily_ok True",        bs_none["daily_ok"] is True)
    check("no thresholds: monthly_ok True",      bs_none["monthly_ok"] is True)
    check("no thresholds: exceeded False",       bs_none["exceeded"] is False)

    print(f"\n{'='*40}")
    print(f"  {PASS} passed  {FAIL} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
