"""No-AI tests for PINNED-CONSTRAINT WRITE HYGIENE (issue #153).

Production incident: 6 pinned constraints in one namespace totaled 10,040 chars against
render_context's 9,000-char default budget. Pins render first, so recall injected ZERO
ranked facts for every prompt. Contributing gaps: untagged constraints were immortal
(retire_constraints() only matches on subject), and nothing bounded pinned constraints at
write time.

Covered here, against a real server and real Postgres:
  1. SUBJECT: a constraint written WITHOUT constraint_subject is not left untagged. The
     server derives a collision-safe 'auto:<slug>-<hash>' subject, stores it, and returns
     it. The derived subject then works as a real retirement handle. Legacy untagged rows
     are backfilled on the next constraint write in their namespace.
  2. BUDGET GUARD (hard reject, 409, nothing saved): the exact production scenario, plus
     the count cap. The rejection names retirement candidates (id, subject, size, age).
     Supersession is netted, and a write that shrinks usage is always accepted (the
     ratchet), so an already-over-budget namespace can be brought back under.
  3. NO REGRESSION: a namespace with a few small pins plus many ranked memories renders
     BOTH in /recall's context.

    MEMNOS_DSN=... MEMNOS_URL=... python tests/test_constraint_budget_guard.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore
from core.service import derive_constraint_subject, PINNED_BUDGET_CHARS_DEFAULT, \
    PINNED_MAX_COUNT_DEFAULT

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"

NS_SUBJ = "test:cbg:subject"
NS_PROD = "test:cbg:prod-repro"
NS_COUNT = "test:cbg:count"
NS_NORMAL = "test:cbg:normal"
NS_OTHER = "test:cbg:other"
ALL_NS = (NS_SUBJ, NS_PROD, NS_COUNT, NS_NORMAL, NS_OTHER)
PASS = FAIL = 0


def call(path, token=None, body=None, method="POST"):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        for ns in ALL_NS:
            for t in ("edges", "entities", "semantic", "episodic", "raw_turns"):
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (ns,))
            c.execute("DELETE FROM memnos_control.audit_log WHERE namespace=%s", (ns,))
        c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals pr "
                  "WHERE t.principal_id=pr.id AND pr.name='cbg-admin'")
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals pr "
                  "WHERE g.principal_id=pr.id AND pr.name='cbg-admin'")
        c.execute("DELETE FROM memnos_control.principals WHERE name='cbg-admin'")


def live_constraints(conn, ns):
    with conn.cursor() as c:
        c.execute(f"SELECT id, text, constraint_subject, constraint_retired_at FROM "
                  f"{SCHEMA}.raw_turns WHERE namespace=%s AND memory_type='constraint' "
                  f"ORDER BY id", (ns,))
        return c.fetchall()


def constraint(tok, ns, text, subject=None):
    body = {"namespace": ns, "text": text, "type": "constraint"}
    if subject is not None:
        body["constraint_subject"] = subject
    return call("/remember", tok, body)


def rule(i, n):
    """A distinct constraint of EXACTLY n chars of content."""
    head = f"Rule {i}: agents MUST follow deployment policy clause {i}. "
    return (head + ("x" * n))[:n]


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)
    pid = Control.create_principal(conn, "cbg-admin", "service")
    Control.grant(conn, pid, "*")
    TOK = Control.mint_token(conn, pid, "t")

    # ---------------------------------------------------------------- 1. subject
    print("=== 1. constraint without a subject: derived, stored, returned, retirable ===")
    s, j = constraint(TOK, NS_SUBJ, "Never deploy to production on Fridays.")
    check("untagged constraint write accepted (derived, not rejected)", s == 200, f"{s} {j}")
    subj = j.get("constraint_subject") or ""
    check("response carries the derived subject", subj.startswith("auto:never-deploy-to-production"), subj)
    check("response marks it derived", j.get("constraint_subject_derived") is True, str(j))
    rows = live_constraints(conn, NS_SUBJ)
    check("stored row is TAGGED with that subject (no longer immortal)",
          len(rows) == 1 and rows[0]["constraint_subject"] == subj, str(rows))
    check("derivation is deterministic (matches derive_constraint_subject)",
          subj == derive_constraint_subject("Never deploy to production on Fridays."))

    s, j2 = constraint(TOK, NS_SUBJ, "Never deploy to production without a rollback plan.")
    check("a DIFFERENT rule with the same leading words gets a DIFFERENT subject",
          s == 200 and j2.get("constraint_subject") not in (None, subj), str(j2))
    check("... and does NOT retire the first rule", not j2.get("constraints_retired"), str(j2))
    s, j3 = constraint(TOK, NS_SUBJ, "  ", subject="   ")
    check("whitespace-only text still rejected (400)", s == 400, f"{s} {j3}")
    s, j4 = constraint(TOK, NS_SUBJ, "Always run the test suite before merging.", subject="  ")
    check("blank explicit subject is treated as omitted (derived)",
          s == 200 and j4.get("constraint_subject_derived") is True
          and j4["constraint_subject"].startswith("auto:always-run-the-test"), str(j4))
    s, j5 = constraint(TOK, NS_SUBJ, "CI must pass before merge.", subject="Merge-Policy")
    check("explicit subject is kept (normalized) and not marked derived",
          s == 200 and j5.get("constraint_subject") == "merge-policy"
          and j5.get("constraint_subject_derived") is False, str(j5))

    s, j6 = constraint(TOK, NS_SUBJ, "Never deploy to prod on Fridays or weekends.", subject=subj)
    check("the derived subject is a working retirement handle (supersession)",
          s == 200 and any(r["kind"] == "turn" and r["id"] == j["turn_id"]
                           for r in j6.get("constraints_retired") or []), str(j6))
    s, rc = call("/recall", TOK, {"namespace": NS_SUBJ, "query": "deploy policy"})
    ctx = rc.get("context", "")
    check("retired (formerly untagged) constraint is no longer injected",
          "CONSTRAINT: Never deploy to production on Fridays." not in ctx
          and "CONSTRAINT: Never deploy to prod on Fridays or weekends." in ctx, ctx[:400])

    s, jd = constraint(TOK, NS_SUBJ, "CI must pass before merge.", subject="merge-policy")
    check("re-saving identical text supersedes its duplicate",
          s == 200 and len(jd.get("constraints_retired") or []) == 1, str(jd))

    print("=== 1b. legacy untagged rows are backfilled on the next constraint write ===")
    legacy_text = "Legacy rule: NEVER print secrets in logs."
    lid = store.insert_raw_turn(SCHEMA, NS_OTHER, None, None, legacy_text,
                                __import__("datetime").datetime.now(
                                    __import__("datetime").timezone.utc), None,
                                memory_type="constraint", constraint_subject=None)
    check("precondition: legacy row starts untagged",
          live_constraints(conn, NS_OTHER)[0]["constraint_subject"] is None)
    s, _ = constraint(TOK, NS_OTHER, "Unrelated new rule.", subject="unrelated")
    rows = {r["id"]: r for r in live_constraints(conn, NS_OTHER)}
    legacy_subj = rows[lid]["constraint_subject"]
    check("legacy row backfilled with its derived subject",
          legacy_subj == derive_constraint_subject(legacy_text), str(rows[lid]))
    check("backfill retired nothing", rows[lid]["constraint_retired_at"] is None)
    s, jl = constraint(TOK, NS_OTHER, "NEVER print secrets or tokens in logs.", subject=legacy_subj)
    check("legacy (formerly immortal) row is now retirable via its backfilled subject",
          s == 200 and {"kind": "turn", "id": lid} in (jl.get("constraints_retired") or []), str(jl))

    # ---------------------------------------------------------------- 2. budget guard
    print("=== 2. production scenario: 6 constraints totaling 10,040 chars ===")
    budget = PINNED_BUDGET_CHARS_DEFAULT
    # 6 rules whose content totals exactly 10,040 chars (4 x 1673 + 2 x 1674)
    sizes = [1673, 1673, 1673, 1673, 1674, 1674]
    check("fixture really totals 10,040 chars", sum(sizes) == 10040)
    accepted, rejected = [], None
    for i, n in enumerate(sizes):
        s, j = constraint(TOK, NS_PROD, rule(i, n))
        if s == 200:
            accepted.append(j)
        else:
            rejected = (i, s, j)
            break
    check("guard fires before the pinned set can exceed the budget",
          rejected is not None and rejected[1] == 409, str(rejected)[:300])
    total_live = sum(len("CONSTRAINT: ") + len(r["text"]) for r in live_constraints(conn, NS_PROD)
                     if r["constraint_retired_at"] is None)
    check(f"live pinned total stays within the {budget}-char budget", total_live <= budget,
          str(total_live))
    if rejected:
        i, s, j = rejected
        cb = j.get("constraint_budget") or {}
        check("rejected write saved NOTHING",
              not any(r["text"] == rule(i, sizes[i]) for r in live_constraints(conn, NS_PROD)))
        check("error message is clear and actionable",
              "constraint rejected (nothing saved)" in j.get("error", "")
              and "--subject" in j.get("error", ""), j.get("error", ""))
        cands = cb.get("candidates") or []
        check("error names retirement candidates by id / subject / size / age",
              len(cands) == len(accepted) and all(
                  c["id"].startswith("turn:") and c["subject"] and c["chars"] > 0
                  and c["age_days"] is not None for c in cands), json.dumps(cands)[:300])
        check("projected usage reported over budget",
              cb.get("projected_chars", 0) > budget and cb.get("budget_chars") == budget, str(cb)[:200])
        with conn.cursor() as c:
            c.execute("SELECT count(*) AS n FROM memnos_control.audit_log WHERE namespace=%s "
                      "AND action='remember' AND ok=false AND detail->>'reason'='constraint_budget'",
                      (NS_PROD,))
            check("rejection is audited", c.fetchone()["n"] >= 1)

    print("=== 2b. an ALREADY over-budget namespace (pre-guard state) can be repaired ===")
    # Recreate the exact incident state directly (bypassing the guard, as pre-#153 writes
    # did): 6 UNTAGGED constraints = 10,040 chars, plus ranked memories.
    with conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (NS_PROD,))
    import datetime as _dt
    for i, n in enumerate(sizes):
        store.insert_raw_turn(SCHEMA, NS_PROD, None, None, rule(i, n),
                              _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30 - i),
                              None, memory_type="constraint", constraint_subject=None)
    for k in range(8):
        s, _ = call("/remember", TOK, {"namespace": NS_PROD,
                                       "text": f"Staging database runs Postgres 16 on host{k}."})
    s, rc = call("/recall", TOK, {"namespace": NS_PROD, "query": "staging database postgres"})
    ctx = rc.get("context", "")
    ranked = [l for l in ctx.split("\n") if l.startswith("- (")]
    print(f"    (incident state: {ctx.count('CONSTRAINT:')} pins + {len(ranked)} ranked lines "
          f"rendered from 10,040 chars of pins)")
    s, j = constraint(TOK, NS_PROD, "One more short rule.")
    check("new constraint on the over-budget namespace is rejected (409)", s == 409, f"{s} {str(j)[:200]}")
    cands = (j.get("constraint_budget") or {}).get("candidates") or []
    check("candidates carry backfilled (retirable) subjects",
          len(cands) == 6 and all((c["subject"] or "").startswith("auto:rule-") for c in cands),
          json.dumps(cands)[:300])
    check("candidates are ordered largest first",
          [c["chars"] for c in cands] == sorted([c["chars"] for c in cands], reverse=True))
    # RATCHET: supersede each oversized rule with a short one. Every step shrinks usage,
    # so every step is accepted even while the namespace is still over budget.
    ok_all = True
    for c in cands:
        s, jj = constraint(TOK, NS_PROD, f"Short form of {c['id']}: follow deploy policy.",
                           subject=c["subject"])
        ok_all = ok_all and s == 200 and len(jj.get("constraints_retired") or []) == 1
    check("each shrinking supersession accepted while still over budget (ratchet)", ok_all)
    s, rc = call("/recall", TOK, {"namespace": NS_PROD, "query": "staging database postgres"})
    ctx = rc.get("context", "")
    ranked = [l for l in ctx.split("\n") if l.startswith("- (")]
    check("after repair: all 6 (short) constraints render", ctx.count("CONSTRAINT:") == 6, ctx[:300])
    check("after repair: ranked memories render too", len(ranked) >= 5, ctx[-400:])
    s, j = constraint(TOK, NS_PROD, "One more short rule.")
    check("after repair: a small new constraint is accepted again", s == 200, f"{s} {j}")

    print("=== 2c. count cap ===")
    for i in range(PINNED_MAX_COUNT_DEFAULT):
        s, j = constraint(TOK, NS_COUNT, f"Small rule number {i}.")
        if s != 200:
            break
    check(f"{PINNED_MAX_COUNT_DEFAULT} small constraints accepted", s == 200, f"{s} {j}")
    s, j = constraint(TOK, NS_COUNT, "Small rule number eleven.")
    cb = j.get("constraint_budget") or {}
    check(f"constraint #{PINNED_MAX_COUNT_DEFAULT + 1} rejected (409): would never be injected",
          s == 409 and cb.get("projected_count") == PINNED_MAX_COUNT_DEFAULT + 1
          and "never injected" in j.get("error", ""), f"{s} {str(j)[:300]}")
    check("count rejection saved nothing",
          len([r for r in live_constraints(conn, NS_COUNT) if r["constraint_retired_at"] is None])
          == PINNED_MAX_COUNT_DEFAULT)
    first_subj = cb["candidates"][0]["subject"] if cb.get("candidates") else None
    s, j = constraint(TOK, NS_COUNT, "Merged: small rules 0 and eleven.", subject=first_subj)
    check("at the cap, superseding an existing constraint is still accepted (netted)",
          s == 200 and len(j.get("constraints_retired") or []) == 1, f"{s} {j}")

    # ---------------------------------------------------------------- 3. normal case
    print("=== 3. no regression: small pins + many ranked memories render together ===")
    pins = ["Always write tests first.", "Never force-push to master.",
            "Use UTC timestamps everywhere."]
    for p in pins:
        s, _ = constraint(TOK, NS_NORMAL, p)
        check(f"small constraint accepted: {p!r}", s == 200)
    for k in range(20):
        call("/remember", TOK, {"namespace": NS_NORMAL,
                                "text": f"The billing service {k} is written in Go and owned by team {k}."})
    s, rc = call("/recall", TOK, {"namespace": NS_NORMAL, "query": "billing service team"})
    ctx = rc.get("context", "")
    lines = ctx.split("\n")
    ranked = [l for l in lines if l.startswith("- (")]
    check("recall 200", s == 200)
    check("all 3 constraints render", all(f"CONSTRAINT: {p}" in ctx for p in pins), ctx[:300])
    check("pins lead the context block",
          all(l.startswith("CONSTRAINT:") for l in lines[:3]), str(lines[:4]))
    check("ranked memories render after the pins", len(ranked) >= 5, ctx[:600])

    cleanup(conn)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
