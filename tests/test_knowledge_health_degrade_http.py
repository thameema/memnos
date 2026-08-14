"""End-to-end HTTP regression for issue #69's second half: /knowledge/health degrades
instead of 500ing when one of its underlying signal queries hits a live, reachable-server
failure. Same client-facing contract #41 fix C established for /recall
(tests/test_recall_arm_degrade_http.py) -- "a single-arm statement_timeout cancellation
yields a partial degraded=true result, not a 5xx/exception" -- applied here to a
diagnostic endpoint instead of recall, reusing the SAME shared helper
(core/store.py:record_arm_failure, which core/service.py's recall arms also call) rather
than bespoke degrade logic for this one endpoint.

Before this fix: memnos_server.py's /knowledge/health handler called
`store.health(mem.schema, ns)` with no try/except, and BrainStore.health() ran its six
structural-signal queries as one sequential block with no per-query error handling -- ANY
one of them raising (most likely the orphan-entities query, see
test_orphan_entities_index.py for why that one specifically blew the statement_timeout on
a large namespace) failed the WHOLE report with an unhandled 500, even though the other
five signals had already succeeded.

Forces a REAL (non-mocked) Postgres statement_timeout cancellation via an ACCESS EXCLUSIVE
lock held on a second connection -- same technique as test_recall_arm_degrade_http.py and
test_recall_arm_degrade.py's real-lock scenario -- rather than mocking psycopg, so this
proves the fix against the actual failure shape (psycopg.errors.QueryCanceled) issue #69
reports, not a fake exception class a mock happens to raise.

Covers:
  1. Locking {schema}.edges cancels ONLY the orphan_entities signal (the query issue #69
     is about) -- /knowledge/health still returns 200, degraded:true, degraded_reasons
     names arm=orphan_entities, orphan_entities is null in the response, and -- the part a
     naive "wrap the whole call" fix would get wrong -- the OTHER five signals
     (facts_current/facts_superseded/facts_expired/entities/contradiction_groups) are
     STILL present and correct, not silently dropped along with the one that failed.
  2. Locking {schema}.semantic cancels the THREE fact-count signals (facts_current,
     facts_superseded, facts_expired) that query it, while entities/orphan_entities
     (which never touch semantic) survive untouched -- proves the degrade is genuinely
     per-signal, not just per-endpoint-call, and that every signal shares the same
     degrade-on-failure treatment (issue #69's own triage note: "every diagnostic/recall
     arm fails soft the same way").
  3. Recall recovers to a fully non-degraded report once each lock is released.

Owns its own memnos_server.py subprocess on a dedicated port (never touches a real :8900
instance), same pattern as test_recall_arm_degrade_http.py.

Run: python tests/test_knowledge_health_degrade_http.py
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
PORT = int(os.environ.get("MEMNOS_HEALTH_DEGRADE_HTTP_TEST_PORT", "8963"))
URL = f"http://127.0.0.1:{PORT}"
SCHEMA = "tenant_memnos"
NS = "test:healthdegrade:http"
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
    # issue #41 fix C precedent: set via the connection-string DEFAULT (the pool's normal
    # mechanism, memnos_server.py's `-c statement_timeout=...`), not a session-level SET --
    # see test_recall_arm_degrade_http.py's module docstring for why that distinction
    # matters (fts_clamp's own numnode() probe resets statement_timeout to DEFAULT after
    # every call, which would silently wipe out a bare session-level SET).
    env["MEMNOS_STMT_TIMEOUT_MS"] = "1800"
    return env


def cleanup(conn):
    with conn.cursor() as c:
        c.execute(f"SELECT id FROM {SCHEMA}.entities WHERE namespace=%s", (NS,))
        eids = [r["id"] for r in c.fetchall()]
        if eids:
            c.execute(f"DELETE FROM {SCHEMA}.mentions WHERE entity_id = ANY(%s)", (eids,))
        for t in ("edges", "semantic", "entities"):
            c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals p "
                  "WHERE t.principal_id=p.id AND p.name=%s", ("healthdegrade-http-bot",))
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals p "
                  "WHERE g.principal_id=p.id AND p.name=%s", ("healthdegrade-http-bot",))
        c.execute("DELETE FROM memnos_control.principals WHERE name=%s", ("healthdegrade-http-bot",))


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
                if urllib.request.urlopen(f"{URL}/readyz", timeout=2).status == 200:
                    up = True; break
            except Exception:
                pass
            time.sleep(1)
        check("dedicated server came up", up)
        if not up:
            print("server never became ready; aborting"); sys.exit(1)

        # A connected entity + an orphan entity + a couple of live facts, so every
        # health() signal has a real, non-zero value to check for survival under degrade.
        a = store.upsert_entity(SCHEMA, NS, "Alpha")
        b = store.upsert_entity(SCHEMA, NS, "Beta")
        store.upsert_entity(SCHEMA, NS, "OrphanEntity")   # no edges -- the one true orphan
        store.bump_edge(SCHEMA, NS, a, b)
        from datetime import datetime, timezone
        now_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store.insert_semantic(SCHEMA, NS, "fact", "Alpha relates_to Beta", subject="Alpha",
                              valid_from=now_ts, vec=None, source_turn_ids=[], observed_at=now_ts)

        user_id = Control.create_principal(conn, "healthdegrade-http-bot", "agent")
        token = Control.mint_token(conn, user_id, "t")
        Control.grant(conn, user_id, NS, can_read=True, can_write=True)

        print("=== baseline: /knowledge/health succeeds normally (no lock yet) ===")
        s, j = call("/knowledge/health", token, {"namespace": NS})
        check("baseline is 200", s == 200)
        check("baseline is not degraded", not j.get("degraded"), str(j))
        check("baseline orphan_entities == 1 (OrphanEntity)", j.get("orphan_entities") == 1, str(j))
        check("baseline entities == 3", j.get("entities") == 3, str(j))
        check("baseline facts_current == 1", j.get("facts_current") == 1, str(j))

        # ============================================================================
        # 1. Lock {schema}.edges -- ONLY orphan_entities touches it. Every other signal
        #    must survive untouched (the partial-degrade claim, not just "no 500").
        # ============================================================================
        print("=== locking edges: ONLY orphan_entities degrades, the other 5 signals survive ===")
        lock_conn = psycopg.connect(DSN, autocommit=False)
        try:
            with lock_conn.cursor() as lc:
                lc.execute(f"LOCK TABLE {SCHEMA}.edges IN ACCESS EXCLUSIVE MODE")
            s, j = call("/knowledge/health", token, {"namespace": NS}, timeout=30)
        finally:
            lock_conn.rollback()
            lock_conn.close()

        check("degraded /knowledge/health is 200, NOT a 5xx/exception (the exact issue #69 symptom)",
              s == 200, f"got {s}: {j}")
        check("response is flagged degraded:true", j.get("degraded") is True, str(j))
        reasons = j.get("degraded_reasons") or []
        check("degraded_reasons identifies namespace + arm=orphan_entities",
              any(r.get("namespace") == NS and r.get("arm") == "orphan_entities" for r in reasons),
              str(reasons))
        check("degraded_reasons never leaks the raw exception message (class name only, "
              "plus the optional crafted hint)",
              all(set(r.keys()) <= {"namespace", "arm", "error", "sqlstate", "hint"} for r in reasons),
              str(reasons))
        check("orphan_entities is null (the failed signal)", j.get("orphan_entities") is None, str(j))
        check("facts_current SURVIVES (1) despite orphan_entities failing", j.get("facts_current") == 1, str(j))
        check("facts_superseded SURVIVES (0)", j.get("facts_superseded") == 0, str(j))
        check("facts_expired SURVIVES (0)", j.get("facts_expired") == 0, str(j))
        check("entities SURVIVES (3)", j.get("entities") == 3, str(j))
        check("contradiction_groups SURVIVES (0)", j.get("contradiction_groups") == 0, str(j))
        check("score is null -- orphan_entities feeds the score calculation and just failed, "
              "so a best-effort number would silently understate the real orphan count",
              j.get("score") is None, str(j))

        print("=== recovers to fully non-degraded once the edges lock is released ===")
        s, j = call("/knowledge/health", token, {"namespace": NS})
        check("post-lock-release is 200", s == 200)
        check("post-lock-release is no longer degraded", not j.get("degraded"), str(j))
        check("post-lock-release orphan_entities is back (1)", j.get("orphan_entities") == 1, str(j))

        # ============================================================================
        # 2. Lock {schema}.semantic -- the three fact-count signals AND contradictions()
        #    all query it; entities/orphan_entities never touch semantic and must
        #    survive. Proves the degrade is per-signal, not per-endpoint-call.
        # ============================================================================
        print("=== locking semantic: the fact-count + contradiction signals degrade, "
              "entities/orphan_entities survive ===")
        lock_conn = psycopg.connect(DSN, autocommit=False)
        try:
            with lock_conn.cursor() as lc:
                lc.execute(f"LOCK TABLE {SCHEMA}.semantic IN ACCESS EXCLUSIVE MODE")
            s, j = call("/knowledge/health", token, {"namespace": NS}, timeout=30)
        finally:
            lock_conn.rollback()
            lock_conn.close()

        check("degraded (semantic-locked) /knowledge/health is 200, NOT a 5xx", s == 200, f"got {s}: {j}")
        check("response is flagged degraded:true", j.get("degraded") is True, str(j))
        sem_reasons = j.get("degraded_reasons") or []
        sem_arms = {r.get("arm") for r in sem_reasons if r.get("namespace") == NS}
        check("degraded_reasons names all three fact-count arms plus contradictions",
              {"facts_current", "facts_superseded", "facts_expired", "contradictions"} <= sem_arms,
              str(sem_reasons))
        check("facts_current is null (failed)", j.get("facts_current") is None, str(j))
        check("facts_superseded is null (failed)", j.get("facts_superseded") is None, str(j))
        check("facts_expired is null (failed)", j.get("facts_expired") is None, str(j))
        check("contradiction_groups is null (failed)", j.get("contradiction_groups") is None, str(j))
        check("score is null -- contradiction_groups feeds the score calculation and just failed",
              j.get("score") is None, str(j))
        check("entities SURVIVES (3) -- never touches semantic", j.get("entities") == 3, str(j))
        check("orphan_entities SURVIVES (1) -- never touches semantic", j.get("orphan_entities") == 1, str(j))

        print("=== recovers to fully non-degraded once the semantic lock is released ===")
        s, j = call("/knowledge/health", token, {"namespace": NS})
        check("post-lock-release is 200", s == 200)
        check("post-lock-release is no longer degraded", not j.get("degraded"), str(j))
        check("post-lock-release facts_current is back (1)", j.get("facts_current") == 1, str(j))
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
