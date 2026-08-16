"""No-AI tests for CONSTRAINT PRECEDENCE (issue #83) + CONSTRAINT SUPERSESSION (issue #84).

- `constraint_subject` (optional, on /remember when type=constraint) is an AUTHOR-supplied
  grouping key — normalized (stripped + lowercased) at write time, NEVER LLM-inferred
  (issue #29). Two constraints only ever compete for precedence or supersession when they
  share memory_type='constraint' AND a non-null, equal constraint_subject.
- PRECEDENCE (#83): within one recall's already-co-injected namespace set (grounded via
  `namespace_links` — this test does NOT exercise epic #70 item 1's live inheritance,
  which is out of scope), two constraints sharing a subject across DIFFERENT namespaces
  are adjudicated by ':'-prefix ancestry — the ancestor wins by default, an explicit
  `constraint override` edge lets the descendant win instead, and unrelated (sibling)
  namespaces are left alone. The loser is excluded from /recall's `memories` + `context`
  but still audit-logged (`constraint.suppress`) — detected, not silently dropped.
  `constraint override` edges are NAMESPACE-PAIR scoped (not per-subject): once
  declared between two namespaces, the child wins EVERY conflict against that specific
  parent, not just one subject's — the coarse-grained, namespace-level authority model
  the rest of the control plane already uses (grants, namespace_links), rather than a
  finer per-rule dimension the issue didn't ask for.
- SUPERSESSION (#84): a newer constraint with the same (namespace, constraint_subject)
  immediately retires the older one (`constraint.retire` audit event); the retired row
  stops being injected and is excluded from precedence adjudication entirely (it can never
  separately trigger a suppression event against its own successor).
- `BrainStore.resolve_constraint_precedence` / `_is_ancestor_ns` are also exercised
  directly (no DB) for the cycle-safety fail-open path and the ancestor-string edge case.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python tests/test_constraint_precedence_supersession.py
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"

NS_PARENT = "test:cps:org:acme"
NS_CHILD = "test:cps:org:acme:widgets"
NS_SIBLING = "test:cps:org:beta"
ALL_NS = (NS_PARENT, NS_CHILD, NS_SIBLING)
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


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        for ns in ALL_NS:
            for t in ("edges", "entities", "semantic", "episodic", "raw_turns"):
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.namespace_links WHERE src_ns=%s OR dst_ns=%s",
                      (ns, ns))
            c.execute("DELETE FROM memnos_control.constraint_overrides "
                      "WHERE child_namespace=%s OR parent_namespace=%s", (ns, ns))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (ns,))
            # this test's own count-based assertions ("exactly one constraint.suppress
            # row") depend on a clean slate — same pattern test_constraint_enforce_hook.py
            # uses for action='constraint.enforce'. Without this, a second run against a
            # non-fresh DB (no schema drop between runs, e.g. inside the full `for t in
            # tests/test_*.py` suite loop) accumulates rows from prior runs and every
            # exact-count check fails, even though the underlying precedence/supersession
            # behavior is correct.
            c.execute("DELETE FROM memnos_control.audit_log WHERE namespace=%s "
                      "AND action IN ('constraint.suppress','constraint.retire')", (ns,))
        for p in ("cps-admin",):
            c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals pr "
                      "WHERE t.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals pr "
                      "WHERE g.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.principals WHERE name=%s", (p,))


def audit_rows(conn, action, namespace=None):
    with conn.cursor() as c:
        if namespace:
            c.execute("SELECT * FROM memnos_control.audit_log WHERE action=%s AND namespace=%s "
                      "ORDER BY id", (action, namespace))
        else:
            c.execute("SELECT * FROM memnos_control.audit_log WHERE action=%s ORDER BY id",
                      (action,))
        return c.fetchall()


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)

    admin_id = Control.create_principal(conn, "cps-admin", "service")
    Control.grant(conn, admin_id, "*")
    TADM = Control.mint_token(conn, admin_id, "t")

    # Grounded recall wiring: NS_CHILD queries also pull NS_PARENT + NS_SIBLING (the ONLY
    # mechanism by which two namespaces' pinned constraints co-inject today — see
    # memnos_server.py `pin_nss = [ns] + grounded`. Precedence adjudicates constraints that
    # already co-inject; it does not make them co-inject (that's epic #70 item 1).
    Control.link_namespaces(conn, NS_CHILD, NS_PARENT, created_by=admin_id)
    Control.link_namespaces(conn, NS_CHILD, NS_SIBLING, created_by=admin_id)

    print("=== pure function: _is_ancestor_ns ===")
    check("'org:acme' is an ancestor of 'org:acme:widgets'",
          BrainStore._is_ancestor_ns("org:acme", "org:acme:widgets"))
    check("'org:acme' is NOT an ancestor of 'org:acme-widgets' (no ':' boundary)",
          not BrainStore._is_ancestor_ns("org:acme", "org:acme-widgets"))
    check("a namespace is not its own ancestor",
          not BrainStore._is_ancestor_ns("org:acme", "org:acme"))

    print("=== pure function: resolve_constraint_precedence — cycle fails OPEN ===")
    root = {"id": 1, "kind": "turn", "namespace": "c:root", "constraint_subject": "cyc",
            "content": "root rule"}
    mid = {"id": 2, "kind": "turn", "namespace": "c:root:mid", "constraint_subject": "cyc",
           "content": "mid rule"}
    leaf = {"id": 3, "kind": "turn", "namespace": "c:root:mid:leaf", "constraint_subject": "cyc",
            "content": "leaf rule"}
    # only ONE pair reversed (leaf wins over root) in a 3-level chain -> root beats mid,
    # mid beats leaf, leaf beats root: a genuine cycle.
    overrides = {("c:root:mid:leaf", "c:root")}
    winners, losers = BrainStore.resolve_constraint_precedence([root, mid, leaf], overrides)
    check("cyclic partial-override group: all 3 returned as winners (fail open)",
          {w["id"] for w in winners} == {1, 2, 3})
    check("cyclic partial-override group: zero losers", losers == [])

    print("=== A: precedence default — ANCESTOR wins ===")
    now = datetime.now(timezone.utc)
    id_parent_a = store.insert_raw_turn(SCHEMA, NS_PARENT, None, "user",
        "Always require 2 reviewers before merge.", now, None,
        memory_type="constraint", constraint_subject="policy-a")
    id_child_a = store.insert_raw_turn(SCHEMA, NS_CHILD, None, "user",
        "1 reviewer is enough for this project.", now + timedelta(seconds=1), None,
        memory_type="constraint", constraint_subject="policy-a")
    s, j = call("/recall", TADM, {"namespace": NS_CHILD, "query": "unrelated weather query",
                                  "constraint_cap": 50})
    check("recall 200", s == 200)
    # NOTE: pinned-constraint precedence adjudicates the ALWAYS-INJECT-REGARDLESS-OF-
    # RELEVANCE pin path only (issue #83's "recall/injection output" is that guaranteed
    # channel) — it does not erase the underlying raw_turn from the corpus, so ordinary
    # relevance-ranked search can still surface a suppressed constraint's text with a
    # `score` (no `pinned` flag), same as any other memory. The correct assertion is
    # therefore "absent from the PINNED subset", not "absent from `memories` entirely".
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    pin_contents = [m["content"] for m in pins]
    check("winner (ancestor NS_PARENT) present as a PIN",
          "Always require 2 reviewers before merge." in pin_contents)
    check("loser (child) ABSENT from the PINS",
          "1 reviewer is enough for this project." not in pin_contents)
    check("loser not rendered as a 'CONSTRAINT:' (pinned) context line",
          "CONSTRAINT: 1 reviewer is enough for this project." not in j.get("context", ""))
    rows = audit_rows(conn, "constraint.suppress", NS_CHILD)
    rows = [r for r in rows if r["detail"].get("subject") == "policy-a"]
    check("exactly one constraint.suppress audit row for policy-a", len(rows) == 1)
    check("suppress row cites the loser's own id",
          rows and rows[0]["detail"]["constraint_id"] == f"turn:{id_child_a}")
    check("suppress row cites the ancestor as winner",
          rows and rows[0]["detail"]["winner_namespace"] == NS_PARENT
          and rows[0]["detail"]["winner_constraint_id"] == f"turn:{id_parent_a}")

    print("=== B: explicit override reverses the default ===")
    id_parent_b = store.insert_raw_turn(SCHEMA, NS_PARENT, None, "user",
        "Deploys happen only on Fridays.", now, None,
        memory_type="constraint", constraint_subject="policy-b")
    id_child_b = store.insert_raw_turn(SCHEMA, NS_CHILD, None, "user",
        "Deploys happen any day for this project.", now + timedelta(seconds=1), None,
        memory_type="constraint", constraint_subject="policy-b")
    try:
        oid = Control.add_constraint_override(conn, NS_CHILD, NS_PARENT, created_by=admin_id)
    except ValueError as e:
        oid = None
        print(f"  (override add raised: {e})")
    check("override edge created", oid is not None)
    s, j = call("/recall", TADM, {"namespace": NS_CHILD, "query": "unrelated weather query",
                                  "constraint_cap": 50})
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    pin_contents = [m["content"] for m in pins]
    check("override reverses default: CHILD's rule now wins (is a PIN)",
          "Deploys happen any day for this project." in pin_contents)
    check("override reverses default: PARENT's rule now suppressed (NOT a pin)",
          "Deploys happen only on Fridays." not in pin_contents)
    rows = audit_rows(conn, "constraint.suppress", NS_PARENT)
    rows = [r for r in rows if r["detail"].get("subject") == "policy-b"]
    check("suppress audit row exists for the NOW-losing ancestor",
          len(rows) == 1 and rows[0]["detail"]["winner_namespace"] == NS_CHILD)
    check("Control.add_constraint_override rejects a non-ancestor pair",
          _rejects_non_ancestor(conn, admin_id))
    # override edges are NAMESPACE-PAIR scoped, not per-subject (a deliberate design
    # choice — see the PR description): once declared, CHILD wins EVERY conflict against
    # this specific PARENT, not just policy-b's. Remove it now so scenarios C onward
    # (which reuse the same NS_CHILD/NS_PARENT pair under different subjects) exercise
    # the DEFAULT rule again — this removal is itself proof the scoping is per-pair, not
    # a test artifact: leaving it in place would make every later ancestor-wins
    # assertion fail, exactly as observed while developing this test.
    check("override removed", Control.remove_constraint_override(conn, oid))

    print("=== C: unrelated (sibling) namespaces — no default verdict, both survive ===")
    id_child_c = store.insert_raw_turn(SCHEMA, NS_CHILD, None, "user",
        "Widgets team standup is at 9am.", now, None,
        memory_type="constraint", constraint_subject="policy-c")
    id_sib_c = store.insert_raw_turn(SCHEMA, NS_SIBLING, None, "user",
        "Beta team standup is at 10am.", now + timedelta(seconds=1), None,
        memory_type="constraint", constraint_subject="policy-c")
    s, j = call("/recall", TADM, {"namespace": NS_CHILD, "query": "unrelated weather query",
                                  "constraint_cap": 50})
    pin_contents = [m["content"] for m in j.get("memories", []) if m.get("pinned")]
    check("sibling A survives (no ancestor relation => no verdict), still pinned",
          "Widgets team standup is at 9am." in pin_contents)
    check("sibling B survives too, still pinned",
          "Beta team standup is at 10am." in pin_contents)
    rows = audit_rows(conn, "constraint.suppress")
    rows = [r for r in rows if r["detail"].get("subject") == "policy-c"]
    check("no suppression audit for the sibling pair", rows == [])

    print("=== D: supersession — newer constraint retires the older, same namespace ===")
    s, j = call("/remember", TADM, {"namespace": NS_CHILD, "type": "constraint",
                                    "constraint_subject": "policy-d",
                                    "text": "Rotate the API key every 90 days."})
    check("first write 200", s == 200)
    tid_v1 = j["turn_id"]
    check("first write reports no retirements", not j.get("constraints_retired"))
    s, j = call("/remember", TADM, {"namespace": NS_CHILD, "type": "constraint",
                                    "constraint_subject": "Policy-D",   # case-insensitive match
                                    "text": "Rotate the API key every 30 days."})
    check("second write 200", s == 200)
    tid_v2 = j["turn_id"]
    check("second write reports v1 retired",
          j.get("constraints_retired") == [{"kind": "turn", "id": tid_v1}])
    with conn.cursor() as c:
        c.execute(f"SELECT constraint_retired_at, constraint_retired_by FROM {SCHEMA}.raw_turns "
                  f"WHERE id=%s", (tid_v1,))
        r1 = c.fetchone()
        c.execute(f"SELECT constraint_retired_at FROM {SCHEMA}.raw_turns WHERE id=%s", (tid_v2,))
        r2 = c.fetchone()
    check("v1 stamped retired", r1["constraint_retired_at"] is not None)
    check("v1's retired_by cites v2", r1["constraint_retired_by"] == f"turn:{tid_v2}")
    check("v2 stays live", r2["constraint_retired_at"] is None)
    s, j = call("/recall", TADM, {"namespace": NS_CHILD, "query": "unrelated weather query",
                                  "constraint_cap": 50})
    # "stops being injected" (issue #84) means the PINNED/guaranteed-injection channel —
    # same scope boundary as A/B above. The retired raw_turn stays in the append-only
    # audit trail and (like superseded facts elsewhere in this codebase — valid_to is
    # set, never deleted) can still surface via ordinary relevance-ranked search; it is
    # simply no longer force-injected regardless of relevance.
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    pin_contents = [m["content"] for m in pins]
    check("only v2 (30 days) is a PIN", "Rotate the API key every 30 days." in pin_contents)
    check("v1 (90 days) stops being PINNED once retired",
          "Rotate the API key every 90 days." not in pin_contents)
    rows = audit_rows(conn, "constraint.retire", NS_CHILD)
    rows = [r for r in rows if r["detail"].get("subject") == "policy-d"]
    check("exactly one constraint.retire audit row for policy-d", len(rows) == 1)
    check("retire row's detail matches the retirement",
          rows and rows[0]["detail"]["retired"] == [{"kind": "turn", "id": tid_v1}]
          and rows[0]["detail"]["superseded_by"] == f"turn:{tid_v2}")

    print("=== E: interaction — supersession + precedence never double-count ===")
    # NS_PARENT writes a competing constraint under the SAME subject as D's v1/v2.
    id_parent_e = store.insert_raw_turn(SCHEMA, NS_PARENT, None, "user",
        "Rotate the API key every 7 days (org standard).", now, None,
        memory_type="constraint", constraint_subject="policy-d")
    s, j = call("/recall", TADM, {"namespace": NS_CHILD, "query": "unrelated weather query",
                                  "constraint_cap": 50})
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    pin_contents = [m["content"] for m in pins]
    check("ancestor's rule wins over v2 (the only live CHILD row) — is a PIN",
          "Rotate the API key every 7 days (org standard)." in pin_contents)
    check("v2 (CHILD's live row) suppressed by precedence — not a pin",
          "Rotate the API key every 30 days." not in pin_contents)
    check("v1 (retired) never resurfaces as a pin either",
          "Rotate the API key every 90 days." not in pin_contents)
    retire_rows = [r for r in audit_rows(conn, "constraint.retire", NS_CHILD)
                   if r["detail"].get("subject") == "policy-d"]
    suppress_rows_v2 = [r for r in audit_rows(conn, "constraint.suppress", NS_CHILD)
                        if r["detail"].get("subject") == "policy-d"]
    check("still exactly ONE retire event (unchanged by the later precedence check)",
          len(retire_rows) == 1)
    check("exactly ONE suppress event, and it names v2 — never v1 (v1 is retired, "
          "excluded from adjudication entirely, so it can't separately lose to its own "
          "successor or to the ancestor)",
          len(suppress_rows_v2) == 1
          and suppress_rows_v2[0]["detail"]["constraint_id"] == f"turn:{tid_v2}")
    check("no suppress event ever names v1",
          all(r["detail"]["constraint_id"] != f"turn:{tid_v1}"
              for r in audit_rows(conn, "constraint.suppress")))

    print("=== F: cap ordering — a precedence loser must not shrink the returned cap ===")
    base = now + timedelta(minutes=10)
    id_child_f = store.insert_raw_turn(SCHEMA, NS_CHILD, None, "user",
        "Ship on the child cadence.", base, None,
        memory_type="constraint", constraint_subject="policy-f")
    id_parent_f = store.insert_raw_turn(SCHEMA, NS_PARENT, None, "user",
        "Ship on the org cadence.", base + timedelta(seconds=1), None,
        memory_type="constraint", constraint_subject="policy-f")
    for i in range(11):
        store.insert_raw_turn(SCHEMA, NS_CHILD, None, "user",
            f"Untagged CHILD rule {i} for cap testing.", base + timedelta(minutes=i + 1), None,
            memory_type="constraint")
    s, j = call("/recall", TADM, {"namespace": NS_CHILD, "query": "unrelated weather query",
                                  "constraint_cap": 10})
    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    check("exactly `cap`=10 pins returned despite one candidate losing precedence",
          len(pins) == 10)
    pin_contents = [m["content"] for m in pins]
    check("the precedence WINNER (org cadence) fills a slot",
          "Ship on the org cadence." in pin_contents)
    check("the precedence LOSER (child cadence) does not",
          "Ship on the child cadence." not in pin_contents)

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)


def _rejects_non_ancestor(conn, admin_id):
    try:
        Control.add_constraint_override(conn, "unrelated:ns:a", "unrelated:ns:b",
                                        created_by=admin_id)
        return False
    except ValueError:
        return True


if __name__ == "__main__":
    main()
