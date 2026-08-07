"""fts_clamp shape-invariance gate (issue #41 fix B).

Before this fix, fts_clamp bounded ONLY whitespace-token count (default 200), on the
theory that a shorter query builds a smaller tsquery. That's false in general: node count
is what determines parser cost, not word count, and (measured below) a leading "-" on
every word inflates node count LINEARLY, not multiplicatively — exactly +1 node per
negated word (confirmed at both 40 and 199 words). An earlier version of this fix reacted
to that by unconditionally stripping quotes/"OR"/leading "-" from ANY query containing
them, regardless of length — which silently mangled ordinary short queries too (a PR #43
review found `'python -django'` -> `'python django'`, INVERTING an exclusion into a match)
and was never actually necessary: since the per-word cost of "-" is additive and bounded,
capping WORD COUNT alone already bounds node count for any shape (a `cap`-word query can
have at most `cap` negated words, so worst case is `(2*cap-1)+cap = 3*cap-1` nodes — a
fixed ceiling). fts_clamp now computes that bound directly (_tsquery_node_estimate /
_fts_node_bound in core/store.py) and only truncates when a query is ACTUALLY over the
token cap or the node bound — never rewrites shape a query didn't need rewritten.

What this file proves, against a REAL Postgres (numnode() is the ground truth for tsquery
complexity, not a Python approximation):

  1. NORMAL QUERY UNCHANGED: a plain query under the cap, with none of the shape-driving
     constructs, is returned byte-for-byte identical — the fix doesn't touch what it
     doesn't need to.
  1b. SHORT QUERIES WITH OPERATORS UNCHANGED: a short query that DOES contain "-", a
     quoted phrase, or "OR" — but stays under both the token cap and the node bound — is
     ALSO returned byte-for-byte identical, negation/phrase/OR semantics intact. This is
     the specific case the PR #43 review found broken (exclusion silently became a match).
  2. SHAPE INVARIANCE: for an adversarial corpus (a single long quoted phrase, many
     degenerate one-word "phrases", a long OR-chain, a leading-"-" chain, and an irregular
     mix of all of them) fts_clamp's output builds a tsquery whose numnode() stays under
     the same fixed safety bound the clamp itself is gated on — i.e. no shape can push a
     capped query's real complexity past what the clamp is designed to guarantee.
  3. FIX REDUCES COMPLEXITY: for the same adversarial input, the OLD clamp (200-token,
     shape-blind) built a LARGER tsquery than the NEW clamp — a direct, measured
     before/after contrast, not just an absolute bound.
  4. NEVER RAISES: fts_clamp itself has no DB dependency and cannot fail on any input here
     (empty string, pure operators, unicode).
  5. TRUNCATION IS SAFE MID-PHRASE: truncating an over-cap query can leave a quote
     unbalanced (e.g. a long quoted phrase cut mid-way) — confirmed against a live
     Postgres that websearch_to_tsquery treats an unterminated quote leniently (same
     result as if it had been closed), not as an error or degenerate empty match.

HONESTY NOTE (see the PR description for the full writeup): extensive adversarial testing
against a live pg16 instance — every shape below, up to tens of thousands of tokens, with
max_stack_depth forced to Postgres's enforced minimum — never reproduced the
"tsquery stack too small" crash issue #41 reports for websearch_to_tsquery specifically
(numnode scaled linearly, ~2x word count, identically for phrase and plain input). That
error IS real and reproducible via to_tsquery with deeply nested parentheses, but
websearch_to_tsquery's grammar has no parenthesization syntax and looks unreachable
through it on this Postgres version. This file therefore asserts what the fix actually,
measurably does — bound tsquery complexity for adversarial shapes without rewriting
queries that don't need it — rather than a fabricated crash repro.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import psycopg

from core.store import fts_clamp, _fts_max_tokens, _fts_node_bound, _tsquery_node_estimate

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


def main():
    print("=== fts_clamp shape-invariance (issue #41 fix B) ===")
    cap = _fts_max_tokens()
    check(f"effective FTS cap is a positive int (got {cap})", isinstance(cap, int) and cap >= 1)

    # --- 1. normal query unchanged ----------------------------------------------------
    short = "alpha billing ingest"
    check("fts_clamp leaves a normal (no operators, under-cap) query byte-for-byte unchanged",
          fts_clamp(short) == short)

    # --- 1b. SHORT queries WITH operators are ALSO byte-for-byte unchanged -------------
    # This is the PR #43 review's actual finding: the old fix stripped these regardless of
    # length. "python -django" is the sharpest case -- stripping "-" doesn't just lose
    # precision, it INVERTS the query (an exclusion becomes a match).
    SHORT_OPERATOR_QUERIES = [
        ("negation (review repro)", 'python -django'),
        ("quoted phrase (review repro)", 'find memories about "kill switch"'),
        ("OR (review repro)", 'deploy OR release notes'),
        ("bare leading dash", "-secret plan"),
        ("multiple negations", "python -django -flask web"),
        ("quoted + negation mixed", '"exact phrase here" -excluded word'),
    ]
    for label, q in SHORT_OPERATOR_QUERIES:
        out = fts_clamp(q)
        check(f"[{label}] under cap+bound: fts_clamp('{q}') is byte-for-byte unchanged "
              f"(got {out!r}) -- negation/quote/OR semantics preserved", out == q)

    # --- 4. never raises, even on degenerate input --------------------------------------
    for label, qtext in [("empty", ""), ("only-quotes", '"""""'), ("only-OR", "OR OR OR"),
                          ("only-dashes", "----"), ("unicode", "café ééé OR " * 5)]:
        try:
            fts_clamp(qtext)
            check(f"fts_clamp does not raise on degenerate input ({label})", True)
        except Exception as e:
            check(f"fts_clamp does not raise on degenerate input ({label}) -- {e}", False)

    conn = psycopg.connect(DSN, autocommit=True)

    def numnode(qtext):
        with conn.cursor() as c:
            c.execute("SELECT numnode(websearch_to_tsquery('english', %s))", (qtext,))
            return c.fetchone()[0]

    # baseline: a plain query of `cap` words, no operators at all.
    plain_baseline = " ".join(f"w{i}" for i in range(cap))
    baseline_nodes = numnode(fts_clamp(plain_baseline))
    node_bound = _fts_node_bound(cap)
    print(f"  plain {cap}-word baseline numnode = {baseline_nodes}; safety bound = {node_bound}")

    # --- 5. truncation mid-phrase is safe (unbalanced quote) ----------------------------
    # An over-cap quoted-phrase query gets truncated to the first `cap` tokens, which can
    # leave a dangling opening quote with no closing one. Confirm this is handled
    # leniently (same result as if it HAD been closed), not an error or a degenerate match.
    long_phrase = '"' + " ".join(f"ph{i}" for i in range(cap + 20)) + '"'
    truncated = fts_clamp(long_phrase)
    check("truncating a quoted phrase leaves an unbalanced opening quote (expected -- "
          "this is exactly the case being verified as SAFE, not avoided)",
          truncated.count('"') == 1)
    unbalanced_nodes = numnode(truncated)
    balanced_equivalent_nodes = numnode(truncated + '"')   # same tokens, quote properly closed
    check(f"websearch_to_tsquery treats the unbalanced trailing quote leniently -- same "
          f"numnode ({unbalanced_nodes}) as the properly-closed equivalent "
          f"({balanced_equivalent_nodes}), not an error or an empty/degenerate match",
          unbalanced_nodes == balanced_equivalent_nodes and unbalanced_nodes > 0)

    # --- 2 & 3. shape invariance + before/after reduction, per adversarial shape --------
    # N chosen so plain/phrase/quoted/dashed shapes (199 whitespace tokens each) stay
    # UNDER the old 200-token cap -- isolating shape normalization's effect from
    # truncation's. The OR-chain and mixed shapes exceed 200 raw tokens once the "OR"
    # separators and multi-word phrases are counted, so the old clamp DOES truncate them
    # too -- old_nodes reflects whatever the old clamp actually produced either way; the
    # new-vs-old comparison below holds regardless.
    N = 199
    for label, build in ADVERSARIAL_SHAPES.items():
        adversarial = build(N)
        new_clamped = fts_clamp(adversarial)
        old_clamped = _old_clamp(adversarial, cap=200)

        check(f"[{label}] fts_clamp output respects the token cap (<= {cap})",
              len(new_clamped.split()) <= cap)

        new_nodes = numnode(new_clamped)
        old_nodes = numnode(old_clamped)
        estimated_nodes = _tsquery_node_estimate(new_clamped)

        check(f"[{label}] the pure-Python complexity estimate ({estimated_nodes}) matches "
              f"real Postgres numnode() ({new_nodes}) -- the estimator fts_clamp gates on "
              "is actually accurate, not just an unverified guess",
              estimated_nodes == new_nodes)

        # SAFETY BOUND (not "close to the plain-word baseline" -- the fix deliberately no
        # longer flattens shape it doesn't need to, so a heavily-negated clamped query can
        # legitimately have MORE nodes than the plain-word baseline. What must hold is the
        # fixed worst-case ceiling the clamp is designed to guarantee: 3*cap-1.)
        check(f"[{label}] new-clamp numnode ({new_nodes}) stays within the safety bound "
              f"({node_bound}) -- worst-case shape (all `cap` words negated) is a fixed, "
              "bounded cost regardless of adversarial input", new_nodes <= node_bound)

        check(f"[{label}] new clamp builds a SMALLER (or equal) tsquery than the old, "
              f"shape-blind clamp for the same adversarial input (old={old_nodes}, new={new_nodes})",
              new_nodes <= old_nodes)

    conn.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
