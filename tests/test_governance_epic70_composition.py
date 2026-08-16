"""Cross-feature composition test for epic #70's four just-merged sub-issues (#81/#82/
#83/#84/#85, PRs #90/#91/#93/#95). Each PR's own suite only ever ran against a tree
without the other three -- this proves the ACTUAL composed behavior on the merged tree,
not any one feature in isolation.

Scenario (real Postgres, real HTTP server, no LLM -- free local-384 mode, pure SQL):

  1. A principal is granted access ENTIRELY via ROLE membership (issue #81) -- zero
     direct grants of its own. Every namespace it can touch in this test is reached only
     because it is a member of a role that was granted those namespaces.

  2. Two namespaces in the SAME root tree, no `namespace_links` edge between them:
       PARENT = test:xfc:acme
       CHILD  = test:xfc:acme:widgets     (a ':'-prefix descendant of PARENT)
     CHILD automatically consults PARENT's pinned constraints at recall time purely via
     issue #85's Mechanism A (same-root automatic ancestor inheritance) -- there is no
     explicit link anywhere in this file, which is the point: `inherited_in` must fire,
     `grounded_in`/`links_skipped` must not appear at all.

  3. PARENT gets an OLDER constraint (v1, subject 'deploy-policy'), then a NEWER one
     under the SAME (namespace, subject) -- issue #84 supersession immediately retires
     v1 the moment v2 is written (constraint.retire), before /recall ever runs.

  4. CHILD gets its OWN constraint under the SAME subject. Once CHILD's recall pulls
     PARENT in via #85's ancestor fan-out, PARENT's (live, v2) and CHILD's constraints
     now genuinely co-inject for the first time and issue #83's precedence engine
     adjudicates them: PARENT is the ':'-prefix ancestor, so it wins by default (no
     override declared) -- CHILD's rule is suppressed (constraint.suppress), never
     force-injected, but still visible via ordinary relevance search (not force-pinned).

  5. Issue #82's per-constraint injection audit must show: the retired v1 constraint
     appears in NEITHER a constraint.inject NOR a constraint.suppress row anywhere, ever
     (retirement excludes it from precedence adjudication entirely at the SQL level --
     the "never double-count" invariant #93 tested via grounded links, now proven via
     #85's ancestor-inheritance path instead, which is a genuinely different code path
     feeding the same `pinned_constraints()` call). The one row that DOES inject is
     tagged with its SOURCE namespace (PARENT), not the namespace CHILD recalled on --
     proving audit correctness survives inherited (not just grounded) pins.

Run against a live local server (see tests/test_memory_types.py for the pattern):
    MEMNOS_DSN=... MEMNOS_URL=... python tests/test_governance_epic70_composition.py
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

PARENT = "test:xfc:acme"
CHILD = "test:xfc:acme:widgets"
ALL_NS = (PARENT, CHILD)
ROLE = "xfc-role"
PRINCIPAL = "xfc-agent"
SUBJECT = "deploy-policy"

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


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


def audit_rows(conn, action, namespace=None):
    with conn.cursor() as c:
        if namespace:
            c.execute("SELECT * FROM memnos_control.audit_log WHERE action=%s AND namespace=%s "
                      "ORDER BY id", (action, namespace))
        else:
            c.execute("SELECT * FROM memnos_control.audit_log WHERE action=%s ORDER BY id",
                      (action,))
        return c.fetchall()


def cleanup(conn):
    with conn.cursor() as c:
        for ns in ALL_NS:
            for t in ("semantic", "episodic", "raw_turns"):
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.namespace_links WHERE src_ns=%s OR dst_ns=%s", (ns, ns))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (ns,))
            c.execute("DELETE FROM memnos_control.audit_log WHERE namespace=%s "
                      "AND action IN ('constraint.suppress','constraint.retire','constraint.inject')",
                      (ns,))
        c.execute("SELECT id FROM memnos_control.roles WHERE name=%s", (ROLE,))
        r = c.fetchone()
        if r:
            c.execute("DELETE FROM memnos_control.role_grants WHERE role_id=%s", (r["id"],))
            c.execute("DELETE FROM memnos_control.role_members WHERE role_id=%s", (r["id"],))
            c.execute("DELETE FROM memnos_control.roles WHERE id=%s", (r["id"],))
        c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", (PRINCIPAL,))
        r = c.fetchone()
        if r:
            pid = r["id"]
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.audit_log WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)
    now = datetime.now(timezone.utc)

    # --- setup: access ONLY via role membership (issue #81) --------------------------
    print("=== setup: principal reaches PARENT+CHILD via role membership only (issue #81) ===")
    pid = Control.create_principal(conn, PRINCIPAL, "agent")
    tok = Control.mint_token(conn, pid, "t")
    Control.create_role(conn, ROLE, "cross-feature test role")
    Control.grant_role(conn, ROLE, "test:xfc:*", can_read=True, can_write=True)
    Control.add_role_member(conn, ROLE, pid)
    check("principal has ZERO direct grants of its own",
          Control.authorized_namespaces(conn, pid) == [])
    check("principal can nonetheless read PARENT (via role only)",
          Control.authorize(conn, pid, PARENT, write=False) is True)
    check("principal can nonetheless write CHILD (via role only)",
          Control.authorize(conn, pid, CHILD, write=True) is True)

    # --- D: supersession on PARENT (issue #84) BEFORE any recall happens -------------
    print("=== supersession: v2 retires v1 on PARENT, same subject (issue #84) ===")
    s, j = call("/remember", tok, {"namespace": PARENT, "type": "constraint",
                                    "constraint_subject": SUBJECT,
                                    "text": "Always require 2 reviewers before merge (v1)."})
    check("v1 write 200", s == 200, str(j))
    tid_v1 = j["turn_id"]
    check("v1 write reports no retirements (nothing to supersede yet)",
          not j.get("constraints_retired"))
    s, j = call("/remember", tok, {"namespace": PARENT, "type": "constraint",
                                    "constraint_subject": SUBJECT,
                                    "text": "Always require 3 reviewers before merge (v2, org standard)."})
    check("v2 write 200", s == 200, str(j))
    tid_v2 = j["turn_id"]
    check("v2 write retires v1", j.get("constraints_retired") == [{"kind": "turn", "id": tid_v1}])
    retire_rows = [r for r in audit_rows(conn, "constraint.retire", PARENT)
                   if r["detail"].get("subject") == SUBJECT]
    check("exactly one constraint.retire audit row", len(retire_rows) == 1)
    check("retire row cites v1 as retired, v2 as successor",
          retire_rows and retire_rows[0]["detail"]["retired"] == [{"kind": "turn", "id": tid_v1}]
          and retire_rows[0]["detail"]["superseded_by"] == f"turn:{tid_v2}")

    # --- CHILD writes its own competing constraint, same subject ---------------------
    print("=== CHILD writes its own rule under the SAME subject (sets up #83's conflict) ===")
    s, j = call("/remember", tok, {"namespace": CHILD, "type": "constraint",
                                    "constraint_subject": SUBJECT,
                                    "text": "1 reviewer is enough for this project."})
    check("child write 200", s == 200, str(j))
    tid_child = j["turn_id"]

    # --- the composed recall: #85 fan-out feeds #83 precedence feeds #82 audit -------
    print("=== recall on CHILD: #85 auto-inherits PARENT (no namespace_links anywhere) ===")
    before_wm = 0
    with conn.cursor() as c:
        c.execute("SELECT COALESCE(max(id),0) AS m FROM memnos_control.audit_log")
        before_wm = c.fetchone()["m"]
    s, j = call("/recall", tok, {"namespace": CHILD, "query": "unrelated space weather",
                                  "constraint_cap": 50, "session_id": "xfc-session-1"})
    check("recall 200", s == 200, str(j))
    check("Mechanism A fired: inherited_in reports PARENT",
          j.get("inherited_in") == [PARENT], str(j.get("inherited_in")))
    check("Mechanism B never touched: no grounded_in/links_skipped keys leaked in "
          "(this whole scenario uses zero namespace_links edges)",
          "grounded_in" not in j and "links_skipped" not in j)

    pins = [m for m in j.get("memories", []) if m.get("pinned")]
    pin_contents = [m["content"] for m in pins]
    check("PARENT's LIVE v2 rule wins precedence (ancestor default) and is force-pinned",
          "Always require 3 reviewers before merge (v2, org standard)." in pin_contents)
    check("CHILD's own rule LOSES precedence to its inherited ancestor -- not force-pinned",
          "1 reviewer is enough for this project." not in pin_contents)
    check("v1 (retired before this recall even ran) never resurfaces as a pin",
          "Always require 2 reviewers before merge (v1)." not in pin_contents)
    v2_pin = next((m for m in pins if m["content"].startswith("Always require 3")), None)
    check("the winning pin is tagged with its SOURCE namespace (PARENT), not CHILD -- "
          "inherited-pin transparency, same field grounded pins already carry",
          v2_pin is not None and v2_pin.get("namespace") == PARENT,
          str(v2_pin))

    # --- issue #82: per-constraint injection audit, inherited-pin namespace correctness
    print("=== injection audit (issue #82): fires for the winner, tagged with PARENT ===")
    inject_rows = [r for r in audit_rows(conn, "constraint.inject")
                   if r["id"] > before_wm and r["detail"].get("session_id") == "xfc-session-1"]
    check("exactly one constraint.inject row for this recall (only the winner injects)",
          len(inject_rows) == 1, str(inject_rows))
    check("the inject row's NATIVE namespace column is PARENT (the constraint's own "
          "namespace), even though the caller recalled CHILD -- proves #82's audit "
          "reads the row's real source, not the recall's target namespace",
          inject_rows and inject_rows[0]["namespace"] == PARENT, str(inject_rows))
    check("the inject row cites v2's own constraint_id",
          inject_rows and inject_rows[0]["detail"]["constraint_id"] == f"turn:{tid_v2}",
          str(inject_rows))
    check("NO inject row ever cites v1 (retired) -- retirement excluded it from "
          "adjudication before precedence/audit ever saw it",
          all(r["detail"]["constraint_id"] != f"turn:{tid_v1}"
              for r in audit_rows(conn, "constraint.inject")))
    check("NO inject row ever cites CHILD's own (suppressed) rule",
          all(r["detail"]["constraint_id"] != f"turn:{tid_child}"
              for r in audit_rows(conn, "constraint.inject")))

    # --- issue #83: suppression audit for the loser, and the double-count guard ------
    print("=== suppression audit (issue #83): CHILD's rule loses, v1 never appears ===")
    suppress_rows = [r for r in audit_rows(conn, "constraint.suppress", CHILD)
                      if r["detail"].get("subject") == SUBJECT]
    check("exactly one constraint.suppress row, naming CHILD's rule as the loser",
          len(suppress_rows) == 1
          and suppress_rows[0]["detail"]["constraint_id"] == f"turn:{tid_child}",
          str(suppress_rows))
    check("the suppress row cites PARENT/v2 as the winner (the INHERITED constraint, "
          "reached via #85, not a grounded link)",
          suppress_rows
          and suppress_rows[0]["detail"]["winner_namespace"] == PARENT
          and suppress_rows[0]["detail"]["winner_constraint_id"] == f"turn:{tid_v2}",
          str(suppress_rows))
    check("v1 (retired) NEVER appears as a suppression loser either -- the core "
          "'never double-count a supersession as a second suppression event' "
          "invariant, now proven via #85's ancestor-inheritance path rather than "
          "#93's own grounded-link test",
          all(r["detail"]["constraint_id"] != f"turn:{tid_v1}"
              for r in audit_rows(conn, "constraint.suppress")))
    check("v1 never appears as a WINNER of some other suppression either "
          "(fully excluded from adjudication, not just never losing)",
          all(r["detail"].get("winner_constraint_id") != f"turn:{tid_v1}"
              for r in audit_rows(conn, "constraint.suppress")))

    cleanup(conn)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
