"""No-AI tests for TYPED MEMORIES + PINNED CONSTRAINT INJECTION (0.1.6).

- `type` on /remember (decision | incident | constraint | skill | fact) is validated
  server-side (400 on unknown) and stamped as memory_type on the raw turn; facts derived
  by extraction INHERIT the turn's type (tested deterministically at the service layer).
- type='constraint' memories are ALWAYS injected into /recall on their namespace (and
  grant-readable linked knowledge namespaces) regardless of query similarity — first in
  `memories` (pinned: true) and rendered as leading "CONSTRAINT: ..." context lines.
  `constraint_cap` bounds them (default 10, 0 disables); they ADD to ranked results.
- `type` on /recall filters ranked results (pins are exempt); recall rows carry `type`
  and render_context labels typed lines '- (decision, ...)'.
- EPISODIC tier: an episode INHERITS a memory_type only when ALL its source turns share
  one non-null type (unanimous — mixed or partly-typed groups stay NULL); /episode/recall
  rows emit memory_type; constraint-typed episodes are pinned into /recall too.
- CLI: `memnos remember --type ...` / `memnos recall --type ...`.
- Admin memory feed: GET /admin/api/memory/feed (admin-only, paginated, type filter).

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python tests/test_memory_types.py
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore
from core.service import MemnosMemory

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NSX, NSK, NSE = "test:mt", "test:mt:kb", "test:mt:extract"
NSP = "test:mt:episodes"
PASS = FAIL = 0


def call(path, token=None, body=None, method="POST"):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def cli(*args, token=None):
    env = dict(os.environ, MEMNOS_DSN=DSN, MEMNOS_URL=URL)
    if token:
        env["MEMNOS_TOKEN"] = token
    r = subprocess.run([sys.executable, os.path.join(ROOT, "memnos_cli.py"), *args],
                       capture_output=True, text=True, timeout=60, env=env)
    return r.returncode, (r.stdout + r.stderr)


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        for ns in (NSX, NSK, NSE, NSP):
            c.execute(f"SELECT id FROM {SCHEMA}.entities WHERE namespace=%s", (ns,))
            eids = [r["id"] for r in c.fetchall()]
            if eids:
                c.execute(f"DELETE FROM {SCHEMA}.mentions WHERE entity_id = ANY(%s)", (eids,))
            c.execute(f"DELETE FROM {SCHEMA}.provenance WHERE episodic_id IN "
                      f"(SELECT id FROM {SCHEMA}.episodic WHERE namespace=%s)", (ns,))
            c.execute(f"DELETE FROM {SCHEMA}.provenance WHERE semantic_id IN "
                      f"(SELECT id FROM {SCHEMA}.semantic WHERE namespace=%s)", (ns,))
            for t in ("edges", "entities", "semantic", "episodic", "raw_turns"):
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.namespace_links WHERE src_ns=%s OR dst_ns=%s",
                      (ns, ns))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (ns,))
        for p in ("mt-admin", "mt-user"):
            c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals pr "
                      "WHERE t.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals pr "
                      "WHERE g.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.principals WHERE name=%s", (p,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)

    admin_id = Control.create_principal(conn, "mt-admin", "service")
    Control.grant(conn, admin_id, "*")
    TADM = Control.mint_token(conn, admin_id, "t")
    user_id = Control.create_principal(conn, "mt-user", "agent")
    TUSR = Control.mint_token(conn, user_id, "t")
    Control.grant(conn, user_id, NSX, can_read=True, can_write=True)

    print("=== write validation + stamping ===")
    s, j = call("/remember", TADM, {"namespace": NSX, "type": "bogus", "text": "x"})
    check("unknown type rejected with 400", s == 400 and "type" in j.get("error", ""))
    s, j = call("/remember", TADM,
                {"namespace": NSX, "type": "constraint",
                 "text": "Schema identifiers MUST be validated before interpolation."})
    check("remember with type=constraint 200", s == 200)
    s, j = call("/remember", TADM,
                {"namespace": NSX, "type": "decision",
                 "text": "We chose flombuzzle Postgres over ArcadeDB for the engine."})
    check("remember with type=decision 200", s == 200)
    s, j = call("/remember", TADM,
                {"namespace": NSX, "text": "Plain untyped flombuzzle note about the engine."})
    check("untyped remember still 200", s == 200)
    with conn.cursor() as c:
        c.execute(f"SELECT memory_type, count(*) n FROM {SCHEMA}.raw_turns "
                  f"WHERE namespace=%s GROUP BY memory_type", (NSX,))
        by_type = {r["memory_type"]: r["n"] for r in c.fetchall()}
    check("memory_type stamped on raw turns (constraint/decision/NULL)",
          by_type.get("constraint") == 1 and by_type.get("decision") == 1
          and by_type.get(None) == 1)

    print("=== pinned constraint injection ===")
    s, j = call("/recall", TADM, {"namespace": NSX, "query": "weather on the moon next tuesday"})
    mems = j.get("memories", [])
    pins = [m for m in mems if m.get("pinned")]
    check("constraint present for a completely unrelated query",
          any("MUST be validated" in m.get("content", "") for m in pins))
    check("pinned rows come FIRST in memories",
          pins and all(m.get("pinned") for m in mems[:len(pins)]))
    check("pinned rows are typed constraint", all(m.get("type") == "constraint" for m in pins))
    check("context starts with CONSTRAINT: line",
          j.get("context", "").startswith("CONSTRAINT:") and "MUST be validated" in j["context"])
    check("non-constraint types are NOT pinned",
          not any(m.get("type") == "decision" for m in pins))
    s, j = call("/recall", TADM, {"namespace": NSX, "query": "weather on the moon",
                                  "constraint_cap": 0})
    check("constraint_cap=0 disables pinning",
          s == 200 and not any(m.get("pinned") for m in j.get("memories", [])))
    s, j = call("/recall", TADM, {"namespace": NSX, "query": "weather", "constraint_cap": "x"})
    check("non-integer constraint_cap rejected with 400", s == 400)

    # cap respected: bulk-seed 14 more constraint turns directly (no embedding needed).
    # Dated AFTER the server-written one so oldest-first keeps the original rule first.
    now = datetime.now(timezone.utc)
    for i in range(14):
        store.insert_raw_turn(SCHEMA, NSX, None, "user", f"Rule {i}: services MUST retry idempotently ({i}).",
                              now + timedelta(minutes=i + 1), None, memory_type="constraint")
    s, j = call("/recall", TADM, {"namespace": NSX, "query": "weather on the moon"})
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    check("default cap = 10 pinned constraints", len(pins) == 10)
    check("pins ADD to ranked results (ranked rows still present)",
          any(not m.get("pinned") for m in j.get("memories", [])))
    s, j = call("/recall", TADM, {"namespace": NSX, "query": "weather on the moon",
                                  "constraint_cap": 3})
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    check("constraint_cap=3 respected", len(pins) == 3)
    check("oldest constraints first (the original rule leads)",
          pins and "MUST be validated" in pins[0]["content"])

    print("=== type filter + typed rows / context labels ===")
    s, j = call("/recall", TADM, {"namespace": NSX, "query": "flombuzzle engine",
                                  "type": "decision", "constraint_cap": 0})
    rows = j.get("memories", [])
    check("type filter returns only decision rows",
          rows and all(m.get("type") == "decision" for m in rows))
    check("typed row content matched", any("ArcadeDB" in m["content"] for m in rows))
    check("context labels the type: '- (decision'", "- (decision" in j.get("context", ""))
    s, j = call("/recall", TADM, {"namespace": NSX, "query": "flombuzzle engine",
                                  "type": "skill", "constraint_cap": 0})
    check("type filter with no matches returns empty", s == 200 and j.get("memories") == [])
    s, j = call("/recall", TADM, {"namespace": NSX, "query": "x", "type": "bogus"})
    check("unknown recall type filter rejected with 400", s == 400)
    s, j = call("/recall", TADM, {"namespace": NSX, "query": "flombuzzle engine",
                                  "type": "decision"})
    check("pins exempt from the type filter (constraints still injected)",
          any(m.get("pinned") for m in j.get("memories", []))
          and all(m.get("type") == "decision" for m in j["memories"] if not m.get("pinned")))

    print("=== extraction inheritance (service layer, deterministic fake extractor) ===")
    fake = lambda text, date: [{"subject": "Engine", "predicate": "", "object": "",
                                "statement": "The engine choice is Postgres."}]
    mem = MemnosMemory(store, lambda t: None, llm=None, extract_fn=fake, redact=False)
    mem.remember(NSE, "We decided the engine is Postgres.", memory_type="decision")
    with conn.cursor() as c:
        c.execute(f"SELECT memory_type FROM {SCHEMA}.semantic WHERE namespace=%s "
                  f"AND kind='fact'", (NSE,))
        rows = c.fetchall()
    check("extracted facts INHERIT the turn's type",
          rows and all(r["memory_type"] == "decision" for r in rows))
    mem.remember(NSE, "An untyped note.")
    with conn.cursor() as c:
        c.execute(f"SELECT memory_type FROM {SCHEMA}.semantic WHERE namespace=%s "
                  f"AND kind='fact' AND memory_type IS NULL", (NSE,))
        check("untyped turn yields untyped facts", c.fetchone() is not None)

    print("=== constraint verbatim bypass (issue #29): extraction never mangles a rule ===")
    # A deliberately WRONG extractor -- if it's ever invoked for a constraint write,
    # this mangled/inverted fragment would land in the DB. Call-counted so the test
    # proves the extractor was never called, not just that its output was discarded.
    calls = []
    def mangler(text, date):
        calls.append(text)
        return [{"subject": "Sai", "predicate": "will_fix", "object": "",
                 "statement": "Sai will fix the blockers through the SDLC."}]
    mem2 = MemnosMemory(store, lambda t: None, llm=None, extract_fn=mangler, redact=False)
    repro = ("If a reviewer leaves blockers on an MR, the agent must fix them through "
             "the SDLC and re-request review; never bypass them.")
    out = mem2.remember(NSE, repro, memory_type="constraint")
    check("constraint remember() extracted zero facts", out["facts"] == 0)
    check("the extractor was NEVER invoked for a constraint write (not just discarded)",
          calls == [])
    with conn.cursor() as c:
        c.execute(f"SELECT text FROM {SCHEMA}.raw_turns WHERE namespace=%s "
                  f"AND memory_type='constraint' AND text=%s", (NSE, repro))
        turn_row = c.fetchone()
        c.execute(f"SELECT statement FROM {SCHEMA}.semantic WHERE namespace=%s "
                  f"AND memory_type='constraint' AND statement ILIKE %s", (NSE, "%Sai will fix%"))
        mangled_row = c.fetchone()
    check("raw turn stores the rule VERBATIM (unchanged, unmangled)", turn_row is not None)
    check("the mangled/inverted fragment was never stored", mangled_row is None)

    # regression guard: a NON-constraint typed write still extracts normally through
    # the same extract_fn path -- the bypass is memory_type-scoped, not global.
    calls2 = []
    def normal_extractor(text, date):
        calls2.append(text)
        return [{"subject": "Engine", "predicate": "", "object": "",
                 "statement": "The engine choice is Postgres."}]
    mem3 = MemnosMemory(store, lambda t: None, llm=None, extract_fn=normal_extractor, redact=False)
    mem3.remember(NSE, "We decided the engine is Postgres, again.", memory_type="decision")
    check("a non-constraint typed write still runs extraction as before", calls2 != [])

    # live-server end-to-end: the exact repro text through /remember with type=constraint,
    # verified via /recall's pinned-constraint rendering (proves the fix holds through the
    # full HTTP path, not just the service-layer call above).
    s, j = call("/remember", TADM, {"namespace": NSX,
                                    "text": "The deploy pipeline must never run on a Friday afternoon.",
                                    "type": "constraint"})
    check("live /remember with type=constraint 200", s == 200)
    s, j = call("/recall", TADM, {"namespace": NSX, "query": "completely unrelated weather question"})
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    check("live: the new constraint is pinned verbatim (no paraphrase)",
          any(m.get("content") == "The deploy pipeline must never run on a Friday afternoon."
              for m in pins))

    print("=== episodic tier: unanimous type inheritance + pinning + recall arm ===")
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    # session A: ALL turns typed constraint → episode inherits 'constraint'
    for i, txt in enumerate(["Glorp tokens MUST never leave the enclave.",
                             "Glorp writes MUST be idempotent on retry."]):
        store.insert_raw_turn(SCHEMA, NSP, "ep-constraint", "user", txt,
                              t0 + timedelta(minutes=i), None, memory_type="constraint")
    # session B: mixed non-null types (decision + incident) → NULL
    store.insert_raw_turn(SCHEMA, NSP, "ep-mixed", "user",
                          "We chose the glorp scheduler.", t0 + timedelta(minutes=10),
                          None, memory_type="decision")
    store.insert_raw_turn(SCHEMA, NSP, "ep-mixed", "user",
                          "The glorp scheduler crashed at noon.", t0 + timedelta(minutes=11),
                          None, memory_type="incident")
    # session C: partly typed (decision + untyped) → NULL (unanimity is conservative)
    store.insert_raw_turn(SCHEMA, NSP, "ep-partial", "user",
                          "Decided to ship glorp v2 on Friday.", t0 + timedelta(minutes=20),
                          None, memory_type="decision")
    store.insert_raw_turn(SCHEMA, NSP, "ep-partial", "user",
                          "Also the weather was nice.", t0 + timedelta(minutes=21), None)
    # session D: single decision turn → unanimous 'decision'
    store.insert_raw_turn(SCHEMA, NSP, "ep-decision", "user",
                          "We will standardize on halfvec embeddings.",
                          t0 + timedelta(minutes=30), None, memory_type="decision")
    s, j = call("/episode/segment", TADM, {"namespace": NSP, "gap_minutes": 5})
    check("segmentation created 4 episodes", s == 200 and j.get("episodes") == 4)
    with conn.cursor() as c:
        c.execute(f"SELECT session_id, memory_type FROM {SCHEMA}.episodic "
                  f"WHERE namespace=%s ORDER BY id", (NSP,))
        by_sess = {r["session_id"]: r["memory_type"] for r in c.fetchall()}
    check("unanimous constraint turns => episode inherits 'constraint'",
          by_sess.get("ep-constraint") == "constraint")
    check("mixed types => episode memory_type NULL", by_sess.get("ep-mixed") is None)
    check("partly-typed group => episode memory_type NULL (conservative)",
          by_sess.get("ep-partial") is None)
    check("unanimous decision turn => episode inherits 'decision'",
          by_sess.get("ep-decision") == "decision")
    s, j = call("/episode/recall", TADM, {"namespace": NSP, "query": "glorp enclave tokens",
                                          "k": 8})
    eps = j.get("episodes", [])
    check("/episode/recall rows emit memory_type", s == 200 and eps
          and all("memory_type" in e for e in eps))
    check("constraint episode carries its type in recall",
          any(e.get("memory_type") == "constraint" and "enclave" in e.get("content", "")
              for e in eps))
    s, j = call("/recall", TADM, {"namespace": NSP, "query": "weather on the moon next tuesday"})
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    check("constraint-typed EPISODE pinned into /recall (kind=episode)",
          any(m.get("kind") == "episode" and "enclave" in m.get("content", "") for m in pins))
    check("non-constraint episodes are NOT pinned",
          not any(m.get("kind") == "episode" and "halfvec" in m.get("content", "")
                  for m in pins))
    check("episodic pins are typed constraint",
          all(m.get("type") == "constraint" for m in pins))

    print("=== grounded pinning (linked knowledge namespace) ===")
    store.insert_raw_turn(SCHEMA, NSK, None, "user",
                          "Tokens SHALL never be written to logs.", now, None,
                          memory_type="constraint")
    Control.set_namespace_kind(conn, NSK, "knowledge")
    Control.link_namespaces(conn, NSX, NSK, created_by=admin_id)
    Control.grant(conn, user_id, NSK, can_read=True, can_write=False)
    s, j = call("/recall", TUSR, {"namespace": NSX, "query": "weather on the moon",
                                  "constraint_cap": 50})
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    knpin = [m for m in pins if "never be written to logs" in m["content"]]
    check("linked knowledge-namespace constraint pinned too", bool(knpin))
    check("grounded pin tagged with its source namespace",
          knpin and knpin[0].get("namespace") == NSK)
    Control.unlink_namespaces(conn, NSX, NSK)
    s, j = call("/recall", TUSR, {"namespace": NSX, "query": "weather on the moon",
                                  "constraint_cap": 50})
    check("unlinked => knowledge constraint no longer pinned",
          not any("never be written to logs" in m["content"]
                  for m in j.get("memories", []) if m.get("pinned")))

    print("=== CLI ===")
    rc, out = cli("remember", "CLI deploys MUST run the full suite first.",
                  "--namespace", NSX, "--type", "constraint", "--json", token=TADM)
    check("CLI: remember --type constraint", rc == 0 and '"turn_id"' in out)
    rc, out = cli("remember", "x", "--namespace", NSX, "--type", "nonsense", token=TADM)
    check("CLI: bad --type rejected by the parser", rc != 0 and "invalid choice" in out)
    rc, out = cli("recall", "unrelated cli query", "--namespace", NSX, token=TADM)
    check("CLI: recall renders CONSTRAINT lines", rc == 0 and "CONSTRAINT:" in out)
    rc, out = cli("recall", "flombuzzle engine", "--namespace", NSX, "--type", "decision",
                  token=TADM)
    check("CLI: recall --type decision shows the decision", rc == 0 and "(decision" in out)

    print("=== admin memory feed ===")
    s, j = call("/admin/api/memory/feed?limit=5&offset=0", TADM, method="GET")
    check("feed 200 with rows", s == 200 and len(j.get("memories", [])) == 5)
    check("feed is newest-first", j["memories"][0]["id"] >= j["memories"][-1]["id"])
    check("feed rows carry namespace/type/author/age fields",
          all(set(m) >= {"id", "namespace", "content", "type", "author", "observed_at"}
              for m in j["memories"]))
    ns_q = NSX.replace(":", "%3A")
    s, j = call(f"/admin/api/memory/feed?namespace={ns_q}&type=decision", TADM, method="GET")
    check("feed namespace+type filter", s == 200 and j["memories"]
          and all(m["namespace"] == NSX and m["type"] == "decision" for m in j["memories"]))
    s, j = call("/admin/api/memory/feed?type=bogus", TADM, method="GET")
    check("feed unknown type rejected with 400", s == 400)
    s, j = call("/admin/api/memory/feed", TUSR, method="GET")
    check("feed is admin-only (403 for non-admin)", s == 403)
    s, j = call("/admin/api/memory/feed", method="GET")
    check("feed requires a token (401)", s == 401)

    cleanup(conn)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
