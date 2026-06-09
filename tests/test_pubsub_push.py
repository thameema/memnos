"""End-to-end tests for Batch 3 webhook PUSH delivery. Starts a real local webhook
receiver, subscribes through the live memnos server, writes memories, and asserts the
webhook receives them (via the background pusher and/or the admin 'deliver' endpoint).
Also verifies the failure path: a dead webhook is retried and deactivated after
max_failures, and its cursor never advances. No LLM.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_pubsub_push.py
"""
import http.server
import json
import os
import socket
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
NS = "test:push"
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


class Receiver(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            Receiver.received.append(json.loads(self.rfile.read(n) or b"{}"))
        except Exception:
            pass
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def start_receiver():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/hook"


def dead_url():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return f"http://127.0.0.1:{p}/hook"      # nothing listening -> connection refused


def delivered(sid):
    out = []
    for body in list(Receiver.received):
        if body.get("subscription_id") == sid:
            out += [e["content"] for e in body.get("events", [])]
    return out


def wait_until(fn, timeout=15, step=0.4):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return fn()


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace=%s", (NS,))

    srv, hook = start_receiver()
    admin_id = Control.create_principal(conn, "test-push-admin", "service")
    Control.grant(conn, admin_id, "*")
    admin_tok = Control.mint_token(conn, admin_id, "test")
    user_id = Control.create_principal(conn, "test-push-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)

    print("=== namespace pub/sub WEBHOOK PUSH (Batch 3 full) ===")

    # --- success path: subscribe with a webhook, write, webhook receives ---
    s, sub = call("POST", "/subscribe", user_tok, {"namespace": NS, "webhook": hook})
    sid = sub["subscription_id"]
    check("subscribe with webhook 200", s == 200 and sid)
    for t in ("push event one", "push event two", "push event three"):
        call("POST", "/memory/write", user_tok, {"namespace": NS, "text": t})
    got = wait_until(lambda: len(delivered(sid)) >= 3)
    check("webhook received all 3 events (background pusher)", got)
    check("events in order", delivered(sid)[:3] == ["push event one", "push event two", "push event three"])

    # incremental: a later write is pushed too, not the old ones again in a flood
    base = len(delivered(sid))
    call("POST", "/memory/write", user_tok, {"namespace": NS, "text": "push event four"})
    check("incremental push of new event", wait_until(lambda: "push event four" in delivered(sid)))

    # admin deliver-now endpoint works (idempotent — nothing pending or re-confirms)
    s, j = call("POST", "/admin/api/deliver", admin_tok, {})
    check("admin deliver endpoint 200", s == 200 and "delivered" in j)
    check("non-admin cannot deliver", call("POST", "/admin/api/deliver", user_tok, {})[0] == 403)

    # --- failure path: dead webhook is retried then deactivated; cursor frozen ---
    s, subF = call("POST", "/subscribe", user_tok, {"namespace": NS, "webhook": dead_url()})
    sidF = subF["subscription_id"]
    cursor0 = subF["cursor"]
    call("POST", "/memory/write", user_tok, {"namespace": NS, "text": "to a dead webhook"})

    def sub_state(target):
        _, jj = call("GET", f"/admin/api/subscriptions?principal={user_id}", admin_tok)
        for su in jj.get("subscriptions", []):
            if su["id"] == target:
                return su
        return None

    # drive deliveries via the admin endpoint until it deactivates (max_failures=5)
    for _ in range(8):
        call("POST", "/admin/api/deliver", admin_tok, {})
        st = sub_state(sidF)
        if st and not st["active"]:
            break
        time.sleep(0.2)
    st = sub_state(sidF)
    check("dead webhook deactivated after retries", st is not None and st["active"] is False)
    check("failed sub cursor never advanced", st["cursor"] == cursor0)
    check("failure count recorded", st["delivery_failures"] >= 5)

    # the healthy subscription is still active + still delivering
    healthy = sub_state(sid)
    check("healthy sub still active", healthy is not None and healthy["active"] is True)

    # list endpoint
    s, j = call("GET", f"/admin/api/subscriptions?principal={user_id}", admin_tok)
    check("admin can list subscriptions", s == 200 and len(j.get("subscriptions", [])) >= 2)

    # cleanup
    srv.shutdown()
    with conn.cursor() as c:
        c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace=%s", (NS,))
        for pid in (admin_id, user_id):
            c.execute("DELETE FROM memnos_control.subscriptions WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
