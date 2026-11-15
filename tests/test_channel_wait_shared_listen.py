"""Shared-LISTEN fan-out for /channel/wait (Layer A of the memnos-wake design).

Before: every /channel/wait request opened its own `LISTEN memnos_channel` connection
and held it for up to 120s -- N concurrent waiters held N DB connections. Now: one
background thread per server process holds the only LISTEN connection and wakes
waiters via in-process Events. This asserts (a) multiple concurrent waiters all get
woken promptly by one publish, and (b) the number of backends actually LISTENing on
Postgres does not grow with the number of concurrent waiters.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5433/memnos python tests/test_channel_wait_shared_listen.py
"""
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:channel_wait_shared_listen"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond)
    FAIL += not cond


def call(method, path, token=None, body=None, timeout=20):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def listen_backend_count(conn):
    r = conn.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE query ILIKE 'LISTEN%'").fetchone()
    return r["n"]


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    user_id = Control.create_principal(conn, "test-wait-shared-listen-user", "agent")
    tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)

    print("=== shared-LISTEN fan-out for /channel/wait ===")

    for name in ("waiter-a", "waiter-b", "waiter-c"):
        s, _ = call("POST", "/session/register", tok, {"namespace": NS, "name": name, "ttl_seconds": 300})
        check(f"register {name} 200", s == 200)

    baseline = listen_backend_count(conn)

    results = {}
    def waiter(name):
        s, out = call("POST", "/channel/wait", tok,
                      {"namespace": NS, "name": name, "timeout_seconds": 15})
        results[name] = (s, out)

    threads = [threading.Thread(target=waiter, args=(n,)) for n in ("waiter-a", "waiter-b", "waiter-c")]
    for t in threads:
        t.start()
    time.sleep(0.5)   # let all 3 register and block in /channel/wait

    during = listen_backend_count(conn)
    check("LISTEN backend count does not scale with concurrent waiters (shared, not per-request)",
          during <= baseline + 1)   # +1 tolerance: the one shared listener thread itself

    s, _ = call("POST", "/channel/publish", tok,
               {"namespace": NS, "channel": "@waiter-a", "content": "hi a", "sender_session": "pub", "intent": "message"})
    check("publish to waiter-a 200", s == 200)
    s, _ = call("POST", "/channel/publish", tok,
               {"namespace": NS, "channel": "@waiter-b", "content": "hi b", "sender_session": "pub", "intent": "message"})
    check("publish to waiter-b 200", s == 200)
    s, _ = call("POST", "/inbox/send", tok,
               {"namespace": NS, "to_session": "waiter-c", "sender_session": "pub", "content": "hi c", "intent": "message"})
    check("inbox send to waiter-c 200", s == 200)

    for t in threads:
        t.join(timeout=20)

    sa, oa = results.get("waiter-a", (None, {}))
    sb, ob = results.get("waiter-b", (None, {}))
    sc, oc = results.get("waiter-c", (None, {}))
    check("waiter-a woken promptly (not timed out)", sa == 200 and not oa.get("timed_out"))
    check("waiter-b woken promptly (not timed out)", sb == 200 and not ob.get("timed_out"))
    check("waiter-c woken promptly via inbox (not timed out)", sc == 200 and not oc.get("timed_out"))

    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.agent_inbox WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.channel_messages WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.channel_subscriptions WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.agent_sessions WHERE namespace=%s", (NS,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
