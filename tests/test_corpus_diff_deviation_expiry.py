"""Regression test for a BLOCKING bug found by adversarial review: corpus_check_diff()
(core/store.py, issue #105/#112) had NO version parameter and NO corpus_deviation lookup
at all -- unlike its sibling corpus_check() (issue #106), which already does both
correctly. Concretely, before this fix:

  1. Deviation gap: a constraint with a genuine, approved, audited corpus_deviation on
     file still got reported as `violated` by corpus_check_diff -- and #112's
     tommy_verdict gates `merge_blocked` on exactly that list, so the whole
     /corpus/deviation audit trail was functionally inert on the one path that actually
     blocks a merge.
  2. Expiry gap: a constraint formally retired via corpus_ingest(..., until="2.0") and
     genuinely inactive at the diff's target version was STILL reported as `violated`,
     because corpus_check_diff had no `version` parameter to filter against
     constraint_since/constraint_until at all.

This file proves both are fixed, matching corpus_check's existing version/deviation
semantics (see tests/test_corpus_expiry_version_106.py) as closely as the diff-verdict
shape allows: a would-be-`violated` constraint whose status is not "active" is routed to
a new `deviated` bucket (not `violated`, and not silently dropped either -- same
"distinct status/category, not just excluded" behavior corpus_check uses), while a
genuinely active, non-deviated, non-expired constraint is still correctly `violated`.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_corpus_diff_deviation_expiry.py
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
NS = "test:corpusdiffdevexp"
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

# Requirement-style constraint (no SHALL NOT/MUST NOT/PROHIBITED -> not a prohibition).
# c_words (after RFC-2119/stopword stripping) include: legacy, caches, invalidated,
# write, path, touches, shared, state.
DEV_DOC = "Legacy caches MUST be invalidated on write when the write path touches shared state."
# A diff whose REMOVED side carries the constraint's own vocabulary and whose ADDED side
# carries none of it -- for a requirement, removed-side-dominant is a violation (the
# required behavior's text was deleted, nothing equivalent was added back).
VIOLATING_DIFF = (
    "--- a/cache.py\n+++ b/cache.py\n@@ -1,2 +1,1 @@\n"
    "-    # legacy caches invalidated on write; write path touches shared state\n"
    "-    do_invalidate()\n"
    "+    pass\n"
)

# A second, independent constraint used as the "still works normally" control -- no
# since/until, no deviation, must still land in `violated` for the same kind of diff.
PLAIN_DOC = "Session tokens MUST be rotated on privilege escalation after elevation."
PLAIN_VIOLATING_DIFF = (
    "--- a/auth.py\n+++ b/auth.py\n@@ -1,2 +1,1 @@\n"
    "-    # session tokens rotated on privilege escalation after elevation\n"
    "-    do_rotate()\n"
    "+    pass\n"
)


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
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s AND kind='constraint'", (NS,))
        c.execute("DELETE FROM memnos_control.corpus_sources WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.corpus_deviations WHERE namespace=%s", (NS,))


def by_content(entries, needle):
    return [e for e in entries if needle in e["content"]]


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup(conn)

    user_id = Control.create_principal(conn, "test-cdiffdevexp-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)

    print("=== corpus_check_diff: deviation + expiry gap (adversarial review fix) ===")

    s, j = call("POST", "/corpus/ingest", user_tok,
                {"namespace": NS, "name": "cache-doc", "text": DEV_DOC, "until": "2.0"})
    check("ingest with until -> 200", s == 200, str(j))
    check("extracted 1 constraint", j.get("constraints") == 1, str(j))
    dev_id = j["ids"][0]

    s, j = call("POST", "/corpus/ingest", user_tok, {"namespace": NS, "name": "auth-doc", "text": PLAIN_DOC})
    check("ingest plain (unbounded) constraint -> 200", s == 200, str(j))
    plain_id = j["ids"][0]

    # --- sanity: BEFORE any deviation/expiry, the versioned constraint's own violating
    # diff is genuinely violated (proves VIOLATING_DIFF actually triggers the classifier,
    # so the later "not violated" assertions are meaningful, not vacuous) ---
    s, j = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": VIOLATING_DIFF})
    check("sanity: check_diff 200", s == 200, str(j))
    check("sanity: versioned constraint IS violated before any deviation/expiry applies",
          bool(by_content(j.get("violated", []), "Legacy caches MUST")), json.dumps(j)[:400])

    # --- the OTHER, independent constraint (no since/until, no deviation) must ALWAYS
    # be reported violated by its own violating diff -- run throughout as a live control,
    # so a bug that makes corpus_check_diff report nothing as violated (rather than
    # correctly excluding only deviated/expired ones) cannot pass this file. ---
    s, j = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": PLAIN_VIOLATING_DIFF})
    check("control: independent non-deviated, non-expired constraint is violated",
          bool(by_content(j.get("violated", []), "Session tokens MUST")), json.dumps(j)[:400])
    check("control: independent constraint carries status 'active'",
          by_content(j.get("violated", []), "Session tokens MUST")[0].get("status") == "active"
          if by_content(j.get("violated", []), "Session tokens MUST") else False)

    print("=== bug 1: approved deviation must exclude a real violation from `violated` ===")
    s, j = call("POST", "/corpus/deviation", user_tok,
                {"namespace": NS, "constraint_id": dev_id, "rationale": "phase-2 cache rewrite in flight",
                 "approved_by": "architect"})
    check("deviation recorded -> 200", s == 200, str(j))

    # version OMITTED -- matches corpus_check's version-omitted behaviour: a standing
    # deviation always wins, regardless of the request never mentioning a version at all.
    s, j = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": VIOLATING_DIFF})
    check("check_diff (deviation, no version) -> 200", s == 200, str(j))
    check("deviated constraint NOT in violated",
          not by_content(j.get("violated", []), "Legacy caches MUST"), json.dumps(j)[:400])
    dev_hits = by_content(j.get("deviated", []), "Legacy caches MUST")
    check("deviated constraint surfaced in `deviated` bucket, not silently dropped",
          bool(dev_hits), json.dumps(j)[:400])
    check("deviated entry carries status 'approved_deviation'",
          dev_hits and dev_hits[0].get("status") == "approved_deviation", str(dev_hits))
    check("deviated entry still carries real evidence (matched_terms non-empty)",
          dev_hits and bool(dev_hits[0].get("matched_terms")), str(dev_hits))
    check("evaluated excludes the deviated entry (same treatment as uncovered)",
          j.get("evaluated") == len(j.get("violated", [])) + len(j.get("satisfied", [])), str(j))

    s, j2 = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": PLAIN_VIOLATING_DIFF})
    check("control still violated with an unrelated deviation on file elsewhere",
          bool(by_content(j2.get("violated", []), "Session tokens MUST")), json.dumps(j2)[:400])

    print("=== bug 2: version-retired constraint must exclude a real violation from `violated` ===")
    # version PAST the retirement point ("2.0"), no deviation involved for this check --
    # temporarily neutralize the standing deviation's effect by asking for the version
    # verdict directly; the constraint's own until="2.0" is what must drive this.
    s, j = call("POST", "/corpus/check_diff", user_tok,
                {"namespace": NS, "diff": VIOLATING_DIFF, "version": "2.5"})
    check("check_diff (version past until) -> 200", s == 200, str(j))
    check("retired constraint NOT in violated at version 2.5",
          not by_content(j.get("violated", []), "Legacy caches MUST"), json.dumps(j)[:400])
    # the deviation recorded above has no `until` of its own, so it's still active too --
    # status should be approved_deviation (deviation wins over plain expiry, same
    # precedence corpus_check documents), and it must land in `deviated` either way.
    dev_hits2 = by_content(j.get("deviated", []), "Legacy caches MUST")
    check("still surfaced in `deviated` bucket at the retired version",
          bool(dev_hits2), json.dumps(j)[:400])
    check("status at retired version is approved_deviation (deviation still on file, no until)",
          dev_hits2 and dev_hits2[0].get("status") == "approved_deviation", str(dev_hits2))

    print("=== bug 2b: expiry alone (no deviation) must also exclude from `violated` ===")
    s, j = call("POST", "/corpus/ingest", user_tok,
                {"namespace": NS, "name": "expiry-only-doc",
                 "text": "Config writes MUST be validated against the schema before persisting.",
                 "until": "2.0"})
    check("ingest second versioned (no-deviation) constraint -> 200", s == 200, str(j))
    exp_only_diff = (
        "--- a/config.py\n+++ b/config.py\n@@ -1,2 +1,1 @@\n"
        "-    # config writes validated against schema before persisting\n"
        "-    do_validate()\n"
        "+    pass\n"
    )
    s, j = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": exp_only_diff})
    check("sanity: no-deviation versioned constraint IS violated when version is omitted",
          bool(by_content(j.get("violated", []), "Config writes MUST")), json.dumps(j)[:400])
    s, j = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": exp_only_diff, "version": "2.5"})
    check("check_diff (expired, no deviation) -> 200", s == 200, str(j))
    check("expired-only constraint NOT in violated past its own until",
          not by_content(j.get("violated", []), "Config writes MUST"), json.dumps(j)[:400])
    exp_hits = by_content(j.get("deviated", []), "Config writes MUST")
    check("expired-only constraint surfaced in `deviated` bucket with status 'expired'",
          exp_hits and exp_hits[0].get("status") == "expired", str(exp_hits))

    print("=== not-yet-introduced: dropped entirely, same as corpus_check ===")
    s, j = call("POST", "/corpus/ingest", user_tok,
                {"namespace": NS, "name": "future-doc",
                 "text": "Webhook payloads MUST be signed with an HMAC before delivery.",
                 "since": "5.0"})
    check("ingest future-scoped constraint -> 200", s == 200, str(j))
    future_diff = (
        "--- a/webhook.py\n+++ b/webhook.py\n@@ -1,2 +1,1 @@\n"
        "-    # webhook payloads signed with hmac before delivery\n"
        "-    do_sign()\n"
        "+    pass\n"
    )
    s, j = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": future_diff, "version": "1.0"})
    all_entries = (j.get("violated", []) + j.get("satisfied", []) + j.get("uncovered", []) + j.get("deviated", []))
    check("not-yet-introduced constraint dropped from every bucket at version 1.0",
          not by_content(all_entries, "Webhook payloads MUST"), json.dumps(j)[:400])

    print("=== malformed version -> 400 (validated before the diff-vocabulary early return) ===")
    s, j = call("POST", "/corpus/check_diff", user_tok,
                {"namespace": NS, "diff": VIOLATING_DIFF, "version": "not-a-version"})
    check("malformed version -> 400", s == 400, str(j))

    cleanup(conn)
    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (user_id,))
        c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (user_id,))
        c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (user_id,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
