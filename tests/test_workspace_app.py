"""End-to-end governance, channel management, and SSE tests for the MEMNOS Workspace App
(`/app`, `/workspace/api/*`, `/workspace/events`).

Verifies:
1. `/app` and `/admin` serve the Slack-style Workspace UI with clean open-source placeholders.
2. Role-based token scoping (`admin` vs `read+write` vs `read-only` vs unauthorized namespaces).
3. Channel creation, message & claimable-task publishing, handle subscribe/unregister, and
   channel deletion through `/workspace/api/*`.
4. Real-time Server-Sent Events (`/workspace/events`) wake immediately on `NOTIFY`.
"""
import base64
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
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

        print("=== 1. Headless server JSON status + Web UI completely removed (/app & /admin -> 404) ===")
        root_status, root_json = call("GET", "/")
        app_status, _ = call("GET", "/app")
        adm_status, _ = call("GET", "/admin")
        js_status, _ = call("GET", "/admin/app.js")
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        check("GET / returns JSON service metadata (no HTML web console)",
              root_status == 200
              and root_json.get("ok") is True
              and root_json.get("service") == "memnos"
              and bool(root_json.get("version")))
        check("Legacy Web UI routes (/app, /admin, /admin/app.js) return 404 and ui/ directory is removed",
              app_status == 404
              and adm_status == 404
              and js_status == 404
              and not os.path.exists(os.path.join(repo_root, "ui", "index.html")))
        health_json = json.loads(urllib.request.urlopen(URL + "/healthz", timeout=5).read().decode())
        check("/healthz exposes server version and build SHA",
              bool(health_json.get("version")) and "build" in health_json)

        # CLI first-time admin onboarding & token listing/revealing verification
        import subprocess
        memnos_bin = os.path.join(repo_root, "target", "release", "memnos")
        desktop_bin = os.path.join(repo_root, "target", "release", "memnos-desktop")
        if os.path.isfile(memnos_bin):
            tok_ls = subprocess.run(
                [memnos_bin, "token", "ls", "--reveal", "--json"],
                capture_output=True, text=True, timeout=15,
                env={**os.environ, "MEMNOS_DSN": DSN},
            )
            ls_arr = json.loads(tok_ls.stdout or "[]") if tok_ls.returncode == 0 else []
            check("CLI `memnos token ls --reveal --json` lists tokens across all principals with decrypted mnk_ tokens",
                  tok_ls.returncode == 0
                  and any(t.get("principal_name") == "ws-test-admin" and str(t.get("token", "")).startswith("mnk_") for t in ls_arr))
        if os.path.isfile(desktop_bin):
            dv = subprocess.run([desktop_bin, "--version"], capture_output=True, text=True, timeout=10)
            check("Single-process `memnos-desktop --version` matches server version",
                  dv.returncode == 0 and health_json["version"] in dv.stdout)

        print("=== 2. Role-based token scoping ===")
        check("unauthenticated -> 401", call("GET", "/workspace/api/overview")[0] == 401)

        s_adm, ov_adm = call("GET", "/workspace/api/overview", admin_tok)
        check("admin overview -> 200 & is_admin=True", s_adm == 200 and ov_adm.get("is_admin") is True)
        check("admin vitals expose db_size_bytes + version + build",
              ov_adm.get("vitals", {}).get("db_size_bytes") is not None
              and bool(ov_adm.get("vitals", {}).get("version"))
              and "build" in ov_adm.get("vitals", {}))

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

        # Peer-to-peer inbox_send between @worker-alpha and @worker-beta (both subscribed to #arch-sync)
        call("POST", "/inbox/send", scoped_tok, {
            "namespace": NS_RW,
            "from_session": "worker-alpha",
            "to_session": "worker-beta",
            "intent": "review_request",
            "content": "Please review PR #42",
        })
        _, mdata_peer = call("GET", f"/workspace/api/channel/messages?namespace={NS_RW}&channel=%23arch-sync", scoped_tok)
        peer_msgs = mdata_peer.get("messages", [])
        check("channel/messages includes peer_dm tasks between subscribed handles of #arch-sync",
              len(peer_msgs) == 2 and any(m.get("source") == "peer_dm" and m.get("to_session") == "worker-beta" for m in peer_msgs),
              str(peer_msgs))

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

        print("=== 4. Unique #channel across multiple namespaces (zero duplicates) ===")
        call("POST", "/workspace/api/channel/publish", admin_tok,
             {"namespace": NS_RW, "channel": "#shared-sync", "sender_session": "agent-a", "content": "msg from rw"})
        call("POST", "/workspace/api/channel/publish", admin_tok,
             {"namespace": NS_RO, "channel": "#shared-sync", "sender_session": "agent-b", "content": "msg from ro"})
        call("POST", "/workspace/api/channel/publish", admin_tok,
             {"namespace": NS_HIDDEN, "channel": "#shared-sync", "sender_session": "agent-c", "content": "secret msg"})

        _, ov_uni = call("GET", "/workspace/api/overview?namespace=*", scoped_tok)
        shared_rows = [c for c in ov_uni.get("channels", []) if c["channel"] == "#shared-sync"]
        check("#shared-sync appears ONCE (unique across namespaces) with merged count=2 (hidden excluded)",
              len(shared_rows) == 1 and shared_rows[0]["message_count"] == 2,
              str(shared_rows))

        _, sm_uni = call("GET", f"/workspace/api/channel/messages?namespace={NS_RW}&channel=%23shared-sync", scoped_tok)
        uni_texts = [m["content"] for m in sm_uni.get("messages", [])]
        check("channel/messages merges authorized namespaces chronologically without leaking hidden",
              uni_texts == ["msg from rw", "msg from ro"], str(uni_texts))
        call("DELETE", "/workspace/api/channel?namespace=*&channel=%23shared-sync", admin_tok)

        print("=== 5. Live activity SSE on regular memnos API calls + /workspace/api/activity ===")
        live_ops = []

        def listen_live_op():
            req = urllib.request.Request(f"{URL}/workspace/events?token={scoped_tok}")
            with urllib.request.urlopen(req, timeout=6) as resp:
                for _ in range(10):
                    line = resp.readline().decode().strip()
                    if line.startswith("data:"):
                        ev = json.loads(line[5:].strip())
                        if ev.get("type") == "activity":
                            live_ops.append(ev.get("op"))
                            break

        tl = threading.Thread(target=listen_live_op, daemon=True)
        tl.start()
        time.sleep(0.25)
        sr, _ = call("POST", "/session/register", scoped_tok,
                     {"namespace": NS_RW, "name": "live-probe", "harness": "antigravity", "channels": ["#general"]})
        tl.join(timeout=3.0)
        check("regular /session/register call streams live activity SSE in real time",
              sr == 200 and "session/register" in live_ops, str(live_ops))
        sa, act_body = call("GET", "/workspace/api/activity?limit=20", scoped_tok)
        check("/workspace/api/activity returns authorized recent operations",
              sa == 200 and any(e.get("op") == "session/register" for e in act_body.get("events", [])),
              str(act_body.get("events", [])[:3]))

        print("=== 6. DELETE /admin/api/principals (FK-safe delete + self-delete guard) ===")
        _, cp = call("POST", "/admin/api/principals", admin_tok, {"name": f"test-del-pr-{int(time.time())}", "kind": "agent"})
        del_pid = cp.get("id")
        _, mt = call("POST", "/admin/api/tokens", admin_tok, {"principal_id": del_pid, "label": "tmp"})
        del_tok = mt.get("token")
        call("POST", "/admin/api/grants", admin_tok, {"principal_id": del_pid, "namespace": NS_RW, "can_read": True, "can_write": True})
        call("POST", "/session/register", del_tok, {"namespace": NS_RW, "name": "del-sess"})
        call("POST", "/inbox/send", del_tok, {"namespace": NS_RW, "to_session": "del-sess", "from_session": "del-sess", "content": "hi"})

        s_self, _ = call("DELETE", f"/admin/api/principals?id={admin_id}", admin_tok)
        check("DELETE /admin/api/principals refuses self-deletion -> 400", s_self == 400, str(s_self))
        s_nonadm, _ = call("DELETE", f"/admin/api/principals?id={del_pid}", scoped_tok)
        check("non-admin cannot delete principal -> 403", s_nonadm == 403, str(s_nonadm))
        s_del, del_body = call("DELETE", f"/admin/api/principals?id={del_pid}", admin_tok)
        _, p_after = call("GET", "/admin/api/principals", admin_tok)
        remaining_ids = [p["id"] for p in p_after.get("principals", [])]
        check("DELETE /admin/api/principals deletes principal + tokens + grants + sessions + inbox cleanly",
              s_del == 200 and del_body.get("ok") is True and del_pid not in remaining_ids,
              str(del_body))

        print("=== 7. Harness metadata, Constraints validator, All-principals tokens + reveal, and two-way WebSocket ===")
        _, ov_h = call("GET", f"/workspace/api/overview?namespace={NS_RW}", scoped_tok)
        probe_sess = next((s for s in ov_h.get("sessions", []) if s["name"] == "live-probe"), {})
        check("session overview exposes attached harness ('antigravity')",
              probe_sess.get("harness") == "antigravity", str(probe_sess))

        # Constraints creation, validation, and scope isolation
        sc_add, enf_res = call("POST", "/workspace/api/constraints/enforcement", scoped_tok, {
            "namespace": NS_RW,
            "enforce_level": "block",
            "tool_matcher": "Bash(rm -rf*)",
            "rule_text": "Never run recursive rm without confirmation",
        })
        enf_id = enf_res.get("id")
        call("POST", "/workspace/api/constraints/enforcement", admin_tok, {
            "namespace": NS_HIDDEN,
            "enforce_level": "block",
            "tool_matcher": "Bash(secret*)",
            "rule_text": "Hidden secret rule",
        })
        _, cs_list = call("GET", "/workspace/api/constraints?namespace=*", scoped_tok)
        cs_nss = {r["namespace"] for r in cs_list.get("enforcement_rules", [])}
        check("GET /workspace/api/constraints returns authorized guardrails without leaking hidden",
              sc_add == 200 and NS_RW in cs_nss and NS_HIDDEN not in cs_nss, str(cs_nss))

        _, val_blk = call("POST", "/workspace/api/constraints/validate", scoped_tok,
                          {"namespace": NS_RW, "tool_subject": "Bash(rm -rf /tmp/x)"})
        _, val_ok = call("POST", "/workspace/api/constraints/validate", scoped_tok,
                         {"namespace": NS_RW, "tool_subject": "Bash(ls -la)"})
        check("POST /workspace/api/constraints/validate evaluates block vs allow accurately",
              val_blk.get("tool_validation", {}).get("decision") == "block"
              and val_ok.get("tool_validation", {}).get("decision") == "allow",
              f"blk={val_blk} ok={val_ok}")
        call("DELETE", f"/workspace/api/constraints/enforcement?id={enf_id}", scoped_tok)

        # Admin all-principals token listing + AES-256-GCM reveal
        _, m_rev = call("POST", "/admin/api/tokens", admin_tok, {"principal_id": scoped_id, "label": "recoverable-tok"})
        plain_tok = m_rev.get("token", "")
        _, all_toks = call("GET", "/admin/api/tokens?principal=0", admin_tok)
        tok_row = next((t for t in all_toks.get("tokens", []) if t.get("label") == "recoverable-tok"), {})
        check("GET /admin/api/tokens?principal=0 lists across all principals with principal_name + recoverable=True (no raw secret)",
              tok_row.get("principal_name") == "ws-test-scoped"
              and tok_row.get("recoverable") is True
              and "token" not in tok_row,
              str(tok_row))
        s_rev_forbid, _ = call("GET", f"/admin/api/tokens/reveal?id={tok_row.get('id', 0)}", scoped_tok)
        s_rev_ok, rev_body = call("GET", f"/admin/api/tokens/reveal?id={tok_row.get('id', 0)}", admin_tok)
        check("GET /admin/api/tokens/reveal decrypts token for admin and forbids non-admin",
              s_rev_forbid == 403 and s_rev_ok == 200 and rev_body.get("token") == plain_tok,
              str(rev_body))

        # Two-way WebSocket (/workspace/ws) RFC 6455 handshake + ping/pong
        u = urllib.parse.urlparse(URL)
        sock = socket.create_connection((u.hostname or "127.0.0.1", u.port or 8900), timeout=5)
        ws_key = base64.b64encode(os.urandom(16)).decode()
        hs = (
            f"GET /workspace/ws?token={scoped_tok} HTTP/1.1\r\n"
            f"Host: {u.netloc}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(hs.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += sock.recv(4096)
        hdr_part, rest = buf.split(b"\r\n\r\n", 1)

        def read_ws_text(sk, initial):
            data = initial
            while len(data) < 2:
                data += sk.recv(4096)
            plen = data[1] & 0x7F
            idx = 2
            if plen == 126:
                while len(data) < 4:
                    data += sk.recv(4096)
                plen = int.from_bytes(data[2:4], "big")
                idx = 4
            while len(data) < idx + plen:
                data += sk.recv(4096)
            return json.loads(data[idx:idx + plen].decode()), data[idx + plen:]

        ws_hello, rest = read_ws_text(sock, rest)
        # Send masked client text frame: {"type":"ping","req_id":77}
        ping_bytes = json.dumps({"type": "ping", "req_id": 77}).encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(ping_bytes))
        sock.sendall(bytes([0x81, 0x80 | len(ping_bytes)]) + mask + masked)
        ws_pong, _ = read_ws_text(sock, rest)
        sock.close()
        check("Two-way WebSocket /workspace/ws upgrades (101) and handles full-duplex ping/pong",
              b"101" in hdr_part.splitlines()[0]
              and ws_hello.get("type") == "connected"
              and ws_hello.get("transport") == "websocket"
              and ws_pong.get("type") == "pong"
              and ws_pong.get("req_id") == 77,
              f"hello={ws_hello} pong={ws_pong}")

        print("=== 8. Headless Spawner Status, PRD Task DAG depends_on, Subscription Quota Failover & Cross-Harness Review ===")
        sp_code, sp_st = call("GET", "/workspace/api/spawner/status", scoped_tok)
        h_keys = {h["key"] for h in sp_st.get("harnesses", [])}
        check("GET /workspace/api/spawner/status discovers harnesses & concurrency caps",
              sp_code == 200 and {"claude-code", "antigravity", "codex", "gemini", "cursor"}.issubset(h_keys)
              and sp_st.get("max_concurrent", 0) >= 1,
              str(sp_st))

        # Register multi-harness worker sessions on #prd-dag
        for s_name, s_harness in [("dag-claude", "claude-code"), ("dag-claude-peer", "claude-code"), ("dag-agy", "antigravity")]:
            call("POST", "/session/register", scoped_tok, {
                "namespace": NS_RW,
                "name": s_name,
                "harness": s_harness,
                "channels": ["#prd-dag"],
            })

        # Publish Task A (prefer claude-code) and Task B (depends_on: [task_a_id])
        _, pub_a = call("POST", "/workspace/api/channel/publish", scoped_tok, {
            "namespace": NS_RW,
            "channel": "#prd-dag",
            "sender_session": "owner",
            "content": "Task A: Implement billing schema",
            "claimable": True,
            "intent": "task",
            "metadata": {"preferred_harness": "claude-code"},
        })
        task_a_id = pub_a["id"]

        _, pub_b = call("POST", "/workspace/api/channel/publish", scoped_tok, {
            "namespace": NS_RW,
            "channel": "#prd-dag",
            "sender_session": "owner",
            "content": "Task B: Wire API endpoints after schema",
            "claimable": True,
            "intent": "task",
            "metadata": {"depends_on": [task_a_id]},
        })
        task_b_id = pub_b["id"]

        # Non-preferred harness (dag-agy) yields Task A while preferred claude-code is online, AND cannot claim blocked Task B
        _, poll_agy_early = call("POST", "/channel/poll", scoped_tok, {
            "namespace": NS_RW, "session_name": "dag-agy", "channel": "#prd-dag"
        })
        claimed_early = [m["id"] for m in poll_agy_early.get("messages", []) if m.get("claimed_by") == "dag-agy"]
        check("preferred_harness + depends_on gate non-preferred & downstream tasks accurately",
              claimed_early == [], str(poll_agy_early))

        # Preferred harness (dag-claude) claims Task A (still cannot claim blocked Task B)
        _, poll_claude = call("POST", "/channel/poll", scoped_tok, {
            "namespace": NS_RW, "session_name": "dag-claude", "channel": "#prd-dag"
        })
        claude_claimed = [m["id"] for m in poll_claude.get("messages", []) if m.get("claimed_by") == "dag-claude"]
        check("preferred harness ('dag-claude') claims Task A while Task B stays gated on depends_on",
              claude_claimed == [task_a_id], str(poll_claude))

        # Simulate subscription quota limit on dag-claude -> releases Task A for automatic failover!
        q_code, q_res = call("POST", "/workspace/api/session/quota", scoped_tok, {
            "namespace": NS_RW, "session": "dag-claude", "paused": True, "cooldown_minutes": 60, "reason": "rate_limit_error"
        })
        # Also pause dag-claude-peer so preferred claude-code has 0 available capacity -> fallback unlocks!
        call("POST", "/workspace/api/session/quota", scoped_tok, {
            "namespace": NS_RW, "session": "dag-claude-peer", "paused": True, "cooldown_minutes": 60, "reason": "rate_limit_error"
        })
        _, poll_paused = call("POST", "/channel/poll", scoped_tok, {
            "namespace": NS_RW, "session_name": "dag-claude", "channel": "#prd-dag"
        })
        check("POST /workspace/api/session/quota pauses handle, blocks polling, and releases in-progress task",
              q_code == 200 and q_res.get("quota_paused") is True and q_res.get("released_tasks") == 1 and poll_paused.get("count") == 0,
              f"q={q_res} poll={poll_paused}")

        # Failover harness (dag-agy) now claims released Task A (with handoff_from metadata) & completes it!
        _, poll_failover = call("POST", "/channel/poll", scoped_tok, {
            "namespace": NS_RW, "session_name": "dag-agy", "channel": "#prd-dag"
        })
        agy_failover_msgs = [m for m in poll_failover.get("messages", []) if m.get("id") == task_a_id and m.get("claimed_by") == "dag-agy"]
        check("failover harness ('dag-agy') claims released Task A when preferred harness is quota_paused",
              len(agy_failover_msgs) == 1 and agy_failover_msgs[0].get("metadata", {}).get("handoff_from") == "dag-claude",
              str(poll_failover))

        call("POST", "/channel/ack", scoped_tok, {
            "namespace": NS_RW, "session_name": "dag-agy", "id": task_a_id, "status": "done", "resolution": "schema migrated"
        })

        # Now that Task A is 'done', Task B unlocks immediately on next poll!
        _, poll_unblocked = call("POST", "/channel/poll", scoped_tok, {
            "namespace": NS_RW, "session_name": "dag-agy", "channel": "#prd-dag"
        })
        unblocked_ids = [m["id"] for m in poll_unblocked.get("messages", []) if m.get("claimed_by") == "dag-agy"]
        check("downstream Task B unlocks immediately once upstream depends_on Task A is done",
              task_b_id in unblocked_ids, str(poll_unblocked))
        call("POST", "/channel/ack", scoped_tok, {
            "namespace": NS_RW, "session_name": "dag-agy", "id": task_b_id, "status": "done", "resolution": "endpoints wired"
        })

        # Resume Claude sessions and test cross-harness code review (`different_harness: True`)
        for s_name in ("dag-claude", "dag-claude-peer"):
            call("POST", "/workspace/api/session/quota", scoped_tok, {
                "namespace": NS_RW, "session": s_name, "paused": False
            })
        _, pub_rev = call("POST", "/workspace/api/channel/publish", scoped_tok, {
            "namespace": NS_RW,
            "channel": "#prd-dag",
            "sender_session": "dag-claude",
            "content": "Cross-harness review of billing PR",
            "claimable": True,
            "intent": "review_request",
            "metadata": {"different_harness": True},
        })
        rev_id = pub_rev["id"]
        _, poll_same_h = call("POST", "/channel/poll", scoped_tok, {
            "namespace": NS_RW, "session_name": "dag-claude-peer", "channel": "#prd-dag"
        })
        same_h_claimed = [m["id"] for m in poll_same_h.get("messages", []) if m.get("claimed_by") == "dag-claude-peer"]
        _, poll_diff_h = call("POST", "/channel/poll", scoped_tok, {
            "namespace": NS_RW, "session_name": "dag-agy", "channel": "#prd-dag"
        })
        diff_h_claimed = [m["id"] for m in poll_diff_h.get("messages", []) if m.get("claimed_by") == "dag-agy"]
        check("different_harness=True prevents same-harness peer claim and routes review to alternate harness",
              rev_id not in same_h_claimed and rev_id in diff_h_claimed,
              f"same={same_h_claimed} diff={diff_h_claimed}")

        # Security & fail-closed checks (C1 admin-only spawner, unbound cwd guard, H5 quota ownership, M4 fail-closed depends_on)
        sp_forbid_code, _ = call("POST", "/workspace/api/spawner/launch", scoped_tok, {
            "namespace": NS_RW, "harness": "claude-code", "prompt": "noop"
        })
        check("non-admin principal is forbidden (403) from launching local headless workers",
              sp_forbid_code == 403, f"code={sp_forbid_code}")

        sp_cwd_code, sp_cwd_err = call("POST", "/workspace/api/spawner/launch", admin_tok, {
            "namespace": NS_RW, "harness": "claude-code", "cwd": "/tmp", "prompt": "noop"
        })
        check("spawner rejects unbound arbitrary cwd ('/tmp') -> 400",
              sp_cwd_code == 400 and "not bound" in sp_cwd_err.get("error", ""),
              str(sp_cwd_err))

        # Admin registers an admin-owned session in NS_RW; non-admin writer (`scoped_tok`) must get 403 trying to quota-pause it
        call("POST", "/session/register", admin_tok, {
            "namespace": NS_RW, "name": "admin-owned-sess", "harness": "antigravity"
        })
        q_hijack_code, _ = call("POST", "/workspace/api/session/quota", scoped_tok, {
            "namespace": NS_RW, "session": "admin-owned-sess", "paused": True
        })
        check("non-owner writer cannot quota-pause another principal's session (403)",
              q_hijack_code == 403, f"code={q_hijack_code}")

        # Fail-closed depends_on: nonexistent dependency ID must stay gated (never fail open!)
        _, pub_missing_dep = call("POST", "/workspace/api/channel/publish", scoped_tok, {
            "namespace": NS_RW,
            "channel": "#prd-dag",
            "sender_session": "owner",
            "content": "Task with nonexistent dependency",
            "claimable": True,
            "intent": "task",
            "metadata": {"depends_on": [999999999]},
        })
        _, poll_missing_dep = call("POST", "/channel/poll", scoped_tok, {
            "namespace": NS_RW, "session_name": "dag-agy", "channel": "#prd-dag"
        })
        missing_claimed = [m["id"] for m in poll_missing_dep.get("messages", []) if m.get("id") == pub_missing_dep["id"]]
        check("nonexistent depends_on ID fails closed (stays gated, never claimed)",
              missing_claimed == [], str(poll_missing_dep))

        print("=== 9. Namespaces Workspace: Create, Related Graph, Links, Bindings & Atomic Merge ===")
        ns_src = "test:ws-merge-src"
        ns_dst = "test:ws-merge-dst"
        ns_child = f"{NS_RW}:api"

        # Scoped user without grant on ns_src cannot create arbitrary root namespace -> 403
        st_forb_create, _ = call("POST", "/workspace/api/namespace/create", scoped_tok, {
            "name": ns_src, "description": "unauthorized root"
        })
        check("scoped token cannot create ungranted namespace -> 403", st_forb_create == 403)

        # Admin creates ns_src + child namespace with link & repo binding
        st_c1, _ = call("POST", "/workspace/api/namespace/create", admin_tok, {
            "name": ns_src,
            "description": "Legacy source scope",
            "kind": "memory",
            "bind_key": "github.com/acme/legacy-payments",
            "bind_key_type": "repo",
        })
        st_c2, _ = call("POST", "/workspace/api/namespace/create", admin_tok, {
            "name": ns_child,
            "description": "API child scope",
            "kind": "knowledge",
            "link_to": NS_RO,
            "link_kind": "link",
        })
        check("POST /workspace/api/namespace/create creates namespaces + link + repo binding -> 200",
              st_c1 == 200 and st_c2 == 200)

        # Cycle detection on namespace/link
        st_cyc1, _ = call("POST", "/workspace/api/namespace/link", admin_tok, {
            "src": NS_RO, "dst": ns_child, "kind": "link"
        })
        check("POST /workspace/api/namespace/link rejects cycle -> 400", st_cyc1 == 400)

        # Scoped token cannot link to hidden namespace
        st_hid_link, _ = call("POST", "/workspace/api/namespace/link", scoped_tok, {
            "src": NS_RW, "dst": NS_HIDDEN, "kind": "link"
        })
        check("scoped token cannot link to unauthorized hidden namespace -> 403", st_hid_link == 403)

        # Inspect detail on child namespace -> ancestors + links_out + kind
        st_det, det = call("GET", f"/workspace/api/namespace/detail?namespace={urllib.parse.quote(ns_child)}", admin_tok)
        check("GET /workspace/api/namespace/detail returns ancestors, outgoing links, and kind",
              st_det == 200
              and det.get("kind") == "knowledge"
              and NS_RW in det.get("related", {}).get("ancestors", [])
              and any(l.get("dst_ns") == NS_RO for l in det.get("related", {}).get("links_out", [])),
              str(det))

        # Seed coordination + memory data in ns_src, preview dry-run, then atomic merge into ns_dst
        call("POST", "/workspace/api/channel/create", admin_tok, {
            "namespace": ns_src, "channel": "#legacy-sync", "description": "Legacy sync"
        })
        call("POST", "/workspace/api/channel/publish", admin_tok, {
            "namespace": ns_src, "channel": "#legacy-sync", "sender_session": "owner", "content": "Migrate this thread"
        })
        call("POST", "/inbox/send", admin_tok, {
            "namespace": ns_src, "from_session": "owner", "to_session": "mig-worker", "intent": "task", "content": "Follow up in merged ns"
        })
        call("POST", "/remember", admin_tok, {
            "namespace": ns_src, "text": "Payments service uses idempotency keys on all retries.", "extract": False
        })

        st_prev, prev = call("POST", "/workspace/api/namespace/transfer", admin_tok, {
            "src": ns_src, "dst": ns_dst, "mode": "merge", "dry_run": True
        })
        pc = prev.get("counts", {})
        check("POST /workspace/api/namespace/transfer (dry_run=True) previews accurate counts",
              st_prev == 200
              and pc.get("raw_turns", 0) >= 1
              and pc.get("channel_messages", 0) == 1
              and pc.get("inbox_messages", 0) == 1
              and pc.get("bindings", 0) == 1,
              str(prev))

        st_mrg, mrg = call("POST", "/workspace/api/namespace/transfer", admin_tok, {
            "src": ns_src,
            "dst": ns_dst,
            "mode": "merge",
            "dry_run": False,
            "include_memories": True,
            "include_coordination": True,
            "include_routing": True,
            "delete_source": True,
        })
        _, dst_det = call("GET", f"/workspace/api/namespace/detail?namespace={urllib.parse.quote(ns_dst)}", admin_tok)
        dc = dst_det.get("counts", {})
        check("atomic merge moves memories, #channels, @inbox, and repo bindings into target & deletes source",
              st_mrg == 200
              and mrg.get("source_deleted") is True
              and dc.get("turns", 0) >= 1
              and dc.get("channel_messages", 0) == 1
              and dc.get("inbox_messages", 0) == 1
              and dc.get("bindings", 0) == 1,
              str(dst_det))

        print("=== 10. MEMNOS Doctor: Diagnostics Scan, Role Guards, Stuck-Lock Repair & VACUUM ANALYZE ===")
        st_doc_forb, _ = call("GET", f"/workspace/api/doctor?namespace={NS_HIDDEN}", scoped_tok)
        check("scoped token cannot scan hidden namespace in doctor -> 403", st_doc_forb == 403)

        st_opt_forb, _ = call("POST", "/workspace/api/doctor/run", scoped_tok, {
            "actions": ["optimize_db"]
        })
        check("scoped token cannot run admin optimize_db -> 403", st_opt_forb == 403)

        # Seed a stuck claimable task + expired file lease in NS_RW
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memnos_control.channel_messages(namespace, channel, sender_session, principal_id, intent, content, claimable, claim_status, claimed_by, claimed_by_ns, claim_lease_until) "
                "VALUES (%s, '#doc-test', 'owner', %s, 'task', 'Stuck task', true, 'claimed', 'dead-worker', %s, now() - interval '15 minutes')",
                (NS_RW, admin_id, NS_RW),
            )
            cur.execute(
                "INSERT INTO memnos_control.leases(namespace, key, holder_id, principal_id, expires_at) "
                "VALUES (%s, 'src/payments.rs', 'dead-worker', %s, now() - interval '15 minutes')",
                (NS_RW, admin_id),
            )

        st_dscan, dscan = call("GET", f"/workspace/api/doctor?namespace={urllib.parse.quote(NS_RW)}", admin_tok)
        coord_chk = next((c for c in dscan.get("checks", []) if c.get("id") == "coordination_hygiene"), {})
        check("GET /workspace/api/doctor detects stuck task claims and expired file leases",
              st_dscan == 200
              and isinstance(dscan.get("score"), int)
              and coord_chk.get("metrics", {}).get("stuck_claims") == 1
              and coord_chk.get("metrics", {}).get("expired_leases") == 1,
              str(coord_chk))

        st_drun, drun = call("POST", "/workspace/api/doctor/run", admin_tok, {
            "namespace": NS_RW,
            "actions": ["repair_coordination", "repair_graph", "optimize_memories", "optimize_db"],
            "dry_run": False,
        })
        rc_res = drun.get("results", {}).get("repair_coordination", {})
        db_res = drun.get("results", {}).get("optimize_db", {})
        post_coord = next((c for c in drun.get("scan", {}).get("checks", []) if c.get("id") == "coordination_hygiene"), {})
        check("POST /workspace/api/doctor/run repairs stuck claims/leases & runs online VACUUM ANALYZE",
              st_drun == 200
              and rc_res.get("released_claims") == 1
              and rc_res.get("released_leases") == 1
              and (db_res.get("tables_vacuum_analyzed", 0) + db_res.get("tables_skipped_clean", 0)) >= 10
              and post_coord.get("metrics", {}).get("stuck_claims") == 0
              and post_coord.get("metrics", {}).get("expired_leases") == 0,
              str(drun))

    finally:
        for ns in (NS_RW, NS_RO, NS_HIDDEN, "test:ws-merge-src", "test:ws-merge-dst", f"{NS_RW}:api"):
            call("DELETE", f"/admin/api/namespaces?name={urllib.parse.quote(ns)}&purge=1", admin_tok)
        call("DELETE", f"/admin/api/principals?id={scoped_id}", admin_tok)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memnos_control.constraint_enforcement WHERE namespace = ANY(%s)", ([NS_RW, NS_RO, NS_HIDDEN],))
            cur.execute("UPDATE memnos_control.namespaces SET created_by = NULL WHERE created_by = %s", (admin_id,))
            cur.execute("UPDATE memnos_control.namespace_links SET created_by = NULL WHERE created_by = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.namespace_copy_provenance WHERE copied_by = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.bindings WHERE principal_id = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.leases WHERE principal_id = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.channel_messages WHERE principal_id = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.agent_inbox WHERE principal_id = %s", (admin_id,))
            cur.execute("UPDATE memnos_control.channel_catalog SET principal_id = NULL WHERE principal_id = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.channel_subscriptions WHERE principal_id = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.agent_sessions WHERE principal_id = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.grants WHERE principal_id = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.audit_log WHERE principal_id = %s", (admin_id,))
            cur.execute("DELETE FROM memnos_control.principals WHERE id = %s", (admin_id,))

    print(f"\nPassed: {PASS}, Failed: {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
