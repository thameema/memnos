"""No-AI tests for Batch 3: namespace pub/sub (subscribe + cursor feed). Verifies a
subscription only delivers memories written AFTER it, that the cursor advances, and the
ownership/ACL boundaries. No LLM (memory_write stores a raw turn without extraction).

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_pubsub_api.py
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
NS = "test:subapi"
PASS = FAIL = 0


def call(method, path, token=None, body=None):
    req = urllib.request.Request(URL + path, method=method,
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


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    # clean any prior rows for this ns
    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace=%s", (NS,))

    user_id = Control.create_principal(conn, "test-sub-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)
    other_id = Control.create_principal(conn, "test-sub-other", "agent")
    other_tok = Control.mint_token(conn, other_id, "test")
    Control.grant(conn, other_id, NS, can_read=True, can_write=True)

    print("=== namespace pub/sub (Batch 3) ===")
    check("no token -> 401", call("POST", "/subscribe", None, {"namespace": NS})[0] == 401)
    check("ungranted ns -> 403", call("POST", "/subscribe", user_tok, {"namespace": "test:nope"})[0] == 403)

    # subscribe AFTER a pre-existing write -> that write must NOT appear in the feed
    call("POST", "/memory/write", user_tok, {"namespace": NS, "text": "before-subscribe note"})
    s, sub = call("POST", "/subscribe", user_tok, {"namespace": NS})
    check("subscribe 200 + id + cursor", s == 200 and sub.get("subscription_id") and "cursor" in sub)
    sid = sub["subscription_id"]

    s, j = call("POST", "/feed", user_tok, {"namespace": NS, "subscription_id": sid})
    check("fresh feed is empty (cursor at head)", s == 200 and j.get("items") == [])

    # write two new memories -> feed delivers exactly those, in order
    call("POST", "/memory/write", user_tok, {"namespace": NS, "text": "event one"})
    call("POST", "/memory/write", user_tok, {"namespace": NS, "text": "event two"})
    s, j = call("POST", "/feed", user_tok, {"namespace": NS, "subscription_id": sid})
    texts = [i["content"] for i in j.get("items", [])]
    check("feed delivers new memories", texts == ["event one", "event two"])
    check("before-subscribe note excluded", "before-subscribe note" not in texts)

    # cursor advanced -> second poll empty
    s, j = call("POST", "/feed", user_tok, {"namespace": NS, "subscription_id": sid})
    check("cursor advanced (second poll empty)", j.get("items") == [])

    # only new writes after the cursor are delivered
    call("POST", "/memory/write", user_tok, {"namespace": NS, "text": "event three"})
    s, j = call("POST", "/feed", user_tok, {"namespace": NS, "subscription_id": sid})
    check("incremental delivery", [i["content"] for i in j.get("items", [])] == ["event three"])

    # ownership: another principal cannot poll someone else's subscription
    check("foreign principal feed -> 404", call("POST", "/feed", other_tok, {"namespace": NS, "subscription_id": sid})[0] == 404)
    # wrong namespace for the subscription -> 404 (after ACL passes via a grant on other ns? use same principal, mismatched ns)
    check("bad subscription_id -> 404", call("POST", "/feed", user_tok, {"namespace": NS, "subscription_id": 999999999})[0] == 404)

    # webhook stored
    s, sub2 = call("POST", "/subscribe", user_tok, {"namespace": NS, "webhook": "https://example.com/hook"})
    check("subscribe with webhook", s == 200 and sub2.get("webhook") == "https://example.com/hook")

    # unsubscribe -> feed now 404
    s, j = call("POST", "/unsubscribe", user_tok, {"namespace": NS, "subscription_id": sid})
    check("unsubscribe 200 + true", s == 200 and j.get("unsubscribed") is True)
    check("feed after unsubscribe -> 404", call("POST", "/feed", user_tok, {"namespace": NS, "subscription_id": sid})[0] == 404)

    # cleanup
    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace=%s", (NS,))
        for pid in (user_id, other_id):
            c.execute("DELETE FROM memnos_control.subscriptions WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
