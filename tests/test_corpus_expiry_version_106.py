"""Issue #106: constraint expiry + version scoping on corpus documents.

Exercises:
  - corpus_ingest's optional `since`/`until` (dotted-numeric version strings) --
    validation (malformed string, since >= until) and storage.
  - corpus_check's optional `version` filter: a constraint not yet introduced at
    `version` is dropped; one whose window has lapsed is kept with status "expired";
    boundary behaviour (since inclusive, until exclusive); `version` omitted returns
    every match unfiltered, each still carrying a `status` field (no regression).
  - corpus_deviation: recording an approved, audited exception -- validation
    (missing fields, unknown constraint_id, non-semver `until`), authorization
    (read-only token -> 403), and its effect on corpus_check's `status` (including
    a deviation whose own `until` has itself lapsed at the queried version, which
    must fall back to the constraint's own window state, not stay "approved_deviation").
  - re-ingest doesn't crash when a deviation references an id the re-ingest deletes
    (corpus_deviations has no FK on constraint_id -- see core/control.py's DDL comment).

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_corpus_expiry_version_106.py
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
NS = "test:corpus106"
NS2 = "test:corpus106b"
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

VDOC = "Legacy caches MUST be invalidated on write when the write path touches shared state."
SNIPPET = "invalidate the legacy caches whenever the write path touches shared state"
PLAIN_DOC = "All background jobs MUST report their exit status to the scheduler."
PLAIN_SNIPPET = "background job reporting exit status to the scheduler"


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
        for ns in (NS, NS2):
            c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s AND kind='constraint'", (ns,))
            c.execute("DELETE FROM memnos_control.corpus_sources WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.corpus_deviations WHERE namespace=%s", (ns,))


def status_of(constraints, cid):
    for c in constraints:
        if c["id"] == cid:
            return c
    return None


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup(conn)

    admin_id = Control.create_principal(conn, "test-c106-admin", "service")
    user_id = Control.create_principal(conn, "test-c106-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)
    Control.grant(conn, user_id, NS2, can_read=True, can_write=True)
    ro_id = Control.create_principal(conn, "test-c106-ro", "agent")
    ro_tok = Control.mint_token(conn, ro_id, "test")
    Control.grant(conn, ro_id, NS, can_read=True, can_write=False)

    print("=== issue #106: corpus_ingest since/until ===")
    s, j = call("POST", "/corpus/ingest", user_tok,
                {"namespace": NS, "name": "versioned-doc", "text": VDOC, "since": "1.0.0", "until": "2.1.0"})
    check("ingest with since/until -> 200", s == 200, str(j))
    check("extracted 1 constraint", j.get("constraints") == 1)
    vid = j["ids"][0]

    s, j = call("POST", "/corpus/ingest", user_tok,
                {"namespace": NS, "name": "bad-until", "text": "X MUST Y.", "until": "not-a-version"})
    check("ingest with malformed until -> 400", s == 400, str(j))

    s, j = call("POST", "/corpus/ingest", user_tok,
                {"namespace": NS, "name": "bad-order", "text": "X MUST Y.", "since": "2.0.0", "until": "1.0.0"})
    check("ingest with since >= until -> 400", s == 400, str(j))

    s, j = call("POST", "/corpus/ingest", user_tok, {"namespace": NS, "name": "plain-doc", "text": PLAIN_DOC})
    check("ingest with no since/until (unbounded) -> 200", s == 200, str(j))
    pid = j["ids"][0]

    print("=== issue #106: corpus_check version filtering ===")
    # version omitted: unfiltered, status always present (no regression)
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET})
    check("check (no version) -> 200", s == 200)
    row = status_of(j.get("constraints", []), vid)
    check("versioned constraint returned when version omitted", row is not None)
    check("status present and 'active' when version omitted", row and row.get("status") == "active")

    # version before `since`: not yet introduced -> dropped entirely
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET, "version": "0.9.0"})
    check("check (version before since) -> 200", s == 200)
    check("not-yet-introduced constraint is dropped", status_of(j.get("constraints", []), vid) is None)

    # since boundary: inclusive -> active
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET, "version": "1.0.0"})
    row = status_of(j.get("constraints", []), vid)
    check("since boundary (version == since) is active", row and row.get("status") == "active", str(row))

    # mid-window -> active
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET, "version": "1.5.0"})
    row = status_of(j.get("constraints", []), vid)
    check("mid-window is active", row and row.get("status") == "active", str(row))
    check("since/until echoed on the result", row and row.get("since") == "1.0.0" and row.get("until") == "2.1.0")

    # until boundary: exclusive -> expired (kept, not dropped)
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET, "version": "2.1.0"})
    row = status_of(j.get("constraints", []), vid)
    check("until boundary (version == until) is expired, still returned", row and row.get("status") == "expired", str(row))

    # well past the window -> still expired, still returned
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET, "version": "9.0.0"})
    row = status_of(j.get("constraints", []), vid)
    check("well past until is still expired (not dropped)", row and row.get("status") == "expired", str(row))

    # unbounded constraint always active regardless of version
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": PLAIN_SNIPPET, "version": "9.0.0"})
    row = status_of(j.get("constraints", []), pid)
    check("unbounded (no since/until) constraint stays active at any version", row and row.get("status") == "active", str(row))

    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET, "version": "not-a-version"})
    check("check with malformed version -> 400", s == 400, str(j))

    print("=== issue #106: corpus_deviation ===")
    check("read-only token: deviation -> 403",
          call("POST", "/corpus/deviation", ro_tok,
               {"namespace": NS, "constraint_id": vid, "rationale": "x", "approved_by": "architect"})[0] == 403)
    s, j = call("POST", "/corpus/deviation", user_tok,
                {"namespace": NS, "constraint_id": 999999999, "rationale": "x", "approved_by": "architect"})
    check("deviation for unknown constraint_id -> 404", s == 404, str(j))
    s, j = call("POST", "/corpus/deviation", user_tok,
                {"namespace": NS, "constraint_id": vid, "approved_by": "architect"})
    check("deviation missing rationale -> 400", s == 400, str(j))
    s, j = call("POST", "/corpus/deviation", user_tok,
                {"namespace": NS, "constraint_id": vid, "rationale": "x", "approved_by": "architect", "until": "nope"})
    check("deviation with malformed until -> 400", s == 400, str(j))

    s, j = call("POST", "/corpus/deviation", user_tok,
                {"namespace": NS, "constraint_id": vid, "rationale": "tenantId fallback accepted until 2.1.0",
                 "approved_by": "architect", "until": "2.1.0"})
    check("deviation created -> 200", s == 200, str(j))
    check("deviation echoes constraint_id/rationale/approved_by/until",
          j.get("constraint_id") == vid and j.get("rationale") == "tenantId fallback accepted until 2.1.0"
          and j.get("approved_by") == "architect" and j.get("until") == "2.1.0")

    # deviation active within its own window (well before the constraint's own until too)
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET, "version": "1.5.0"})
    row = status_of(j.get("constraints", []), vid)
    check("active deviation overrides 'active' -> approved_deviation", row and row.get("status") == "approved_deviation", str(row))

    # deviation still active exactly at the constraint's own (now-superseded) until boundary
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET, "version": "2.0.9"})
    row = status_of(j.get("constraints", []), vid)
    check("deviation active just before its own until", row and row.get("status") == "approved_deviation", str(row))

    # once the DEVIATION's own until has passed, fall back to the constraint's window state
    # (expired here, since the constraint's own until is also 2.1.0)
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET, "version": "2.1.0"})
    row = status_of(j.get("constraints", []), vid)
    check("deviation's own expiry falls back to underlying window state (expired)",
          row and row.get("status") == "expired", str(row))
    # no version given: deviation exists -> approved_deviation regardless of its own until
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET})
    row = status_of(j.get("constraints", []), vid)
    check("no version given: standing deviation still reported as approved_deviation",
          row and row.get("status") == "approved_deviation", str(row))

    print("=== issue #106: cross-namespace + re-ingest orphaning ===")
    s, j = call("POST", "/corpus/deviation", user_tok,
                {"namespace": NS2, "constraint_id": vid, "rationale": "x", "approved_by": "architect"})
    check("deviation constraint_id from a DIFFERENT namespace -> 404 (not leaked cross-namespace)",
          s == 404, str(j))

    # re-ingest deletes+recreates the versioned-doc's constraint rows (new ids). The
    # deviation row above still references the OLD id -- must not error, and must simply
    # stop matching (orphaned, not an FK violation: corpus_deviations has no FK by design).
    s, j = call("POST", "/corpus/ingest", user_tok,
                {"namespace": NS, "name": "versioned-doc", "text": VDOC, "since": "1.0.0", "until": "2.1.0"})
    check("re-ingest after a live deviation on the old id does not error", s == 200, str(j))
    new_vid = j["ids"][0]
    check("re-ingest issued a new id (old one now orphaned in corpus_deviations)", new_vid != vid)
    s, j = call("POST", "/corpus/check", user_tok, {"namespace": NS, "snippet": SNIPPET, "version": "1.5.0"})
    row = status_of(j.get("constraints", []), new_vid)
    check("re-ingested constraint has no deviation of its own -> plain active",
          row and row.get("status") == "active", str(row))

    cleanup(conn)
    for pid_ in (admin_id, user_id, ro_id):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid_,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid_,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid_,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
