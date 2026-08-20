"""Live, end-to-end proof of Tommy's corpus gate (issue #109) against a real
memnos server + real Postgres — the one test in this feature that is NOT a
mocked-HTTP unit test (those live in agents/tommy/tests/, which the CI
tommy-tests job runs DB-free by design; see that suite's test_corpus.py,
test_corpus_gate.py, test_auto_ingest.py for the plumbing/branching
coverage).

This file exercises tommy.corpus (agents/tommy/tommy/corpus.py) — the exact
module tommy_dispatch's corpus gate and cli.py's auto-ingest call — end to
end through a real HTTP round trip:

  1. auto_ingest's real job: corpus_ingest() a REAL design-doc fixture
     (agents/tommy/tests/fixtures/sample_adr.md — genuine SHALL/MUST/SHOULD
     language, the same fixture agents/tommy/tests/test_auto_ingest.py uses
     against a mock) and assert the REAL server-side extractor
     (core/store.py's ingest_constraints / _CONSTRAINT_RE) finds exactly the
     4 constraints in it.
  2. corpus_gate's "match" outcome: corpus_check() with a snippet that
     shares keywords with one of those constraints -> the real constraint
     text comes back.
  3. corpus_gate's "no relevant constraints" outcome: corpus_check() with an
     unrelated snippet -> ok=True, constraints=[] — a clean pass, not an
     error.
  4. corpus_gate's "check itself failed" outcome: corpus_check() against an
     unreachable memnos_url -> ok=False with an error, DISTINCT from
     outcome 3 (this is the fail-open-but-visible distinction issue #109's
     amendment comment requires).
  5. auto_ingest's write-scope question: a read-only token 403s on
     corpus_ingest() — surfaced as ok=False, not a raised exception.

Run against a live local server (same harness as the rest of tests/):
    MEMNOS_DSN=... MEMNOS_URL=... python tests/test_corpus_gate_tommy.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agents", "tommy"))

import psycopg
from psycopg.rows import dict_row
from core.control import Control
from tommy.corpus import corpus_check, corpus_ingest

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
NS = "test:tommy-corpus-gate"
SCHEMA = "tenant_memnos"
RUN = str(int(time.time() * 1000))  # uniquify the source name across reruns
FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "agents", "tommy", "tests", "fixtures", "sample_adr.md")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def cleanup(conn):
    with conn.cursor() as c:
        c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s AND kind='constraint'", (NS,))
        c.execute("DELETE FROM memnos_control.corpus_sources WHERE namespace=%s", (NS,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    cleanup(conn)

    write_id = Control.create_principal(conn, f"test-tommy-corpus-write-{RUN}", "service")
    write_tok = Control.mint_token(conn, write_id, "test")
    Control.grant(conn, write_id, NS, can_read=True, can_write=True)

    ro_id = Control.create_principal(conn, f"test-tommy-corpus-ro-{RUN}", "agent")
    ro_tok = Control.mint_token(conn, ro_id, "test")
    Control.grant(conn, ro_id, NS, can_read=True, can_write=False)

    print("=== Tommy corpus gate — live end-to-end (issue #109) ===")

    # --- auto_ingest: real fixture, real server-side extraction ------------
    with open(FIXTURE_PATH) as f:
        adr_text = f.read()
    source_name = f"sample-adr-{RUN}"

    result = corpus_ingest(URL, write_tok, NS, source_name, adr_text, kind="doc")
    check("corpus_ingest ok", result.get("ok") is True, result)
    check("real extractor finds exactly the fixture's 4 SHALL/MUST/SHOULD sentences",
          result.get("constraints") == 4, result)

    # --- corpus_gate outcome 1: a real match --------------------------------
    match = corpus_check(URL, write_tok, NS, "we need to bypass the repository layer for widget loader queries")
    check("corpus_check (match) ok", match.get("ok") is True, match)
    match_contents = [c["content"] for c in match.get("constraints", [])]
    check("surfaces the real ingested constraint text",
          any("repository layer" in c and "PROHIBITED" in c for c in match_contents), match_contents)

    # --- corpus_gate outcome 2: a real, legitimate "nothing relevant" ------
    no_match = corpus_check(URL, write_tok, NS, "unrelated snippet about quarterly holiday scheduling logistics")
    check("corpus_check (no match) ok=True — a clean pass, not an error", no_match.get("ok") is True, no_match)
    check("no match -> empty constraints", no_match.get("constraints") == [], no_match)

    # --- corpus_gate outcome 3: the check itself cannot run -----------------
    # Distinct from outcome 2 above: ok=False, not an empty-but-ok result —
    # this is the fail-open-but-visible distinction issue #109's amendment
    # comment requires ("no relevant constraints matched" vs "the corpus
    # check itself couldn't run" must never be indistinguishable).
    unreachable = corpus_check("http://127.0.0.1:1", write_tok, NS, "anything", timeout=2.0)
    check("corpus_check against an unreachable server -> ok=False", unreachable.get("ok") is False, unreachable)
    check("unreachable result carries an error message and stays empty (not raised)",
          bool(unreachable.get("error")) and unreachable.get("constraints") == [], unreachable)
    check("outcome 2 (no match) and outcome 3 (unreachable) are NOT the same dict",
          no_match != unreachable)

    # --- auto_ingest write-scope: a read-only token 403s, surfaced not raised
    ro_result = corpus_ingest(URL, ro_tok, NS, f"{source_name}-ro-attempt", adr_text, kind="doc")
    check("read-only token -> corpus_ingest ok=False (403), not a raised exception",
          ro_result.get("ok") is False, ro_result)
    check("403 detail is surfaced in the error message",
          "403" in (ro_result.get("error") or "") or "forbidden" in (ro_result.get("error") or "").lower(),
          ro_result)

    cleanup(conn)
    for pid in (write_id, ro_id):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
