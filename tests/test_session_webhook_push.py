"""Webhook PUSH for named-session channel/inbox events (Control.deliver_pending_sessions) —
the push-side counterpart to /channel/wait. Mirrors tests/test_pubsub_push.py's shape for the
namespace-level feed, but for session channel/inbox events. Starts a real local webhook
receiver, registers a session with that webhook, publishes a channel message + sends an inbox
item to it, and asserts the webhook receives both, each signed with the registration's
webhook_secret via X-Memnos-Signature. Also checks the SSRF guard on /session/register:
link-local and private-range webhooks are rejected, loopback is allowed. Also checks that
session_list never exposes another session's webhook_secret to peers.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5433/memnos python tests/test_session_webhook_push.py
"""
import hashlib
import hmac
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
NS = "test:session_webhook_push"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond)
    FAIL += not cond


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


def start_receiver(box):
    class Receiver(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers["Content-Length"])
            raw = self.rfile.read(n)
            box.append((json.loads(raw), raw, self.headers.get("X-Memnos-Signature", "")))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/hook"


def verify_sig(secret, raw_body, sig_header):
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


def dead_url():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return f"http://127.0.0.1:{p}/hook"      # nothing listening -> connection refused


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    received = []
    srv, hook = start_receiver(received)
    user_id = Control.create_principal(conn, "test-session-webhook-user", "agent")
    tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)
    admin_id = Control.create_principal(conn, "test-session-webhook-admin", "service")
    Control.grant(conn, admin_id, "*")
    admin_tok = Control.mint_token(conn, admin_id, "test")

    print("=== named-session channel/inbox WEBHOOK PUSH ===")

    s, reg = call("POST", "/session/register", tok,
                 {"namespace": NS, "name": "webhook-agent", "webhook": hook, "ttl_seconds": 300})
    check("register with webhook 200", s == 200 and reg.get("name") == "webhook-agent")
    secret = reg.get("webhook_secret")
    check("registration returns a webhook_secret", bool(secret))

    s, _ = call("POST", "/session/register", tok, {"namespace": NS, "name": "sender-agent", "ttl_seconds": 300})
    check("register sender 200", s == 200)

    s, sess_list = call("POST", "/session/list", tok, {"namespace": NS})
    rows = {r["name"]: r for r in sess_list.get("sessions", [])}
    check("session list never exposes webhook_secret to peers",
          s == 200 and all("webhook_secret" not in r for r in rows.values()))
    check("session list never exposes the raw webhook URL to peers",
          all("webhook" not in r for r in rows.values()))
    check("session list reports has_webhook instead", rows.get("webhook-agent", {}).get("has_webhook") is True)

    s, _ = call("POST", "/channel/publish", tok,
               {"namespace": NS, "channel": "@webhook-agent", "content": "hello via push",
                "sender_session": "sender-agent", "intent": "message"})
    check("publish direct message 200", s == 200)

    deadline = time.time() + 15
    while time.time() < deadline and not any(p.get("channel_messages") for p, _, _ in received):
        time.sleep(0.5)
    check("webhook received the channel message", any(p.get("channel_messages") for p, _, _ in received))
    check("channel message delivery is correctly signed",
          any(p.get("channel_messages") and verify_sig(secret, raw, sig) for p, raw, sig in received))

    received.clear()
    s, _ = call("POST", "/inbox/send", tok,
               {"namespace": NS, "to_session": "webhook-agent", "sender_session": "sender-agent",
                "content": "urgent task", "subject": "test", "priority": "urgent"})
    check("inbox send 200", s == 200)

    deadline = time.time() + 15
    while time.time() < deadline and not any(p.get("inbox_items") for p, _, _ in received):
        time.sleep(0.5)
    check("webhook received the inbox item", any(p.get("inbox_items") for p, _, _ in received))
    check("inbox item delivery is correctly signed",
          any(p.get("inbox_items") and verify_sig(secret, raw, sig) for p, raw, sig in received))

    # re-registering without --webhook must NOT silently wipe an existing one
    s, re_reg = call("POST", "/session/register", tok,
                     {"namespace": NS, "name": "webhook-agent", "current_task": "still here"})
    check("re-register omitting webhook 200", s == 200)
    check("re-register omitting webhook preserves it", re_reg.get("webhook_secret") == secret)

    # explicitly clearing it still works
    s, cleared = call("POST", "/session/register", tok,
                      {"namespace": NS, "name": "webhook-agent", "webhook": None})
    check("re-register with webhook=null clears it", s == 200 and not cleared.get("webhook_secret"))
    # restore it for the admin-deliver + dead-webhook checks below
    s, reg = call("POST", "/session/register", tok,
                 {"namespace": NS, "name": "webhook-agent", "webhook": hook, "ttl_seconds": 300})
    secret = reg.get("webhook_secret")

    # admin deliver-now endpoint must flush session webhooks too, not just the old namespace feed
    received.clear()
    s, _ = call("POST", "/channel/publish", tok,
               {"namespace": NS, "channel": "@webhook-agent", "content": "via admin deliver",
                "sender_session": "sender-agent", "intent": "message"})
    s, dl = call("POST", "/admin/api/deliver", admin_tok, {})
    check("admin deliver endpoint 200 and covers sessions", s == 200 and "delivered_sessions" in dl)
    deadline = time.time() + 10
    while time.time() < deadline and not received:
        time.sleep(0.3)
    check("admin deliver pushed the pending channel message", bool(received))

    # --- failure path: dead webhook retried then deactivated; cursor/inbox state frozen ---
    s, _ = call("POST", "/session/register", tok,
               {"namespace": NS, "name": "dead-webhook-agent", "webhook": dead_url(), "ttl_seconds": 300})
    check("register dead-webhook session 200", s == 200)
    s, _ = call("POST", "/channel/publish", tok,
               {"namespace": NS, "channel": "@dead-webhook-agent", "content": "never delivered",
                "sender_session": "sender-agent", "intent": "message"})
    check("publish to dead-webhook session 200", s == 200)

    def session_row(name):
        _, jj = call("POST", "/session/list", tok, {"namespace": NS})
        return {r["name"]: r for r in jj.get("sessions", [])}.get(name)

    for _ in range(8):
        call("POST", "/admin/api/deliver", admin_tok, {})
        row = session_row("dead-webhook-agent")
        if row and not row["webhook_active"]:
            break
        time.sleep(0.3)
    row = session_row("dead-webhook-agent")
    check("dead webhook deactivated after retries", row is not None and row["webhook_active"] is False)
    check("failure count recorded", row is not None and row["webhook_failures"] >= 5)

    s, poll = call("POST", "/channel/poll", tok, {"namespace": NS, "name": "dead-webhook-agent"})
    check("message never consumed by the failed push -- pull still sees it",
          s == 200 and any(m["content"] == "never delivered" for m in poll.get("messages", [])))

    # SSRF guard on /session/register's webhook param
    s, _ = call("POST", "/session/register", tok,
               {"namespace": NS, "name": "bad-agent", "webhook": "http://169.254.169.254/latest/meta-data"})
    check("link-local webhook rejected 400", s == 400)

    s, _ = call("POST", "/session/register", tok,
               {"namespace": NS, "name": "bad-agent2", "webhook": "http://10.0.0.5/hook"})
    check("private-range webhook rejected 400", s == 400)

    s, _ = call("POST", "/session/register", tok,
               {"namespace": NS, "name": "loop-agent", "webhook": "http://127.0.0.1:9/hook"})
    check("loopback webhook accepted at registration", s == 200)

    # namespace-scoped cleanup only (this test's own data); leaving the principal/token/grant
    # rows behind is harmless in a throwaway test DB — same approach test_session_pubsub.py
    # takes for its own fixtures, and avoids FK-order whack-a-mole across every table a
    # principal can touch (agent_sessions, channel_messages, grants, ...).
    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.agent_inbox WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.channel_messages WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.channel_subscriptions WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.agent_sessions WHERE namespace=%s", (NS,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
