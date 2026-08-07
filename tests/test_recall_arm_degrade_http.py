"""End-to-end HTTP regression for issue #41 fix C: the actual client-facing contract the
issue describes -- "a single-arm statement_timeout cancellation yields a partial
degraded=true result, not a 5xx/exception -- client never sees 'memnos unavailable' for a
slow-but-up server."

tests/test_recall_arm_degrade.py already proves the underlying mechanism (recall_fetch /
recall_wide_fetch / recall_prefetch) in-process, including a REAL (non-mocked) Postgres
statement_timeout cancellation. This file proves the SAME real cancellation degrades
correctly all the way through the actual HTTP server process and its actual connection
pool -- the full path a real client sees, mirroring how #41 was originally reported (a
recall that looked like "memnos unavailable" while /healthz was 200 the whole time).

Owns its own memnos_server.py subprocess on a dedicated port (never touches a real :8900
instance), same pattern as test_mcp_http_mount_failure.py. MEMNOS_STMT_TIMEOUT_MS is set
low (1800ms) so the forced cancellation is fast without flaking on a cold DB -- it's set
via the connection-string `-c statement_timeout=...` option (the pool's normal
mechanism), not a bare session-level SET, so fts_clamp's own numnode() probe (which
resets statement_timeout to DEFAULT after every call) restores exactly this bound instead
of silently uncapping the query -- see test_recall_arm_degrade.py's module docstring for
why that distinction matters.

The forced failure: an ACCESS EXCLUSIVE lock on tenant_memnos.semantic, held open on a
second raw connection, blocks the recall's search_semantic query until the pool's
statement_timeout cancels it -- a genuine psycopg.errors.QueryCanceled, not a mock.
`constraint_cap: 0` in the request body skips pinned_constraints (memnos_server.py DB
phase A, which also reads {schema}.semantic) so the lock only collides with the ONE arm
under test, not an earlier, unrelated phase of the same request.

Run: python tests/test_recall_arm_degrade_http.py
(spawns its own server; does not require one already running)
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row

from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PORT = int(os.environ.get("MEMNOS_ARM_DEGRADE_HTTP_TEST_PORT", "8961"))
URL = f"http://127.0.0.1:{PORT}"
SCHEMA = "tenant_memnos"
NS = "test:armdegrade:http"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def call(path, token, body, timeout=30):
    req = urllib.request.Request(URL + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _server_env():
    env = dict(os.environ, MEMNOS_DSN=DSN, MEMNOS_PORT=str(PORT))
    env.setdefault("OPENAI_API_KEY", "")     # force free local-384 embeddings (no vault/network dependency)
    env.setdefault("MEMNOS_SECRET_KEY", "dGVzdF9vbmx5X2tleV8zMl9ieXRlc19leGFjdGx5ITE=")
    # issue #41 fix C: set via the connection-string DEFAULT (the pool's normal
    # mechanism, memnos_server.py's `-c statement_timeout=...`), not a session-level SET
    # -- see the module docstring above for why that distinction matters here.
    env["MEMNOS_STMT_TIMEOUT_MS"] = "1800"
    return env


def cleanup(conn):
    with conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS,))
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals p "
                  "WHERE t.principal_id=p.id AND p.name='armdegrade-http-bot'")
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals p "
                  "WHERE g.principal_id=p.id AND p.name='armdegrade-http-bot'")
        c.execute("DELETE FROM memnos_control.principals WHERE name='armdegrade-http-bot'")


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)

    print(f"=== booting a dedicated memnos_server.py on :{PORT} (MEMNOS_STMT_TIMEOUT_MS=1800) ===")
    proc = subprocess.Popen([sys.executable, "memnos_server.py"], cwd=ROOT, env=_server_env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(60):
            try:
                if urllib.request.urlopen(f"{URL}/healthz", timeout=2).status == 200:
                    up = True; break
            except Exception:
                pass
            time.sleep(1)
        check("dedicated server came up", up)
        if not up:
            print("server never became ready; aborting"); sys.exit(1)

        now_ts = None
        from datetime import datetime, timezone
        now_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def crafted_embed(text):
            v = [0.0] * 384
            v[0] = 1.0
            return v

        raw_text = "the outage started at nine and the incident channel opened immediately"
        sem_text = "the outage was caused by a bad config push"
        store.insert_raw_turn(SCHEMA, NS, None, "user", raw_text, now_ts, crafted_embed(raw_text))
        store.insert_semantic(SCHEMA, NS, "fact", sem_text, subject="outage", valid_from=now_ts,
                              vec=crafted_embed(sem_text), source_turn_ids=[], observed_at=now_ts)

        user_id = Control.create_principal(conn, "armdegrade-http-bot", "agent")
        token = Control.mint_token(conn, user_id, "t")
        Control.grant(conn, user_id, NS, can_read=True, can_write=True)

        print("=== baseline: recall succeeds normally (no lock yet) ===")
        s, j = call("/recall", token, {"namespace": NS, "query": "outage", "constraint_cap": 0})
        check("baseline /recall is 200", s == 200)
        check("baseline is NOT degraded", not j.get("degraded"))
        txt = " ".join(m.get("content", "") for m in j.get("memories", []))
        check("baseline returns both raw and semantic content", raw_text in txt and sem_text in txt)

        print("=== forcing a REAL statement_timeout cancellation on the semantic arm ===")
        lock_conn = psycopg.connect(DSN, autocommit=False)
        try:
            with lock_conn.cursor() as lc:
                lc.execute(f"LOCK TABLE {SCHEMA}.semantic IN ACCESS EXCLUSIVE MODE")
            # constraint_cap=0 skips pinned_constraints (DB phase A, also reads
            # {schema}.semantic) so the lock collides with ONLY the recall arm under test.
            s, j = call("/recall", token,
                       {"namespace": NS, "query": "outage", "constraint_cap": 0}, timeout=30)
        finally:
            lock_conn.rollback()
            lock_conn.close()

        check("degraded recall is 200, NOT a 5xx/exception (the exact issue #41 symptom)",
              s == 200, f"got {s}: {j}")
        check("response is flagged degraded:true", j.get("degraded") is True, str(j))
        reasons = j.get("degraded_reasons") or []
        check("degraded_reasons identifies the failed namespace + arm",
              any(r.get("namespace") == NS and r.get("arm") == "semantic" for r in reasons),
              str(reasons))
        check("degraded_reasons never leaks the raw exception message (class name only)",
              all(set(r.keys()) <= {"namespace", "arm", "error", "sqlstate"} for r in reasons),
              str(reasons))
        txt2 = " ".join(m.get("content", "") for m in j.get("memories", []))
        check("the OTHER (raw-turn) arm's real content is STILL present, not silently dropped",
              raw_text in txt2)
        check("the failed (semantic) arm's content is absent (that's what degraded means)",
              sem_text not in txt2)

        with conn.cursor() as c:
            c.execute("SELECT detail FROM memnos_control.audit_log "
                      "WHERE principal_id=%s AND action='recall' AND namespace=%s "
                      "ORDER BY id DESC LIMIT 1", (user_id, NS))
            audit_row = c.fetchone()
        audit_detail = (audit_row or {}).get("detail") or {}
        check("audit ledger's detail is also flagged degraded (operator-visible, not just client)",
              audit_detail.get("degraded") is True, str(audit_detail))
        check("audit ledger's detail carries the same degraded_reasons",
              any(r.get("namespace") == NS and r.get("arm") == "semantic"
                  for r in (audit_detail.get("degraded_reasons") or [])),
              str(audit_detail))

        print("=== recall recovers to non-degraded once the lock is released ===")
        s, j = call("/recall", token, {"namespace": NS, "query": "outage", "constraint_cap": 0})
        check("post-lock-release recall is 200", s == 200)
        check("post-lock-release recall is no longer degraded", not j.get("degraded"), str(j))
        txt3 = " ".join(m.get("content", "") for m in j.get("memories", []))
        check("post-lock-release recall returns BOTH arms' content again",
              raw_text in txt3 and sem_text in txt3)

        # issue #41 fix C: the WIDE path (`scope: "all"`) has its OWN degraded_reasons
        # plumbing -- wide_degraded_reasons is threaded through recall_wide_fetch as a
        # kwarg and read back separately from the narrow path's bundle.pop(). Every
        # assertion above only exercised the narrow path; this repeats the same real-lock
        # cancellation through /recall?scope=all end to end, so a mis-wired kwarg or a
        # rebound-instead-of-mutated list would show up here as a silent 200 with no
        # degraded key, instead of passing by accident because only the narrow path was
        # ever checked. The token's grant is a concrete namespace (not a wildcard), so
        # readable_namespaces() resolves to just [NS] without hitting the registry table
        # -- the semantic arm inside recall_wide_fetch's per-namespace loop is what hits
        # the lock here, not readable_namespaces() itself.
        print("=== WIDE recall (scope=all) also degrades correctly under the same real lock ===")
        lock_conn = psycopg.connect(DSN, autocommit=False)
        try:
            with lock_conn.cursor() as lc:
                lc.execute(f"LOCK TABLE {SCHEMA}.semantic IN ACCESS EXCLUSIVE MODE")
            s, j = call("/recall", token,
                       {"namespace": NS, "query": "outage", "scope": "all", "constraint_cap": 0},
                       timeout=30)
        finally:
            lock_conn.rollback()
            lock_conn.close()

        check("wide degraded recall is 200, NOT a 5xx/exception", s == 200, f"got {s}: {j}")
        check("wide response is flagged degraded:true", j.get("degraded") is True, str(j))
        wide_reasons = j.get("degraded_reasons") or []
        check("wide degraded_reasons identifies the failed namespace + arm",
              any(r.get("namespace") == NS and r.get("arm") == "semantic" for r in wide_reasons),
              str(wide_reasons))
        wide_txt = " ".join(m.get("content", "") for m in j.get("memories", []))
        check("wide recall's surviving raw-turn content is still present",
              raw_text in wide_txt)
        check("wide recall's namespaces_searched still lists NS (attempted, not silently dropped)",
              NS in (j.get("namespaces_searched") or []), str(j.get("namespaces_searched")))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        cleanup(conn)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
