"""fts_clamp shape-invariance gate (issue #41 fix B).

Before this fix, fts_clamp bounded ONLY whitespace-token count (default 200), on the
theory that a shorter query builds a smaller tsquery. That's false in general: node count
is what determines parser cost, not word count, and (measured below) a leading "-" on
every word inflates node count ~1.5x over plain words at the SAME word count — a NOT-
wrapped term costs more than a plain AND term. Quoted phrases and "OR" are also stripped
here (the issue names them as suspect too) even though, on this Postgres version, they did
NOT show the same per-word amplification — see the module docstring in core/store.py for
the full measured breakdown.

What this file proves, against a REAL Postgres (numnode() is the ground truth for tsquery
complexity, not a Python approximation):

  1. NORMAL QUERY UNCHANGED: a plain query under the cap, with none of the shape-driving
     constructs, is returned byte-for-byte identical — the fix doesn't touch what it
     doesn't need to.
  2. SHAPE INVARIANCE: for an adversarial corpus (a single long quoted phrase, many
     degenerate one-word "phrases", a long OR-chain, a leading-"-" chain, and an irregular
     mix of all of them) fts_clamp's output builds a tsquery whose numnode() stays under a
     fixed bound, close to the byte-for-byte-plain-query baseline — i.e. the WORST-CASE
     shape no longer costs more than the best case.
  3. FIX REDUCES COMPLEXITY: for the same adversarial input, the OLD clamp (200-token,
     shape-blind) built a LARGER tsquery than the NEW clamp — a direct, measured
     before/after contrast, not just an absolute bound.
  4. NEVER RAISES: fts_clamp itself has no DB dependency and cannot fail on any input here
     (empty string, pure operators, unicode).

HONESTY NOTE (see the PR description for the full writeup): extensive adversarial testing
against a live pg16 instance — every shape below, up to tens of thousands of tokens, with
max_stack_depth forced to Postgres's enforced minimum — never reproduced the
"tsquery stack too small" crash issue #41 reports for websearch_to_tsquery specifically
(numnode scaled linearly, ~2x word count, identically for phrase and plain input). That
error IS real and reproducible via to_tsquery with deeply nested parentheses, but
websearch_to_tsquery's grammar has no parenthesization syntax and looks unreachable
through it on this Postgres version. This file therefore asserts what the fix actually,
measurably does — bound and reduce tsquery complexity for adversarial shapes — rather than
a fabricated crash repro.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import psycopg

from core.store import fts_clamp, _fts_max_tokens

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
    print(f"  plain {cap}-word baseline numnode = {baseline_nodes}")

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

        # shape invariance: worst case no worse than ~2x the plain-word baseline for the
        # SAME effective word budget (a flat AND chain of `cap` words is ~2*cap-1 nodes).
        check(f"[{label}] new-clamp numnode ({new_nodes}) stays close to the plain-word "
              f"baseline ({baseline_nodes}) -- shape no longer multiplies node count",
              new_nodes <= baseline_nodes + 2)

        check(f"[{label}] new clamp builds a SMALLER (or equal) tsquery than the old, "
              f"shape-blind clamp for the same adversarial input (old={old_nodes}, new={new_nodes})",
              new_nodes <= old_nodes)

    conn.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
