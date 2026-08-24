"""No-AI tests for issue #107: cross-namespace constraint inheritance — an org-level
namespace's ingested corpus/constraints are automatically visible to (and enforced
against) its child project namespaces via corpus_check / corpus_check_diff, reusing
issue #85's same-root ancestor mechanism (Control.effective_ancestors) rather than a
parallel hierarchy. Covers all four acceptance criteria:

  1. corpus_check in a child namespace searches ancestor namespaces by default
  2. results carry a `namespace` field so the caller can see where a constraint
     actually lives (own namespace vs. an inherited ancestor)
  3. ancestor search can be disabled with inherit=False for backwards-compatible
     behaviour (and is gated per-ancestor by the CALLING token's own read grant --
     an ancestor without a grant is skipped, not silently leaked)
  4. a propagation alert (a "memnos:corpus_propagation" system event, same
     convention as the existing memnos:lease events) fires into every descendant
     namespace that already has its own corpus docs, when a parent's constraints
     are added OR updated (re-ingested)

Also pins the fair-share ranking fix: a leaf namespace with >= k of its own FTS
matches must never silently starve a real ancestor-only match out of the results.

Pure FTS + string namespace hierarchy, no LLM/embeddings — same discipline as
test_corpus_api.py / test_corpus_diff_api.py.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_corpus_inheritance.py
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"

# 3-level org hierarchy: ORG (org-wide rules) -> ENG (never gets its own corpus doc,
# deliberately, so propagation/descendant-detection can prove it correctly skips a
# level with no corpus of its own) -> PROJA (leaf, the project namespace).
ORG = "test:corpusinherit107:acme"
ENG = "test:corpusinherit107:acme:eng"
PROJA = "test:corpusinherit107:acme:eng:projA"
ALL_NS = (ORG, ENG, PROJA)

PASS = FAIL = 0

# 12 distinct leaf-level constraints (all share the distinctive term "gizmocache" so a
# snippet mentioning it FTS-matches all 12) -- enough to exceed corpus_check's default
# k=10 on their own, so a naive specificity-sort-then-truncate would starve the org's
# single relevant rule below out of the results entirely.
LEAF_DOC = "\n".join(f"- Gizmocache slot {i} entries MUST expire within five minutes." for i in range(1, 13))
ORG_DOC = "- Gizmocache access SHALL be audited for compliance.\n"


def call(method, path, token=None, body=None):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        for ns in ALL_NS:
            c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s AND kind='constraint'", (ns,))
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s AND speaker='memnos:corpus_propagation'",
                      (ns,))
            c.execute("DELETE FROM memnos_control.corpus_sources WHERE namespace=%s", (ns,))


def by_ns(entries, ns):
    return [e for e in entries if e.get("namespace") == ns]


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup(conn)

    # full_id: an org-wide reader/writer, granted on all three levels -- the scenario
    # the issue's "Why this matters" describes (an org security team + a project agent
    # that both hold legitimate access to the whole subtree).
    full_id = Control.create_principal(conn, "test-ci107-full", "agent")
    full_tok = Control.mint_token(conn, full_id, "test")
    for ns in ALL_NS:
        Control.grant(conn, full_id, ns, can_read=True, can_write=True)

    # leaf_id: a project-scoped principal with a grant ONLY on the leaf -- proves the
    # ancestor gate is per-CALLER, not per-namespace-existence (issue #85's own
    # "an ancestor without a read grant is skipped" contract, reused here).
    leaf_id = Control.create_principal(conn, "test-ci107-leaf", "agent")
    leaf_tok = Control.mint_token(conn, leaf_id, "test")
    Control.grant(conn, leaf_id, PROJA, can_read=True, can_write=True)

    print("=== cross-namespace constraint inheritance (issue #107) ===")

    # --- ingest leaf's own corpus first (PROJA has no descendants yet -> no propagation)
    s, j = call("POST", "/corpus/ingest", full_tok,
                {"namespace": PROJA, "name": "leaf-gizmocache", "text": LEAF_DOC, "kind": "lld"})
    check("leaf ingest 200", s == 200)
    check("leaf ingest extracted 12 constraints", j.get("constraints") == 12, j)
    check("leaf ingest (no descendants) carries no propagation key", "propagation" not in j, j)

    # --- AC 4: ingest the org rule -- PROJA already has a corpus, so this is a
    # propagation-worthy ADD. ENG (in between) has never ingested anything, so it must
    # be silently skipped, not notified.
    s, j = call("POST", "/corpus/ingest", full_tok,
                {"namespace": ORG, "name": "org-gizmocache-rule", "text": ORG_DOC, "kind": "policy"})
    check("org ingest 200", s == 200)
    check("org ingest extracted 1 constraint", j.get("constraints") == 1, j)
    prop = j.get("propagation")
    check("org ingest (add) fires propagation to PROJA", bool(prop) and any(p["namespace"] == PROJA for p in prop), j)
    check("ENG (no corpus of its own) is NOT a propagation target",
          not any(p.get("namespace") == ENG for p in (prop or [])), j)

    with conn.cursor() as c:
        c.execute(f"SELECT text FROM {SCHEMA}.raw_turns WHERE namespace=%s "
                  f"AND speaker='memnos:corpus_propagation' ORDER BY id", (PROJA,))
        events_after_add = c.fetchall()
    check("exactly one propagation event landed in PROJA's own raw_turns (not just ORG's)",
          len(events_after_add) == 1, events_after_add)
    if events_after_add:
        payload = json.loads(events_after_add[0]["text"])
        check("propagation event names the org source + namespace",
              payload.get("source") == "org-gizmocache-rule" and payload.get("namespace") == ORG, payload)

    # --- AC 4 continued: re-ingest (UPDATE) the same org source must fire again ---
    s, j = call("POST", "/corpus/ingest", full_tok,
                {"namespace": ORG, "name": "org-gizmocache-rule", "text": ORG_DOC, "kind": "policy"})
    check("org re-ingest (update) 200", s == 200)
    check("org re-ingest (update) ALSO fires propagation", bool(j.get("propagation")), j)
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) AS n FROM {SCHEMA}.raw_turns WHERE namespace=%s "
                  f"AND speaker='memnos:corpus_propagation'", (PROJA,))
        n_events = c.fetchone()["n"]
    check("re-ingest produced a SECOND propagation event (added-or-updated, not add-only)",
          n_events == 2, n_events)

    # --- AC 1 + AC 2 + fair-share ranking: corpus_check on the LEAF, full grant token ---
    s, j = call("POST", "/corpus/check", full_tok, {"namespace": PROJA, "snippet": "gizmocache slot entries"})
    check("corpus_check (leaf, full grant) 200", s == 200)
    cons = j.get("constraints", [])
    check("default k=10 results returned", len(cons) == 10, len(cons))
    check("every result carries a namespace field", all("namespace" in c for c in cons), cons)
    org_hits = by_ns(cons, ORG)
    leaf_hits = by_ns(cons, PROJA)
    check("fair-share ranking: the org's single relevant rule is NOT starved out "
          "by the leaf's 12 own matches", len(org_hits) == 1, cons)
    check("fair-share ranking: remaining slots filled from the leaf (most-specific-first)",
          len(leaf_hits) == 9, cons)
    check("org constraint content is the audited rule",
          org_hits and "audited" in org_hits[0]["content"].lower(), org_hits)
    check("inherited_in includes ENG and ORG (both readable by full_tok)",
          set(j.get("inherited_in", [])) >= {ENG, ORG}, j.get("inherited_in"))
    # full_tok is granted ORG/ENG/PROJA only -- namespaces ABOVE ORG in the ':'-prefix
    # chain ("test:corpusinherit107", "test") are real ancestors too but were never
    # granted, so they correctly land in inheritance_skipped rather than ORG/ENG.
    check("inheritance_skipped for the full-grant token never includes ORG or ENG "
          "(only ancestors above the granted subtree)",
          not ({ORG, ENG} & set(j.get("inheritance_skipped", []))), j.get("inheritance_skipped"))

    # --- AC 3 negative path / leak guard: leaf-only token must NOT see the org rule ---
    s, j = call("POST", "/corpus/check", leaf_tok, {"namespace": PROJA, "snippet": "gizmocache slot entries"})
    check("corpus_check (leaf, leaf-only grant) 200", s == 200)
    cons2 = j.get("constraints", [])
    check("leaf-only token sees ONLY its own namespace's constraints",
          cons2 and all(c["namespace"] == PROJA for c in cons2), cons2)
    check("leaf-only token: org's rule never leaks in", not by_ns(cons2, ORG), cons2)
    check("inheritance_skipped surfaces ORG (ungranted ancestor), not silently dropped",
          ORG in j.get("inheritance_skipped", []), j.get("inheritance_skipped"))

    # --- AC 3 explicit opt-out: inherit=False, even with a FULL grant ---
    s, j = call("POST", "/corpus/check", full_tok,
                {"namespace": PROJA, "snippet": "gizmocache slot entries", "inherit": False})
    check("corpus_check (inherit=False) 200", s == 200)
    cons3 = j.get("constraints", [])
    check("inherit=False: only the leaf's own namespace, even for a fully-granted token",
          cons3 and all(c["namespace"] == PROJA for c in cons3), cons3)
    check("inherit=False: response omits inherited_in/inheritance_skipped entirely "
          "(exact pre-#107 response shape)",
          "inherited_in" not in j and "inheritance_skipped" not in j, j)

    # --- corpus_check_diff: same inheritance, gated the same way ---
    diff = ("--- a/audit.py\n+++ b/audit.py\n@@ -1,1 +1,2 @@\n"
            " def handle(event):\n"
            '+    audit_log.write("gizmocache access audited: " + str(event))\n')
    s, j = call("POST", "/corpus/check_diff", full_tok, {"namespace": PROJA, "diff": diff})
    check("corpus_check_diff (leaf, full grant) 200", s == 200)
    all_entries = j.get("violated", []) + j.get("satisfied", []) + j.get("uncovered", [])
    org_diff_hits = by_ns(all_entries, ORG)
    check("check_diff: org's audited rule is reachable via inheritance",
          bool(org_diff_hits), j)
    check("check_diff: org rule lands in satisfied (diff implements the requirement)",
          by_ns(j.get("satisfied", []), ORG), j)

    s, j = call("POST", "/corpus/check_diff", leaf_tok, {"namespace": PROJA, "diff": diff})
    check("corpus_check_diff (leaf, leaf-only grant) 200", s == 200)
    all_entries2 = j.get("violated", []) + j.get("satisfied", []) + j.get("uncovered", [])
    check("check_diff: leaf-only token never sees the org rule",
          not by_ns(all_entries2, ORG), j)

    cleanup(conn)
    for pid in (full_id, leaf_id):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
