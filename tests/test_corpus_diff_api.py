"""No-AI tests for issue #105: corpus_check_diff — a diff-mode verdict against the
architecture corpus (violated / satisfied / uncovered constraints + a compliance
score), as opposed to corpus_check's flat ranked-list-for-a-snippet shape. Pure regex +
SQL FTS, no LLM/embeddings — same discipline as test_corpus_api.py.

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos python test_corpus_diff_api.py
"""
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:corpusdiffapi"
NS2 = "test:corpusdiffapi:otherdoc"
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

# 4 constraints, one per bullet (each bullet is a single sentence — no internal '.'/'!'/'?'
# so ingest_constraints keeps it as one candidate). The 4th deliberately combines a
# requirement clause and a prohibition clause in one sentence via ';' — this is the
# documented mixed-polarity limitation pinned by test_mixed_polarity_known_limitation below.
DOC = """
# Access LLD
- Passwords SHALL NOT be logged in application output.
- Emails SHALL be validated before being sent to recipients.
- Audit trails SHOULD capture the acting user for every request.
- All database writes MUST use the transaction wrapper; direct commits are PROHIBITED.
"""

OTHER_DOC = "- Cache entries SHALL NOT exceed a five minute time to live."


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


def by_content(entries, needle):
    return [e for e in entries if needle in e["content"]]


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup(conn)

    admin_id = Control.create_principal(conn, "test-cdiff-admin", "service")
    user_id = Control.create_principal(conn, "test-cdiff-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "test")
    Control.grant(conn, user_id, NS, can_read=True, can_write=True)
    Control.grant(conn, user_id, NS2, can_read=True, can_write=True)
    ro_id = Control.create_principal(conn, "test-cdiff-ro", "agent")
    ro_tok = Control.mint_token(conn, ro_id, "test")
    Control.grant(conn, ro_id, NS, can_read=True, can_write=False)

    print("=== corpus_check_diff (issue #105) ===")
    check("no token -> 401", call("POST", "/corpus/check_diff", None, {"namespace": NS, "diff": "x"})[0] == 401)

    s, j = call("POST", "/corpus/ingest", user_tok, {"namespace": NS, "name": "access-lld", "text": DOC, "kind": "lld"})
    check("ingest 200", s == 200)
    check("extracted 4 constraints", j.get("constraints") == 4)

    s, j = call("POST", "/corpus/ingest", user_tok, {"namespace": NS2, "name": "cache-lld", "text": OTHER_DOC})
    check("second-namespace ingest 200", s == 200)

    check("read-only token can still check_diff",
          call("POST", "/corpus/check_diff", ro_tok, {"namespace": NS, "diff": "+ passwords logged application output"})[0] == 200)

    check("missing diff -> 400", call("POST", "/corpus/check_diff", user_tok, {"namespace": NS})[0] == 400)

    # whitespace-only diff: same "empty" treatment as corpus_check's blank-snippet -> 400
    check("blank (whitespace-only) diff -> 400 like a missing one",
          call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": "  \n  \n"})[0] == 400)

    # a real diff whose vocabulary matches none of the ingested constraints: nothing to
    # evaluate, so `score` is the VACUOUS 1.0 -- `evaluated` (0) is what tells a caller
    # apart from a diff that actually satisfied everything it touched
    unrelated_diff = ("--- a/render.py\n+++ b/render.py\n@@ -1,1 +1,2 @@\n"
                       "+    renderer.compute_layout(widget, graphics_buffer)  # shader texture update\n"
                       " pass\n")
    s, vac = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": unrelated_diff})
    check("unrelated diff -> 200", s == 200)
    check("unrelated diff: nothing evaluated", vac.get("evaluated") == 0)
    check("unrelated diff: vacuous score is 1.0", vac.get("score") == 1.0)
    check("unrelated diff: no violated/satisfied", not vac.get("violated") and not vac.get("satisfied"))

    # --- one diff touching 3 of the 4 constraints: VIOLATED, SATISFIED, UNCOVERED ---
    diff = (
        "--- a/auth.py\n+++ b/auth.py\n@@ -1,3 +1,4 @@\n"
        " def login(passwords):\n"
        '+    securitylog.write("passwords logged to application output: " + passwords)\n'
        "     return True\n"
        "--- a/mailer.py\n+++ b/mailer.py\n@@ -1,2 +1,3 @@\n"
        " def send(payload):\n"
        "+    emails = validated_and_sent(payload, recipients=recipients)\n"
        "     return dispatch(payload)\n"
        "--- a/routes.py\n+++ b/routes.py\n@@ -1,1 +1,2 @@\n"
        "+    def handle_request(self):\n"
        " pass\n"
    )
    s, j = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": diff})
    check("check_diff 200", s == 200)

    violated = j.get("violated", [])
    satisfied = j.get("satisfied", [])
    uncovered = j.get("uncovered", [])

    check("passwords-not-logged constraint is VIOLATED",
          bool(by_content(violated, "Passwords SHALL NOT be logged")), json.dumps(j)[:400])
    check("violated entry carries matched_terms evidence",
          bool(by_content(violated, "Passwords SHALL NOT be logged")[0].get("matched_terms")) if by_content(violated, "Passwords SHALL NOT be logged") else False)
    check("email-validation constraint is SATISFIED",
          bool(by_content(satisfied, "Emails SHALL be validated")), json.dumps(j)[:400])
    check("audit-trail constraint is UNCOVERED (single incidental word 'request')",
          bool(by_content(uncovered, "Audit trails SHOULD")), json.dumps(j)[:400])
    check("audit-trail constraint NOT in violated or satisfied",
          not by_content(violated, "Audit trails SHOULD") and not by_content(satisfied, "Audit trails SHOULD"))
    check("evaluated == violated + satisfied", j.get("evaluated") == len(violated) + len(satisfied))
    check("score == satisfied / evaluated",
          j.get("score") == round(len(satisfied) / j.get("evaluated"), 4) if j.get("evaluated") else j.get("score") == 1.0)

    # --- KNOWN LIMITATION, pinned as a contract (see core/store.py corpus_check_diff docstring):
    # the 4th constraint mixes a requirement clause ("MUST use the transaction wrapper")
    # and a prohibition clause ("direct commits are PROHIBITED") in ONE ingested sentence.
    # A diff that correctly implements the requirement half still gets classified VIOLATED,
    # because polarity is decided by "does a negative keyword appear anywhere in the
    # statement" — not per-clause. This is documented behavior, not a silent gap.
    mix_diff = ("--- a/db.py\n+++ b/db.py\n@@ -1,1 +1,2 @@\n"
                "+    # database writes go through the transaction wrapper, no direct commits\n"
                " pass\n")
    s, j = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": mix_diff})
    check("mixed-polarity check_diff 200", s == 200)
    mix_hit_violated = by_content(j.get("violated", []), "transaction wrapper")
    mix_hit_satisfied = by_content(j.get("satisfied", []), "transaction wrapper")
    check("KNOWN LIMITATION pinned: compliant diff against a mixed-polarity constraint "
          "still lands in violated (not satisfied) because polarity is whole-sentence",
          bool(mix_hit_violated) and not mix_hit_satisfied, json.dumps(j)[:400])

    # --- name filter: restrict to a specific corpus source ---
    s, j = call("POST", "/corpus/ingest", user_tok,
                {"namespace": NS, "name": "cache-lld-2", "text": "- Cache writes SHALL NOT skip the invalidation hook."})
    check("second source ingest 200", s == 200)
    cache_diff = "+    cache.write(skip_invalidation_hook=True)  # invalidation hook skipped"
    s, j = call("POST", "/corpus/check_diff", user_tok,
                {"namespace": NS, "diff": cache_diff, "name": "access-lld"})
    check("name filter: access-lld source excludes the cache-lld-2 constraint", s == 200 and
          not by_content(j.get("violated", []) + j.get("satisfied", []) + j.get("uncovered", []), "invalidation"))
    s, j = call("POST", "/corpus/check_diff", user_tok,
                {"namespace": NS, "diff": cache_diff, "name": "cache-lld-2"})
    check("name filter: cache-lld-2 source surfaces its own constraint as violated",
          s == 200 and bool(by_content(j.get("violated", []), "invalidation")), json.dumps(j)[:400])

    # --- namespace isolation: NS2's cache constraint must not leak into NS's check ---
    s, j = call("POST", "/corpus/check_diff", user_tok,
                {"namespace": NS, "diff": "+ five minute time to live cache entries exceed"})
    check("namespace isolation: NS2's constraint doesn't leak into NS",
          s == 200 and not by_content(j.get("violated", []) + j.get("satisfied", []) + j.get("uncovered", []),
                                      "five minute"))

    # --- multi-file diff: a constraint relevant only to the LAST file must still surface
    # (regression guard for a low first-seen word cap silently dropping late-file vocab).
    # Filler tokens must be PURE-ALPHABETIC (the [A-Za-z]{4,} word extractor drops digits,
    # so a digit-suffixed token like "wordFI01" collapses to the same word "word" every
    # time) -- so index -> letters (a, b, ... z, aa, ab, ...) instead of a numeric suffix.
    def _letters(n):
        s = ""
        n += 1
        while n:
            n, r = divmod(n - 1, 26)
            s = chr(97 + r) + s
        return s

    filler_files = []
    for fi in range(6):
        lines = [f"--- a/filler{fi}.py\n+++ b/filler{fi}.py\n@@ -1,1 +1,{2 + 8}@@\n pass\n"]
        for wi in range(8):
            token = "filler" + _letters(fi * 8 + wi)
            lines.append(f"+    # {token} placeholder note\n")
        filler_files.append("".join(lines))
    real_hunk = ("--- a/final_auth.py\n+++ b/final_auth.py\n@@ -1,2 +1,3 @@\n"
                 " def login(passwords):\n"
                 '+    securitylog.write("passwords logged to application output: " + passwords)\n'
                 "     return True\n")
    big_diff = "".join(filler_files) + real_hunk
    distinct_words = len(set(w.lower() for w in re.findall(r"[A-Za-z]{4,}", big_diff)))
    check("sanity: multi-file filler pushes distinct word count well past the old 40-word cap",
          distinct_words > 60, f"got {distinct_words}")
    t0 = time.perf_counter()
    s, j = call("POST", "/corpus/check_diff", user_tok, {"namespace": NS, "diff": big_diff})
    elapsed = time.perf_counter() - t0
    check("multi-file diff -> 200", s == 200)
    check("constraint relevant only to the LAST file's hunk still surfaces as violated",
          bool(by_content(j.get("violated", []), "Passwords SHALL NOT be logged")),
          f"distinct_words={distinct_words}")
    check("multi-file diff answered in under 5s (no query-time LLM)", elapsed < 5.0, f"{elapsed:.2f}s")

    cleanup(conn)
    for pid in (admin_id, user_id, ro_id):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
