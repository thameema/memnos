"""fts_clamp shape-invariance gate (issue #41 fix B).

Before this fix, fts_clamp bounded ONLY whitespace-token count (default 200), on the
theory that a shorter query builds a smaller tsquery. That's false in general: node count
is what determines parser cost, not word count.

Two mechanisms have been tried and both left real gaps, found by successive adversarial
reviews of PR #43:

  1. Unconditionally stripping quotes/"OR"/leading "-" from ANY query containing them,
     regardless of length -- silently mangled ordinary short queries (`'python -django'`
     -> `'python django'`, INVERTING an exclusion into a match).
  2. A pure-Python node-count ESTIMATE (measured: a leading "-" adds exactly +1 node per
     negated word, linear and additive) used to justify "capping word count alone bounds
     node count for any shape, so no rewriting is needed." That held for the constructs it
     was measured against (leading "-", quotes, "OR") but is FALSE in general: a word
     containing INTERNAL hyphens (kebab-case identifiers -- file paths, stack traces,
     branch names, CSS selectors) gets DECOMPOSED by Postgres's text-search parser into
     many extra sub-lexemes, which the estimate never counted. Measured live: a single
     40-word query built from ordinary hyphenated identifiers estimated at 79 nodes
     (comfortably under the 120 default bound) but had a REAL numnode() in the thousands,
     and at high enough hyphen density this reproduces an actual, unhandled
     `psycopg.errors.ProgramLimitExceeded: value is too big in tsquery` straight through
     the "safe, pass through unchanged" path -- the exact crash class issue #41 exists to
     eliminate.

fts_clamp no longer estimates complexity at all. It asks Postgres directly
(_tsquery_within_bound in core/store.py): build the real tsquery, check its real
numnode(), under a short dedicated statement_timeout so the check itself can't become the
failure point even if computing numnode() would itself overflow the parser. This is
authoritative for hyphen-decomposition and for any other tsquery-expanding construct that
hasn't been found yet -- it isn't a model of the parser, it's a measurement.

  3. A third review found the MEASUREMENT's own safety net had a hole of the identical
     shape as gap 2 above, one level down: _tsquery_within_bound's except clause caught
     only the two specific error types (ProgramLimitExceeded, QueryCanceled) observed
     from the constructs tested so far -- and a bare run of >=33 consecutive literal
     hyphens (a markdown divider, a YAML frontmatter delimiter -- ordinary text, not
     adversarial fuzzing, and a categorically different shape from the kebab-identifier
     "p0-p1-p2-..." fuzz above) raises `psycopg.errors.InternalError_: tsquery stack too
     small`, which shares no ancestor with either caught type and propagated straight
     through -- a live reproduction of issue #41's ORIGINAL crash through the exact code
     path built to eliminate it. Enumerating that third type would repeat the same
     mistake a third time, so the except clause now catches `psycopg.DatabaseError`
     (broad by class -- every DatabaseError-class failure this probe can raise, both
     server-reported SQLSTATE errors and psycopg's own pre-flight input rejections (e.g.
     a NUL byte, rejected before the statement ever reaches the server -- no SQLSTATE)
     alike -- not by enumeration), deliberately excluding `psycopg.InterfaceError`
     (driver/connection misuse, not a property of the query text, which should still
     propagate). This also closes that NUL-byte crash (`psycopg.DataError`) through the
     same path, for free.

What this file proves, against a REAL Postgres:

  1. NORMAL QUERY UNCHANGED: a plain query under the cap, with none of the shape-driving
     constructs, is returned byte-for-byte identical.
  1b. SHORT QUERIES WITH OPERATORS UNCHANGED: the review's exact `-`/quote/OR repro
     queries, and a few more, stay byte-for-byte identical -- they now round-trip through
     the real probe, so this is also the regression guard that the probe doesn't
     false-positive on ordinary text.
  2. HYPHEN-DENSITY FUZZ (the gap the second review found): a range of internal-hyphen
     densities, from mild (real kebab-case identifiers) to the exact density that
     previously reproduced ProgramLimitExceeded, differentially checked against live
     numnode() -- for every density, fts_clamp's output either passes through unchanged
     (when genuinely safe) or is shrunk to something whose REAL numnode() is confirmed
     under the bound. No case is judged "safe" on an estimate that turns out wrong.
  3. THE TWO EXACT REPROS from the second review: the 40-word kebab-identifier query
     (estimated 79 nodes, real numnode() far over bound) is shrunk, not passed through
     unchecked; the 5000-hyphens/token density that raised a real ProgramLimitExceeded is
     handled by fts_clamp WITHOUT raising.
  4. SHAPE INVARIANCE (non-hyphen constructs): for the original adversarial corpus (a
     single long quoted phrase, many degenerate one-word "phrases", a long OR-chain, a
     leading-"-" chain, an irregular mix) fts_clamp's output builds a tsquery whose real
     numnode() stays under the same fixed safety bound the clamp is gated on.
  5. FIX REDUCES COMPLEXITY: for the same adversarial input, the OLD clamp (200-token,
     shape-blind) built a LARGER tsquery than the NEW clamp.
  6. NEVER RAISES: fts_clamp never lets a Postgres exception (QueryCanceled,
     ProgramLimitExceeded) propagate, on any input tried here, including degenerate ones
     (empty string, pure operators, unicode) and the reproduced-crash density.
  7. TRUNCATION IS SAFE MID-PHRASE: truncating an over-cap query can leave a quote
     unbalanced -- confirmed that websearch_to_tsquery treats an unterminated quote
     leniently (same result as if it had been closed), not as an error or degenerate
     match.
  8. THE THIRD REVIEW'S EXACT REPROS (gap 3 above): a markdown-divider sentence and a
     bare hyphen run (well past the ~33-hyphen cliff) both confirmed to raise
     InternalError_ directly (sanity check the trap is real) yet handled by fts_clamp
     WITHOUT raising; a NUL-byte query confirmed to raise DataError directly, also
     handled without raising -- proving the
     broad psycopg.DatabaseError catch, not a third enumerated exception type.

HONESTY NOTE (see the PR description for the full writeup): extensive earlier adversarial
testing (plain chains, phrases, OR-chains, leading-"-" chains, up to tens of thousands of
tokens) never reproduced the "tsquery stack too small" crash issue #41 originally reports
for websearch_to_tsquery specifically -- but the hyphen-decomposition mechanism this file
now covers DOES reproduce a real, distinct Postgres error (ProgramLimitExceeded) through
the same guarded code path, via a shape ordinary adversarial testing hadn't tried. That's
exactly why the guard is now a real measurement instead of a model.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import psycopg
from psycopg.rows import dict_row

from core.store import fts_clamp, _fts_max_tokens, _fts_node_bound

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def _old_clamp(qtext, cap=200):
    """The pre-#41 behavior: whitespace-token count only, no shape normalization."""
    parts = qtext.split()
    if len(parts) <= cap:
        return qtext
    return " ".join(parts[:cap])


def _mixed_shape(n):
    """Irregular mix: quoted 3-word phrases, bare 'OR', negated words, plain words —
    exactly the kind of messy real-world input a clean synthetic single-construct
    query might not represent."""
    toks = []
    i = 0
    while len(toks) < n:
        r = i % 4
        if r == 0:
            toks.append(f'"a{i} b{i} c{i}"')
        elif r == 1:
            toks.append("OR")
        elif r == 2:
            toks.append(f"-w{i}")
        else:
            toks.append(f"w{i}")
        i += 1
    return " ".join(toks)


ADVERSARIAL_SHAPES = {
    "single-long-phrase": lambda n: '"' + " ".join(f"w{i}" for i in range(n)) + '"',
    "many-single-word-phrases": lambda n: " ".join(f'"w{i}"' for i in range(n)),
    "or-chain": lambda n: " OR ".join(f"w{i}" for i in range(n)),
    "leading-dash-chain": lambda n: " ".join(f"-w{i}" for i in range(n)),
    "irregular-mix": _mixed_shape,
}


def _kebab_word(n_hyphens):
    """A single whitespace-token with n_hyphens internal hyphens, e.g. n=3 -> 'p0-p1-p2-p3'
    -- realistic stand-in for a file path segment, a long identifier, a git branch name."""
    return "-".join(f"p{i}" for i in range(n_hyphens + 1))


def main():
    print("=== fts_clamp shape-invariance (issue #41 fix B) ===")
    cap = _fts_max_tokens()
    check(f"effective FTS cap is a positive int (got {cap})", isinstance(cap, int) and cap >= 1)
    node_bound = _fts_node_bound(cap)

    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)

    def numnode(qtext):
        with conn.cursor() as c:
            c.execute("SELECT numnode(websearch_to_tsquery('english', %s)) AS n", (qtext,))
            return c.fetchone()["n"]

    def session_statement_timeout():
        with conn.cursor() as c:
            c.execute("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'")
            return c.fetchone()["setting"]

    baseline_statement_timeout = session_statement_timeout()

    # --- 1. normal query unchanged ----------------------------------------------------
    short = "alpha billing ingest"
    check("fts_clamp leaves a normal (no operators, under-cap) query byte-for-byte unchanged",
          fts_clamp(short, conn) == short)

    # --- 1b. SHORT queries WITH operators are ALSO byte-for-byte unchanged -------------
    SHORT_OPERATOR_QUERIES = [
        ("negation (review repro)", 'python -django'),
        ("quoted phrase (review repro)", 'find memories about "kill switch"'),
        ("OR (review repro)", 'deploy OR release notes'),
        ("bare leading dash", "-secret plan"),
        ("multiple negations", "python -django -flask web"),
        ("quoted + negation mixed", '"exact phrase here" -excluded word'),
        ("ordinary kebab-case identifier", "fix the co-worker-facing self-driving bug"),
    ]
    for label, q in SHORT_OPERATOR_QUERIES:
        out = fts_clamp(q, conn)
        check(f"[{label}] under cap+bound: fts_clamp('{q}') is byte-for-byte unchanged "
              f"(got {out!r}) -- negation/quote/OR/hyphen semantics preserved", out == q)

    # --- 6. never raises, even on degenerate input --------------------------------------
    for label, qtext in [("empty", ""), ("only-quotes", '"""""'), ("only-OR", "OR OR OR"),
                          ("only-dashes", "----"), ("unicode", "café ééé OR " * 5)]:
        try:
            fts_clamp(qtext, conn)
            check(f"fts_clamp does not raise on degenerate input ({label})", True)
        except Exception as e:
            check(f"fts_clamp does not raise on degenerate input ({label}) -- {e}", False)

    # baseline: a plain query of `cap` words, no operators at all.
    plain_baseline = " ".join(f"w{i}" for i in range(cap))
    baseline_nodes = numnode(fts_clamp(plain_baseline, conn))
    print(f"  plain {cap}-word baseline numnode = {baseline_nodes}; safety bound = {node_bound}")

    # --- 7. truncation mid-phrase is safe (unbalanced quote) ----------------------------
    long_phrase = '"' + " ".join(f"ph{i}" for i in range(cap + 20)) + '"'
    truncated = fts_clamp(long_phrase, conn)
    check("truncating a quoted phrase leaves an unbalanced opening quote (expected -- "
          "this is exactly the case being verified as SAFE, not avoided)",
          truncated.count('"') == 1)
    unbalanced_nodes = numnode(truncated)
    balanced_equivalent_nodes = numnode(truncated + '"')   # same tokens, quote properly closed
    check(f"websearch_to_tsquery treats the unbalanced trailing quote leniently -- same "
          f"numnode ({unbalanced_nodes}) as the properly-closed equivalent "
          f"({balanced_equivalent_nodes}), not an error or an empty/degenerate match",
          unbalanced_nodes == balanced_equivalent_nodes and unbalanced_nodes > 0)

    # --- 2 & 3. THE SECOND REVIEW'S EXACT REPROS ----------------------------------------
    print("=== hyphen-decomposition repros (PR #43 second review) ===")

    # exact shape: 40 words (== cap), each a 27-part kebab identifier. The old pure-Python
    # estimate judged this "safe" (79 nodes, under the 120 bound) -- it isn't.
    q_headline = " ".join(_kebab_word(26) for _ in range(40))
    headline_words = len(q_headline.split())
    check(f"sanity: headline repro is exactly {cap} whitespace words (== cap, so the OLD "
          "estimate would have judged it safe on word count alone)", headline_words == cap)
    real_nodes_unclamped = numnode(q_headline)
    check(f"the headline repro's REAL numnode() ({real_nodes_unclamped}) is drastically "
          f"over the bound ({node_bound}) despite being AT the token cap -- this is the "
          "gap the old Python estimate (79) missed entirely", real_nodes_unclamped > 1000)

    out_headline = fts_clamp(q_headline, conn)
    check("fts_clamp does NOT pass the headline repro through unchanged (the old, wrong "
          "behavior) -- it's genuinely too complex and must be shrunk",
          out_headline != q_headline)
    out_headline_nodes = numnode(out_headline)
    check(f"fts_clamp's shrunk output for the headline repro has a REAL numnode() "
          f"({out_headline_nodes}) confirmed within the safety bound ({node_bound})",
          out_headline_nodes <= node_bound)

    # exact shape: the density that reproduced a real, unhandled ProgramLimitExceeded
    # through the pass-through path before this fix. Uses the connection's own (generous,
    # unrestricted) timeout -- not a tight one -- so a slow-but-eventually-erroring probe
    # surfaces the real ProgramLimitExceeded rather than getting preempted by a timeout.
    q_crash = " ".join(_kebab_word(5000) for _ in range(40))
    try:
        with conn.cursor() as c:
            c.execute("SELECT numnode(websearch_to_tsquery('english', %s)) AS n", (q_crash,))
            c.fetchone()
        crash_confirmed = False
    except (psycopg.errors.ProgramLimitExceeded, psycopg.errors.QueryCanceled):
        crash_confirmed = True
    check("sanity: computing numnode() directly on the crash-density input DOES raise "
          "an error on this Postgres (ProgramLimitExceeded, or QueryCanceled if the "
          "connection's own timeout preempts it first) -- confirms the trap is real",
          crash_confirmed)

    try:
        out_crash = fts_clamp(q_crash, conn)
        crash_raised = False
    except Exception as e:
        out_crash, crash_raised = None, True
        print(f"    fts_clamp RAISED on the crash-density input: {type(e).__name__}: {e}")
    check("fts_clamp handles the crash-density input WITHOUT raising -- the exact "
          "unhandled-crash scenario the second review reproduced is now caught",
          not crash_raised)
    if not crash_raised:
        out_crash_nodes = numnode(out_crash)
        check(f"fts_clamp's output for the crash-density input has a REAL numnode() "
              f"({out_crash_nodes}) confirmed within the safety bound ({node_bound})",
              out_crash_nodes <= node_bound)

    # the crash-density input forces multiple internal probes (each setting a tight
    # statement_timeout, at least one of them timing out/erroring) before converging on a
    # safe result -- confirm the session is left at its ORIGINAL statement_timeout
    # afterward, not stuck at the probe's tight one. A leaked tight timeout wouldn't crash
    # anything here, but it would silently sabotage the next (unrelated, possibly
    # expensive) query run on this same pooled connection.
    check("fts_clamp restores the session's original statement_timeout after the "
          "multi-probe shrink path (no leaked tight timeout onto the next query)",
          session_statement_timeout() == baseline_statement_timeout)

    # --- THE THIRD REVIEW'S EXACT REPRO: bare hyphen run + NUL byte ---------------------
    # _tsquery_within_bound's except clause originally caught only ProgramLimitExceeded
    # and QueryCanceled -- the two error shapes the second review's kebab-identifier fuzz
    # happened to hit. A third review found a THIRD, categorically different shape slips
    # straight through uncaught: a single whitespace-token consisting of ONLY hyphens
    # (no alphanumeric parts between them -- a markdown horizontal rule / YAML frontmatter
    # delimiter / ASCII divider, not the "p0-p1-p2-..." kebab-identifier shape the fuzz
    # above already covers) raises psycopg.errors.InternalError_ ("tsquery stack too
    # small", SQLSTATE XX000) once the run is long enough (minimal repro measured at 33
    # hyphens on this Postgres build -- the exact cliff isn't pinned as a hard assertion
    # here since it's a property of Postgres's compiled stack-depth limit, not of this
    # fix, and could shift a hyphen or two on a different build/arch) -- confirmed via
    # MRO that InternalError_ shares no ancestor with either originally-caught type. This
    # is a live reproduction of issue #41's original crash, through completely ordinary
    # text, not adversarial fuzzing. The fix (store.py's _tsquery_within_bound) now
    # catches psycopg.DatabaseError -- broad by class, not by enumerating a third specific
    # type (repeating that mistake a third time is exactly what this round of review
    # flagged).
    print("=== bare-hyphen-run + NUL byte repros (PR #43 third review) ===")

    divider_query = "find the ---------------------------------------- divider in my notes"
    # comfortably past the ~33-hyphen cliff (not pinned exactly, see above) so this case
    # stays a reliable repro even if the cliff shifts slightly on a different Postgres build
    bare_hyphen_run = "-" * 200

    for label, qtext in [("markdown-divider sentence (reviewer's exact repro)", divider_query),
                          ("bare 200-hyphen token (comfortably past the cliff)", bare_hyphen_run)]:
        try:
            numnode(qtext)
            direct_raised, direct_exc = False, None
        except Exception as e:
            direct_raised, direct_exc = True, e
        check(f"sanity: [{label}] computing numnode() directly DOES raise on this "
              f"Postgres (confirms the trap is real) "
              f"{'-- ' + type(direct_exc).__name__ if direct_raised else ''}",
              direct_raised)
        check(f"sanity: [{label}] the raised error is InternalError_, NOT "
              "ProgramLimitExceeded/QueryCanceled (a genuinely different exception "
              "branch than the second review's repro -- proves this needs the broad "
              "catch, not a third enumerated type)",
              direct_raised and isinstance(direct_exc, psycopg.errors.InternalError_))

        try:
            out = fts_clamp(qtext, conn)
            clamp_raised = False
        except Exception as e:
            out, clamp_raised = None, True
            print(f"    fts_clamp RAISED on [{label}]: {type(e).__name__}: {e}")
        check(f"fts_clamp handles [{label}] WITHOUT raising -- the exact unhandled "
              "crash the third review reproduced is now caught", not clamp_raised)
        if not clamp_raised:
            out_nodes = numnode(out)
            check(f"fts_clamp's output for [{label}] has a REAL numnode() ({out_nodes}) "
                  f"confirmed within the safety bound ({node_bound})",
                  out_nodes <= node_bound)

    # bonus, same root cause: a NUL byte in the query text raises psycopg.DataError
    # through the identical unguarded path -- not introduced by this PR, but the broad
    # DatabaseError catch closes it for free (DataError is a DatabaseError subclass).
    nul_query = "hello\x00world"
    try:
        numnode(nul_query)
        nul_direct_raised, nul_direct_exc = False, None
    except Exception as e:
        nul_direct_raised, nul_direct_exc = True, e
    check("sanity: NUL-byte query DOES raise directly (confirms the bonus trap is real) "
          f"{'-- ' + type(nul_direct_exc).__name__ if nul_direct_raised else ''}",
          nul_direct_raised)
    check("sanity: the NUL-byte error is psycopg.DataError, a THIRD distinct exception "
          "type from InternalError_ and ProgramLimitExceeded/QueryCanceled -- one more "
          "reason an enumerated list of types is the wrong shape of fix",
          nul_direct_raised and isinstance(nul_direct_exc, psycopg.DataError))

    try:
        nul_out = fts_clamp(nul_query, conn)
        nul_clamp_raised = False
    except Exception as e:
        nul_out, nul_clamp_raised = None, True
        print(f"    fts_clamp RAISED on NUL-byte query: {type(e).__name__}: {e}")
    check("fts_clamp handles the NUL-byte query WITHOUT raising (closed for free by the "
          "same broad-catch fix)", not nul_clamp_raised)
    if not nul_clamp_raised:
        nul_out_nodes = numnode(nul_out)
        check(f"fts_clamp's output for the NUL-byte query has a REAL numnode() "
              f"({nul_out_nodes}) confirmed within the safety bound ({node_bound})",
              nul_out_nodes <= node_bound)

    check("fts_clamp restores the session's original statement_timeout after the "
          "bare-hyphen-run/NUL-byte repros too (no leaked tight timeout)",
          session_statement_timeout() == baseline_statement_timeout)

    # --- differential hyphen-density fuzz against live numnode() ------------------------
    # Not just the two fixed repros: sweep a range of densities (mild/realistic through
    # pathological) so this class of gap can't silently reopen for some density nobody
    # thought to pin as a named test case.
    print("=== hyphen-density differential fuzz (vs live numnode()) ===")
    passthrough_seen = False
    # (word_count, hyphens/word) pairs: the first few are deliberately BELOW cap word
    # count as well as low hyphen density, so at least one case is genuinely safe and
    # must round-trip byte-for-byte unchanged -- exercising the "judged safe, no shrink"
    # branch, not just "judged unsafe, shrink and reverify" every time.
    density_cases = [(5, 1), (5, 2)] + [(cap, h) for h in (1, 2, 5, 26, 50, 200, 1000, 5000)]
    for n_words, hyphens in density_cases:
        q = " ".join(_kebab_word(hyphens) for _ in range(n_words))
        out = fts_clamp(q, conn)
        if out == q:
            passthrough_seen = True
            # fts_clamp judged this density safe to pass through unchanged -- verify that
            # against REAL numnode(), not trust the judgment.
            try:
                real = numnode(out)
                ok = real <= node_bound
                detail = f"real numnode()={real} <= bound {node_bound}"
            except (psycopg.errors.ProgramLimitExceeded, psycopg.errors.QueryCanceled) as e:
                ok, detail = False, f"passed through UNCHANGED but numnode() itself raised {type(e).__name__}"
        else:
            # fts_clamp shrunk it -- verify the shrunk output is REALLY safe, not just
            # shorter.
            try:
                real = numnode(out)
                ok = real <= node_bound
                detail = f"shrunk to {len(out.split())} word(s), real numnode()={real} <= bound {node_bound}"
            except (psycopg.errors.ProgramLimitExceeded, psycopg.errors.QueryCanceled) as e:
                ok, detail = False, f"shrunk output STILL raised {type(e).__name__}"
        check(f"[{n_words}x kebab, {hyphens} hyphens/word] fts_clamp's decision is verified "
              f"safe against live numnode() ({detail})", ok)

    check("differential fuzz includes at least one genuinely-safe case that fts_clamp "
          "passes through UNCHANGED (not just cases it shrinks) -- otherwise the "
          "pass-through branch itself is never exercised by this fuzz",
          passthrough_seen)

    # --- 4 & 5. shape invariance + before/after reduction, non-hyphen adversarial shapes -
    N = 199
    for label, build in ADVERSARIAL_SHAPES.items():
        adversarial = build(N)
        new_clamped = fts_clamp(adversarial, conn)
        old_clamped = _old_clamp(adversarial, cap=200)

        check(f"[{label}] fts_clamp output respects the token cap (<= {cap})",
              len(new_clamped.split()) <= cap)

        new_nodes = numnode(new_clamped)
        old_nodes = numnode(old_clamped)

        check(f"[{label}] new-clamp numnode ({new_nodes}) stays within the safety bound "
              f"({node_bound}), confirmed against real Postgres", new_nodes <= node_bound)

        check(f"[{label}] new clamp builds a SMALLER (or equal) tsquery than the old, "
              f"shape-blind clamp for the same adversarial input (old={old_nodes}, new={new_nodes})",
              new_nodes <= old_nodes)

    conn.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
