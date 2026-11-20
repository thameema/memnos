"""End-to-end governance, channel management, and SSE tests for the MEMNOS Workspace App
(`/app`, `/workspace/api/*`, `/workspace/events`).

Verifies:
1. `/app` and `/admin` serve the Slack-style Workspace UI with clean open-source placeholders.
2. Role-based token scoping (`admin` vs `read+write` vs `read-only` vs unauthorized namespaces).
3. Channel creation, message & claimable-task publishing, handle subscribe/unregister, and
   channel deletion through `/workspace/api/*`.
4. Real-time Server-Sent Events (`/workspace/events`) wake immediately on `NOTIFY`.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS_RW = "test:ws-rw"
NS_RO = "test:ws-ro"
NS_HIDDEN = "test:ws-hidden"
PASS = FAIL = 0


def call(method, path, token=None, body=None):
    req = urllib.request.Request(
        URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + token} if token else {}),
        },
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    PASS += bool(cond)
    FAIL += not bool(cond)


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    admin_id = Control.create_principal(conn, "ws-test-admin", "service")
    Control.grant(conn, admin_id, "*")
    admin_tok = Control.mint_token(conn, admin_id, "ws-admin")

    scoped_id = Control.create_principal(conn, "ws-test-scoped", "user")
    Control.grant(conn, scoped_id, NS_RW, can_read=True, can_write=True)
    Control.grant(conn, scoped_id, NS_RO, can_read=True, can_write=False)
    scoped_tok = Control.mint_token(conn, scoped_id, "ws-scoped")

    try:
        call("POST", "/admin/api/namespaces", admin_tok, {"name": NS_RW, "description": "rw scope"})
        call("POST", "/admin/api/namespaces", admin_tok, {"name": NS_RO, "description": "ro scope"})
        call("POST", "/admin/api/namespaces", admin_tok, {"name": NS_HIDDEN, "description": "secret scope"})

        print("=== 1. UI HTML endpoints (/app and /admin) ===")
        app_html = urllib.request.urlopen(URL + "/app", timeout=5).read().decode()
        adm_html = urllib.request.urlopen(URL + "/admin", timeout=5).read().decode()
        check("/app serves MEMNOS Workspace HTML", "MEMNOS · Workspace" in app_html)
        check("/admin preserves generic repo placeholder", "github.com/acme/payments-sync" in adm_html)

        print("=== 2. Role-based token scoping ===")
        check("unauthenticated -> 401", call("GET", "/workspace/api/overview")[0] == 401)

        s_adm, ov_adm = call("GET", "/workspace/api/overview", admin_tok)
        check("admin overview -> 200 & is_admin=True", s_adm == 200 and ov_adm.get("is_admin") is True)
        check("admin vitals expose db_size_bytes", ov_adm.get("vitals", {}).get("db_size_bytes") is not None)

        s_sc, ov_sc = call("GET", "/workspace/api/overview", scoped_tok)
        sc_ns = {n["name"]: n["can_write"] for n in ov_sc.get("namespaces", [])}
        check("scoped overview -> 200 & is_admin=False", s_sc == 200 and ov_sc.get("is_admin") is False)
        check("scoped token sees RW + RO but NOT hidden namespace",
              sc_ns.get(NS_RW) is True and sc_ns.get(NS_RO) is False and NS_HIDDEN not in sc_ns,
              str(sc_ns))
        check("scoped token hides global db_size_bytes", ov_sc.get("vitals", {}).get("db_size_bytes") is None)
        check("scoped token -> 403 on hidden namespace",
              call("GET", f"/workspace/api/overview?namespace={NS_HIDDEN}", scoped_tok)[0] == 403)
        check("scoped token -> 403 creating channel in read-only namespace",
              call("POST", "/workspace/api/channel/create", scoped_tok,
                   {"namespace": NS_RO, "channel": "#forbidden"})[0] == 403)

        print("=== 3. Channel creation, handle subscribe/unregister, publish, and channel delete ===")
        st, _ = call("POST", "/workspace/api/channel/create", scoped_tok,
                     {"namespace": NS_RW, "channel": "#arch-sync", "description": "Architecture sync"})
        check("create #arch-sync in writable namespace -> 200", st == 200)

        st1, _ = call("POST", "/workspace/api/channel/subscribe", scoped_tok,
                      {"namespace": NS_RW, "channel": "#arch-sync", "session_name": "worker-alpha"})
        st2, _ = call("POST", "/workspace/api/channel/subscribe", scoped_tok,
                      {"namespace": NS_RW, "channel": "#arch-sync", "session_name": "worker-beta"})
        check("subscribe @worker-alpha and @worker-beta -> 200", st1 == 200 and st2 == 200)

        # Start SSE listener before publishing to verify real-time push
        sse_events = []

        def listen_sse():
            req = urllib.request.Request(f"{URL}/workspace/events?token={scoped_tok}")
            with urllib.request.urlopen(req, timeout=6) as resp:
                for _ in range(8):
                    line = resp.readline().decode().strip()
                    if line.startswith("data:"):
                        ev = json.loads(line[5:].strip())
                        sse_events.append(ev.get("type"))
                        if "refresh" in sse_events:
                            break

        t = threading.Thread(target=listen_sse, daemon=True)
        t.start()
        time.sleep(0.25)

        sp, _ = call("POST", "/workspace/api/channel/publish", scoped_tok, {
            "namespace": NS_RW,
            "channel": "#arch-sync",
            "sender_session": "owner",
            "content": "Verify deployment pipeline",
            "claimable": True,
        })
        check("publish claimable task to #arch-sync -> 200", sp == 200)
        t.join(timeout=3.0)
        check("SSE /workspace/events pushed connected + refresh in real time",
              "connected" in sse_events and "refresh" in sse_events, str(sse_events))

        sm, mdata = call("GET", f"/workspace/api/channel/messages?namespace={NS_RW}&channel=%23arch-sync", scoped_tok)
        msgs = mdata.get("messages", [])
        check("channel/messages returns published claimable task",
              sm == 200 and len(msgs) == 1 and msgs[0]["claimable"] is True)

        su, _ = call("POST", "/workspace/api/channel/unsubscribe", scoped_tok,
                     {"namespace": NS_RW, "channel": "#arch-sync", "session_name": "worker-beta"})
        _, ov2 = call("GET", f"/workspace/api/overview?namespace={NS_RW}", scoped_tok)
        ch_obj = next((c for c in ov2.get("channels", []) if c["channel"] == "#arch-sync"), {})
        check("unregister @worker-beta keeps @worker-alpha only",
              su == 200 and ch_obj.get("subscribers") == ["worker-alpha"], str(ch_obj))

        sd, del_res = call("DELETE", f"/workspace/api/channel?namespace={NS_RW}&channel=%23arch-sync", scoped_tok)
        _, ov3 = call("GET", f"/workspace/api/overview?namespace={NS_RW}", scoped_tok)
        remaining = [c["channel"] for c in ov3.get("channels", [])]
        check("DELETE /workspace/api/channel removes channel, subs, and history",
              sd == 200 and "#arch-sync" not in remaining and del_res.get("deleted_messages") == 1,
              str(del_res))

    finally:
        for ns in (NS_RW, NS_RO, NS_HIDDEN):
            call("DELETE", f"/admin/api/namespaces?name={ns}&purge=1", admin_tok)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memnos_control.channel_subscriptions WHERE principal_id = ANY(%s)", ([admin_id, scoped_id],))
            cur.execute("DELETE FROM memnos_control.agent_sessions WHERE principal_id = ANY(%s)", ([admin_id, scoped_id],))
            cur.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id = ANY(%s)", ([admin_id, scoped_id],))
            cur.execute("DELETE FROM memnos_control.grants WHERE principal_id = ANY(%s)", ([admin_id, scoped_id],))
            cur.execute("DELETE FROM memnos_control.audit_log WHERE principal_id = ANY(%s)", ([admin_id, scoped_id],))
            cur.execute("DELETE FROM memnos_control.principals WHERE id = ANY(%s)", ([admin_id, scoped_id],))

    print(f"\nPassed: {PASS}, Failed: {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
