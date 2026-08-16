"""Per-constraint injection audit (issue #82, epic #70 item 3).

Gap this closes: `audit_log` already records that a recall happened (action='recall',
per-stage timings in `detail`) and the #28 `constraint.enforce` path already logs
ask/block decisions. Neither records "constraint X was actually shown to session Y" for
the pinned/advise path -- everything `/memnos constraint` writes by default, and the
common case. This test proves the new `constraint.inject` audit_log row does exactly
that: one row per constraint actually injected into a /recall response, carrying
constraint_id ("{kind}:{id}", e.g. "turn:123") + namespace (native column) + session_id
(in `detail`, nullable) + ts (native column, default now()).

No new table, no new column -- see core/control.py CONTROL_DDL / core/store.py
pinned_constraints() for the mechanism. Real Postgres, real HTTP server, no LLM (free
local-384 mode -- constraint pinning is pure SQL, no embedding involved either way).

Run against a live local server (like test_memory_types.py):
    MEMNOS_DSN=... MEMNOS_URL=... python tests/test_constraint_injection_audit.py
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"
NS = "test:inj-audit"          # seeded with 3 pinned constraints
# issue #85: deliberately NOT a ':'-prefix descendant of NS ("test:inj-audit:empty" would
# be) -- once same-root ancestor inheritance (Mechanism A) is live, a namespace nested
# under NS automatically pins NS's 3 seeded constraints too, which breaks this file's
# "genuinely nothing pinned" negative-path fixture. A hyphen keeps the name readable and
# obviously related without creating an accidental parent/child relationship.
NS_EMPTY = "test:inj-audit-empty"   # never gets a constraint written
PASS = FAIL = 0


def call(path, token=None, body=None):
    req = urllib.request.Request(URL + path, method="POST",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def inject_rows(conn, ns=NS, *, session_id=None, min_id=None):
    """constraint.inject audit rows for namespace `ns`, ordered by id ASC. session_id=None
    (the default) means "assert session_id IS NULL" -- a request that omits session_id
    entirely IS the null case, never "don't care" -- so every call site's filter reflects
    exactly what it expects the request to have sent. `min_id` (a pre-call audit_log
    watermark) additionally isolates one specific call when the same session_id/namespace
    combination could otherwise match rows from an earlier call in this same test run."""
    clauses = ["action='constraint.inject'", "namespace=%(ns)s"]
    params = {"ns": ns}
    if session_id is None:
        clauses.append("detail->>'session_id' IS NULL")
    else:
        clauses.append("detail->>'session_id' = %(sid)s")
        params["sid"] = session_id
    if min_id is not None:
        clauses.append("id > %(mid)s")
        params["mid"] = min_id
    with conn.cursor() as c:
        c.execute(f"SELECT id, namespace, ok, status, detail FROM memnos_control.audit_log "
                  f"WHERE {' AND '.join(clauses)} ORDER BY id", params)
        return c.fetchall()


def all_inject_rows(conn, ns, min_id):
    """Every constraint.inject row for `ns` since `min_id`, regardless of session_id --
    the global duplication guard: sums across every sub-test's session_id, so it catches
    ANY source of double-emission (a keep-alive leak, a retry, a stray second flush) that
    a single session_id-scoped query would miss if it happened to duplicate WITHIN one
    session_id's rows."""
    with conn.cursor() as c:
        c.execute("SELECT id FROM memnos_control.audit_log WHERE action='constraint.inject' "
                  "AND namespace=%s AND id > %s", (ns, min_id))
        return c.fetchall()


def max_audit_id(conn):
    with conn.cursor() as c:
        c.execute("SELECT COALESCE(max(id), 0) AS m FROM memnos_control.audit_log")
        return c.fetchone()["m"]


def recall_audit_count(conn, principal_id, ns):
    with conn.cursor() as c:
        c.execute("SELECT count(*) AS n FROM memnos_control.audit_log "
                  "WHERE action='recall' AND principal_id=%s AND namespace=%s",
                  (principal_id, ns))
        return c.fetchone()["n"]


def cleanup(conn):
    with conn.cursor() as c:
        for ns in (NS, NS_EMPTY):
            for t in ("semantic", "episodic", "raw_turns"):
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (ns,))
        c.execute("DELETE FROM memnos_control.audit_log WHERE namespace IN (%s,%s)", (NS, NS_EMPTY))
        c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals pr "
                  "WHERE t.principal_id=pr.id AND pr.name=%s", ("inj-audit-agent",))
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals pr "
                  "WHERE g.principal_id=pr.id AND pr.name=%s", ("inj-audit-agent",))
        c.execute("DELETE FROM memnos_control.principals WHERE name=%s", ("inj-audit-agent",))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)

    pid = Control.create_principal(conn, "inj-audit-agent", "agent")
    tok = Control.mint_token(conn, pid, "t")
    Control.grant(conn, pid, NS, can_read=True, can_write=True)
    Control.grant(conn, pid, NS_EMPTY, can_read=True, can_write=True)

    # seed 3 pinned constraints directly (no embedding needed -- pinned_constraints is
    # pure SQL). Local/free mode never extracts (issue #29), so these land as raw_turns
    # only -- kind='turn' -- exactly what a live deployment without OPENAI_API_KEY sees.
    now = datetime.now(timezone.utc)
    tids = []
    for i in range(3):
        tid = store.insert_raw_turn(SCHEMA, NS, None, "user",
            f"Rule {i}: services in this namespace MUST validate input ({i}).",
            now + timedelta(minutes=i), None, memory_type="constraint")
        tids.append(tid)
    want_cids = {f"turn:{t}" for t in tids}

    # global duplication guard (checked at the very end): every constraint.inject row
    # for NS since this watermark, across EVERY sub-test below regardless of session_id,
    # must sum to exactly `ns_expected` -- proves no call double-emits (e.g. a stale
    # `self._pin_audit` leaking into a later request on the same Handler instance) that a
    # single session_id-scoped assertion could miss.
    ns_watermark = max_audit_id(conn)
    ns_expected = 0

    print("=== positive: N pinned constraints -> N constraint.inject rows ===")
    before = recall_audit_count(conn, pid, NS)
    s, j = call("/recall", tok, {"namespace": NS, "query": "unrelated space weather",
                                 "session_id": "sess-A"})
    check("recall 200", s == 200, str(j))
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    check("all 3 seeded constraints pinned", len(pins) == 3, str(pins))

    rows = inject_rows(conn, session_id="sess-A")
    # the invariant that actually catches double-counting / under-counting bugs: rows
    # emitted == rows actually pinned in THIS response, not a hardcoded 3.
    check("constraint.inject row count == pinned row count", len(rows) == len(pins),
          f"{len(rows)} rows vs {len(pins)} pins")
    check("every row namespaced to the constraint's own namespace",
          all(r["namespace"] == NS for r in rows), str(rows))
    check("every row ok=True status=200", all(r["ok"] and r["status"] == 200 for r in rows))
    got_cids = {r["detail"]["constraint_id"] for r in rows}
    check("constraint_id set matches the exact seeded rows (kind:id, no collisions)",
          got_cids == want_cids, f"got {got_cids} want {want_cids}")
    check("session_id recorded on every row", all(r["detail"]["session_id"] == "sess-A" for r in rows))
    ns_expected += len(pins)

    after = recall_audit_count(conn, pid, NS)
    check("exactly one general 'recall' audit row also fired for this call (unaffected by #82)",
          after == before + 1, f"before={before} after={after}")

    print("=== negative: recall with NO pinned constraints emits ZERO injection rows ===")
    before_wm = max_audit_id(conn)
    s, j = call("/recall", tok, {"namespace": NS_EMPTY, "query": "anything"})
    check("recall on empty namespace 200", s == 200, str(j))
    check("no pins in response", not [m for m in j.get("memories", []) if m.get("pinned")])
    empty_rows = inject_rows(conn, NS_EMPTY, min_id=before_wm)
    check("zero constraint.inject rows for a recall with nothing pinned", len(empty_rows) == 0,
          str(empty_rows))

    print("=== negative: constraint_cap=0 on a namespace THAT HAS constraints -> zero rows ===")
    before_wm2 = max_audit_id(conn)
    s, j = call("/recall", tok, {"namespace": NS, "query": "unrelated", "constraint_cap": 0,
                                 "session_id": "sess-capzero"})
    check("recall with constraint_cap=0 is 200", s == 200, str(j))
    check("cap=0 disables pinning", not [m for m in j.get("memories", []) if m.get("pinned")])
    capzero_rows = inject_rows(conn, session_id="sess-capzero")
    check("zero constraint.inject rows when constraint_cap=0 suppressed the pins",
          len(capzero_rows) == 0, str(capzero_rows))

    print("=== session_id omitted entirely -> rows still fire, session_id recorded null ===")
    before_wm3 = max_audit_id(conn)
    s, j = call("/recall", tok, {"namespace": NS, "query": "unrelated"})   # no session_id field
    check("recall without session_id is 200", s == 200, str(j))
    pins3 = [m for m in j.get("memories", []) if m.get("pinned")]
    null_rows = inject_rows(conn, min_id=before_wm3)
    check("rows still fire without a session_id", len(null_rows) == len(pins3), str(null_rows))
    check("session_id recorded as null, not omitted (uniform detail shape)",
          all(r["detail"]["session_id"] is None for r in null_rows), str(null_rows))
    ns_expected += len(pins3)

    print("=== queryable by session_id: two separate recalls, same session_id, rows accumulate ===")
    sid = "sess-multi"
    s1, j1 = call("/recall", tok, {"namespace": NS, "query": "first turn", "session_id": sid})
    s2, j2 = call("/recall", tok, {"namespace": NS, "query": "second turn", "session_id": sid})
    check("both recalls 200", s1 == 200 and s2 == 200, f"{s1} {s2}")
    pins1 = [m for m in j1.get("memories", []) if m.get("pinned")]
    pins2 = [m for m in j2.get("memories", []) if m.get("pinned")]
    multi_rows = inject_rows(conn, session_id=sid)
    # derived from what the two responses actually pinned, not a hardcoded count -- if a
    # future change makes recall #2 dedupe against #1 (it shouldn't; they're independent
    # requests) this would catch it instead of silently passing on a stale constant.
    check("two recalls under one session_id => the exact sum of both calls' pins, all "
          "queryable together via detail->>'session_id'",
          len(multi_rows) == len(pins1) + len(pins2),
          f"got {len(multi_rows)} rows vs pins1={len(pins1)} pins2={len(pins2)}")
    ns_expected += len(pins1) + len(pins2)

    print("=== a 400 (validation failure) before the recall ever runs emits NOTHING ===")
    before_wm4 = max_audit_id(conn)
    s, j = call("/recall", tok, {"namespace": NS, "query": "x", "constraint_cap": "not-an-int"})
    check("bad constraint_cap rejected with 400", s == 400, str(j))
    bad_rows = inject_rows(conn, min_id=before_wm4)
    check("no constraint.inject rows for a request that never actually recalled anything",
          len(bad_rows) == 0, str(bad_rows))

    print("=== endpoint coverage: /memory/search shares the same emission path as /recall ===")
    before_wm5 = max_audit_id(conn)
    s, j = call("/memory/search", tok, {"namespace": NS, "query": "unrelated",
                                        "session_id": "sess-search"})
    check("/memory/search 200", s == 200, str(j))
    pins5 = [m for m in j.get("memories", []) if m.get("pinned")]
    check("/memory/search still pins constraints (no context field, different response shape)",
          len(pins5) == 3, str(pins5))
    search_rows = inject_rows(conn, session_id="sess-search", min_id=before_wm5)
    check("/memory/search emits constraint.inject rows too (all 4 recall-family endpoints "
          "share this code path)", len(search_rows) == len(pins5), str(search_rows))
    ns_expected += len(pins5)

    print("=== global duplication guard: total rows for NS == sum of every call's pins above ===")
    total_rows = all_inject_rows(conn, NS, ns_watermark)
    check("no duplicate/leaked emission across the whole run (e.g. a stale self._pin_audit "
          "surviving into a later request)", len(total_rows) == ns_expected,
          f"got {len(total_rows)} total rows vs expected {ns_expected}")

    cleanup(conn)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
