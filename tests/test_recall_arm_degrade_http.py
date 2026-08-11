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

Review-round-2 additions cover two gaps the first pass left untested:
  - pinned_constraints() itself was left UNGUARDED at its memnos_server.py call site --
    constraint_cap defaults to 10, not 0, so every scenario above (which always sends
    constraint_cap:0 to isolate the arm under test) accidentally also skipped the one
    query that ran unguarded on the real default. That scenario below deliberately
    omits constraint_cap from the request body -- the actual default a real client
    sends -- under the same real semantic-table lock, and proves it now degrades
    instead of 500ing.
  - readable_namespaces()'s wildcard-grant expansion (memnos_control.namespaces) was
    already correctly guarded at both its call sites (wide-scope fan-out and the
    narrow-path other_readable hint) but had no test coverage -- the wide-recall
    scenario above uses a CONCRETE-namespace grant, which never reaches the wildcard
    query at all. Those scenarios mint a '*'-grant principal and lock
    memnos_control.namespaces itself (not tenant_memnos.semantic) so only the registry
    scan fails, not the recall arms.

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
        for bot in ("armdegrade-http-bot", "armdegrade-http-wcbot"):
            c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals p "
                      "WHERE t.principal_id=p.id AND p.name=%s", (bot,))
            c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals p "
                      "WHERE g.principal_id=p.id AND p.name=%s", (bot,))
            c.execute("DELETE FROM memnos_control.principals WHERE name=%s", (bot,))


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
        # issue #59: poll /readyz, not /healthz — this test is about to send REAL
        # recall traffic against forced-cold arms, and /healthz's 200 (liveness only)
        # gives no guarantee the pool/HNSW indexes are actually warm. /readyz does.
        up = False
        for _ in range(60):
            try:
                if urllib.request.urlopen(f"{URL}/readyz", timeout=2).status == 200:
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
        # detail on failure: which arm/namespace tripped, not just true/false -- a
        # cold-start-dependent false positive here (PR #58 round 2) is otherwise
        # undiagnosable from CI output alone.
        check("baseline is NOT degraded", not j.get("degraded"), str(j.get("degraded_reasons")))
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

        # issue #41 fix C follow-up (review round 2): pinned_constraints() was left
        # UNGUARDED at the memnos_server.py call site -- constraint_cap defaults to 10,
        # not 0, so a real client that doesn't know to send constraint_cap:0 hits this
        # live {schema}.semantic/raw_turns/episodic query on EVERY /recall, before
        # recall_fetch's own guarded arms even run. Every scenario above deliberately
        # sent constraint_cap:0, the one flag that skips this exact query -- so none of
        # them ever exercised the real default. This repeats the same real ACCESS
        # EXCLUSIVE lock cancellation WITHOUT constraint_cap in the request body (the
        # real-world default a client sends when it has never heard of this flag),
        # proving pinned_constraints now degrades instead of 500ing.
        print("=== pinned_constraints degrades under a REAL lock, using the DEFAULT constraint_cap (no override) ===")
        pin_text = "Pinned budgets MUST be approved before spend."
        s, j = call("/remember", token, {"namespace": NS, "type": "constraint", "text": pin_text})
        check("seeding a constraint memory is 200", s == 200, f"got {s}: {j}")

        s, j = call("/recall", token, {"namespace": NS, "query": "outage"})
        check("baseline (no constraint_cap field) /recall is 200", s == 200)
        check("baseline is NOT degraded", not j.get("degraded"), str(j))
        base_pins = [m for m in j.get("memories", []) if m.get("pinned")]
        check("baseline pins the seeded constraint under the real default cap",
              any(pin_text in p.get("content", "") for p in base_pins), str(base_pins))

        lock_conn = psycopg.connect(DSN, autocommit=False)
        try:
            with lock_conn.cursor() as lc:
                lc.execute(f"LOCK TABLE {SCHEMA}.semantic IN ACCESS EXCLUSIVE MODE")
            # deliberately NO constraint_cap field here -- this is the exact gap: before
            # the fix, this request 500'd ("internal error") instead of degrading.
            s, j = call("/recall", token, {"namespace": NS, "query": "outage"}, timeout=30)
        finally:
            lock_conn.rollback()
            lock_conn.close()

        check("pinned_constraints degraded recall is 200, NOT a 5xx (the exact gap this round found)",
              s == 200, f"got {s}: {j}")
        check("response is flagged degraded:true", j.get("degraded") is True, str(j))
        pin_reasons = j.get("degraded_reasons") or []
        check("degraded_reasons identifies arm=pinned_constraints for this namespace",
              any(r.get("namespace") == NS and r.get("arm") == "pinned_constraints" for r in pin_reasons),
              str(pin_reasons))
        # issue #59: pinned_constraints is one of the phase-A-adjacent arms that now runs
        # a cheap control probe on failure and attaches a crafted, non-leaking `hint`
        # string (see openapi.yaml's DegradedReason.hint) — added to the allow-list, not
        # a leak of the raw exception text, which the second check below still guards.
        check("degraded_reasons never leaks the raw exception message (class name only, "
              "plus the optional crafted `hint` issue #59 adds)",
              all(set(r.keys()) <= {"namespace", "arm", "error", "sqlstate", "hint"}
                  for r in pin_reasons),
              str(pin_reasons))
        check("when present, `hint` is a crafted classification string, never the raw "
              "psycopg exception message",
              all("canceling statement" not in r.get("hint", "") for r in pin_reasons),
              str(pin_reasons))
        degraded_mems = j.get("memories", [])
        check("no pinned rows survive the failed pinned_constraints arm (that's what degraded means)",
              not any(m.get("pinned") for m in degraded_mems), str(degraded_mems))
        check("the OTHER (raw-turn) arm's content is STILL present despite pinned_constraints failing",
              raw_text in " ".join(m.get("content", "") for m in degraded_mems))

        print("=== recovers to non-degraded, pin restored, once the lock is released ===")
        s, j = call("/recall", token, {"namespace": NS, "query": "outage"})
        check("post-lock-release recall is 200", s == 200)
        check("post-lock-release recall is no longer degraded", not j.get("degraded"), str(j))
        post_pins = [m for m in j.get("memories", []) if m.get("pinned")]
        check("post-lock-release recall pins the constraint again",
              any(pin_text in p.get("content", "") for p in post_pins), str(post_pins))

        # issue #41 fix C follow-up (review round 2, cheap non-blocking finding):
        # readable_namespaces() is only ever exercised above by a token with a CONCRETE
        # namespace grant, which never reaches the wildcard-expansion query at all (see
        # the wide-recall comment above -- it resolves to [NS] without hitting the
        # registry table). Both call sites that guard THAT query --
        # memnos_server.py's wide-scope readable_namespaces() fan-out and the narrow-path
        # other_readable hint -- are correctly wrapped in RECALL_ARM_FAILURES already,
        # but had zero test coverage before this round (verified manually via real-lock
        # repros, never in CI). A wildcard ('*') grant is required to reach
        # memnos_control.namespaces at all -- lock THAT table (not tenant_memnos.semantic)
        # so only the registry scan fails, not the recall arms themselves.
        print("=== readable_namespaces() wildcard fan-out degrades under a REAL lock on the registry table ===")
        wc_user_id = Control.create_principal(conn, "armdegrade-http-wcbot", "agent")
        wc_token = Control.mint_token(conn, wc_user_id, "t")
        Control.grant(conn, wc_user_id, "*", can_read=True, can_write=False)

        lock_conn = psycopg.connect(DSN, autocommit=False)
        try:
            with lock_conn.cursor() as lc:
                lc.execute("LOCK TABLE memnos_control.namespaces IN ACCESS EXCLUSIVE MODE")
            s, j = call("/recall", wc_token,
                       {"namespace": NS, "query": "outage", "scope": "all", "constraint_cap": 0},
                       timeout=30)
        finally:
            lock_conn.rollback()
            lock_conn.close()

        check("wide recall under a wildcard grant still 200s when the registry scan is canceled",
              s == 200, f"got {s}: {j}")
        check("wildcard-fan-out degrade is flagged degraded:true", j.get("degraded") is True, str(j))
        wc_reasons = j.get("degraded_reasons") or []
        check("degraded_reasons identifies arm=readable_namespaces",
              any(r.get("arm") == "readable_namespaces" for r in wc_reasons), str(wc_reasons))
        check("search scope fell back to just the query namespace (safe: caller already holds a grant on it)",
              j.get("namespaces_searched") == [NS], str(j.get("namespaces_searched")))
        wc_txt = " ".join(m.get("content", "") for m in j.get("memories", []))
        check("the query namespace's own content still comes back despite the registry scan failing",
              raw_text in wc_txt)

        print("=== the narrow-path other_readable HINT degrades to [] under the same lock, without flipping degraded ===")
        lock_conn = psycopg.connect(DSN, autocommit=False)
        try:
            with lock_conn.cursor() as lc:
                lc.execute("LOCK TABLE memnos_control.namespaces IN ACCESS EXCLUSIVE MODE")
            s, j = call("/recall", wc_token,
                       {"namespace": NS, "query": "outage", "constraint_cap": 0}, timeout=30)
        finally:
            lock_conn.rollback()
            lock_conn.close()

        check("narrow recall under a wildcard grant still 200s when the registry scan is canceled",
              s == 200, f"got {s}: {j}")
        check("a HINT failure alone does NOT flip degraded:true (it's not a results source)",
              not j.get("degraded"), str(j))
        check("other_readable_namespaces falls back to [] (hint absent, not stale/wrong data)",
              j.get("scope", {}).get("other_readable_namespaces") == [], str(j.get("scope")))
        check("no scope.hint key when other_readable is empty (no-drift convention)",
              "hint" not in j.get("scope", {}), str(j.get("scope")))
        narrow_txt = " ".join(m.get("content", "") for m in j.get("memories", []))
        check("the primary namespace's own content is unaffected by the hint failure",
              raw_text in narrow_txt)

        print("=== both wildcard-grant paths recover once the registry lock is released ===")
        s, j = call("/recall", wc_token,
                   {"namespace": NS, "query": "outage", "scope": "all", "constraint_cap": 0})
        check("post-lock-release wide recall under wildcard grant is 200 and not degraded",
              s == 200 and not j.get("degraded"), str(j))
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
