"""B1 — storage backend for the brain-inspired schema.

One ACID Postgres engine. Writes raw_turns (verbatim), episodic events, entities/
mentions/edges (associative graph), and (for B2) semantic facts + provenance.
Schema identifiers are validated; values are parameterized.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[a-z0-9_]+$")


def vlit(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


# Postgres builds an in-memory parse tree for websearch_to_tsquery whose depth scales with
# the number of lexemes; a very long recall query (thousands of tokens) overflows the
# backend stack — "tsquery stack too small" / "stack depth limit exceeded" — and a recall
# that should return 200 instead crashes (issue #15). The query's discriminative signal
# lives in its first words anyway, so clamp the text fed to FTS to a sane token cap. The
# FULL query is still used for the vector arm (the embedding is order-insensitive and
# fixed-size) and for the cross-encoder, so retrieval quality on normal queries is
# unchanged — only pathological queries are bounded. MEMNOS_FTS_MAX_TOKENS tunes the cap.
#
# issue #41 fix B: the #15 clamp bounded whitespace-token COUNT, but tsquery-parser
# overflow ("tsquery stack too small") is a function of the built tsquery's node/operator
# count, not word count, so word count alone is the wrong thing to bound. Measured against
# a live pg16 (numnode() on the built tsquery, same word count each time): a leading "-"
# on every word measurably inflates node count, but LINEARLY and ADDITIVELY (+1 node per
# negated word, not a multiplier that compounds with query length); quoted phrases and
# "OR" don't inflate at all on this Postgres version.
#
# A PRIOR version of this fix (see PR #43's review history) concluded from that data that
# bounding WORD COUNT alone therefore bounds node count for ANY shape, and computed a
# pure-Python estimate to gate on instead of asking Postgres directly. That conclusion was
# FALSE, and a follow-up adversarial review found the counterexample: a word containing
# INTERNAL hyphens (kebab-case identifiers — file paths, stack traces, branch names, CSS
# selectors, exactly the text a coding-assistant memory store fields constantly) doesn't
# cost one node like a plain word. Postgres's text-search parser DECOMPOSES a hyphenated
# compound into multiple sub-lexemes joined by phrase operators — measured live: a single
# word with 200 internal hyphens alone already produces >400 nodes, and 40 such words
# together produced numnode() in the low thousands, versus a Python word-count estimate of
# 79. At high enough hyphen density this reproduces a real, unhandled
# `psycopg.errors.ProgramLimitExceeded: value is too big in tsquery` straight through the
# pass-through path the estimate judged "safe" — the exact crash class issue #41 exists to
# eliminate, just via a shape the leading-"-"/quote/OR analysis never covered.
#
# The lesson: modeling Postgres's tsquery-construction cost in Python is fragile by
# construction — it's correct for exactly the constructs someone thought to measure, and
# silently wrong (in the dangerous, UNDER-counting direction) for the next one nobody
# fuzzed yet. So fts_clamp no longer estimates complexity at all. It asks Postgres
# directly: build the actual tsquery and check its actual numnode() against the bound, via
# a short-lived, tightly time-boxed probe (see _tsquery_within_bound) that can never itself
# become the failure point — even a query so pathological that BUILDING it for the probe
# would blow the tsquery parser's own internal limit is caught there and treated as "not
# safe," never allowed to propagate. This is authoritative for any current or future
# tsquery-expanding construct, not just the ones already known about.
#
# issue #41 fix B, round 3: the probe's own except clause repeated exactly the mistake this
# section just described — it caught only the two specific error types (ProgramLimitExceeded,
# QueryCanceled) observed from the shapes tested so far, and a THIRD live Postgres error
# shape slipped straight through uncaught: a bare run of >=33 consecutive literal hyphens
# (an ordinary markdown/YAML/ASCII divider — ordinary content, not adversarial fuzzing)
# raises `psycopg.errors.InternalError_: tsquery stack too small` (SQLSTATE XX000), which
# shares no ancestor with either caught type and reproduced issue #41's original crash
# straight through the code path built to eliminate it. A NUL byte in the query text
# (`psycopg.DataError`, raised by psycopg itself before the statement ever reaches the
# server — no SQLSTATE, since Postgres never saw it) hits the same unguarded path.
# Enumerating a third specific type would repeat the identical mistake a third time for
# whatever shape nobody's tried yet, so the except clause now catches
# `psycopg.DatabaseError` — every DatabaseError-class failure this probe query can raise,
# both server-reported (SQLSTATE-carrying) errors and psycopg's own pre-flight input
# rejections alike, by construction, not by enumeration. It deliberately does NOT catch
# `psycopg.InterfaceError` (misuse of the driver/connection itself — e.g. executing
# against an already-closed cursor — a bug in this code, not a property of the query
# text, and one that should crash loudly rather than be silently filed as "unsafe
# input").
#
# issue #41 fix C: even with the bound above, a single recall arm's statement_timeout
# cancellation or DB-side error used to propagate straight out of core/service.py's
# recall_fetch/recall_wide_fetch and fail the WHOLE recall — the hybrid design has
# independent arms (raw-turn search, semantic search, the timeline/entity-guarantee
# arms, the wide-recall per-namespace fan-out), so one arm's live, reachable-server
# failure shouldn't cost the others. RECALL_ARM_FAILURES is the exception tuple those
# call sites catch to degrade instead of raise. Caught BY CLASS, not enumerated by
# specific error, per the exact precedent set by _tsquery_within_bound below (issue #41
# fix B, round 3) — an enumerated except list is correct for exactly the failure shapes
# someone thought to test and silently wrong for the next one nobody hit yet.
# psycopg.DatabaseError is the common ancestor of every server-reported failure
# (statement_timeout cancellation, internal errors, data errors, operational errors —
# psycopg_pool.PoolTimeout included, since it subclasses psycopg.OperationalError) and
# of input psycopg itself rejects before it ever reaches the server; the bare
# TimeoutError covers any non-psycopg wait timeout the same call could hit. Deliberately
# does NOT include psycopg.InterfaceError — client/driver misuse (e.g. executing against
# an already-closed cursor) is a bug in the CALLING code, not a property of the query or
# the data, and must still crash loudly rather than be silently degraded away.
#
# issue #49: _tsquery_within_bound's own except clause (below) used to be exactly this
# broad too — every DatabaseError-class failure of the numnode() probe treated as "this
# query is pathological, clamp it." That conflated two different things: a query whose
# OWN shape overflows the tsquery parser (a real, reproducible property of the query
# text) versus the probe itself failing for a reason that has nothing to do with the
# query at all — QueryCanceled from unrelated admin/lock contention, AdminShutdown, a
# dropped connection. Under sustained failure of the latter kind, an entirely ordinary
# query got progressively shrunk to a single character, chasing a signal that was never
# about the query's complexity. _tsquery_within_bound now only treats a FIXED, narrow set
# of exceptions as query-shape evidence (ProgramLimitExceeded: the parser's own "value is
# too big in tsquery" limit; InternalError_: "tsquery stack too small", XX000 — a generic
# internal-error SQLSTATE, but the only cause this exact probe query is known to produce
# it for; DataError: psycopg's own pre-flight rejection of the query text, e.g. an
# embedded NUL byte, which never reaches the server at all and so cannot be an infra
# symptom). Every other DatabaseError-class failure (QueryCanceled included — it's
# genuinely ambiguous between "the probe legitimately ran out of its own tight budget"
# and "something unrelated cancelled it") is resolved with a control probe rather than a
# guess: ask the server ONE trivial numnode() question under the same tight timeout, on
# the same cursor. If the control succeeds, the server is healthy and the ORIGINAL
# failure really is attributable to this query's text — clamp it, exactly as before. If
# the control ALSO fails, the probe itself — not the query — is what's broken, and the
# original exception is re-raised rather than swallowed. Re-raising here is deliberate,
# not a loose end: every fts_clamp call site is a BrainStore search method invoked from
# core/service.py under the RECALL_ARM_FAILURES degrade-not-raise path (issue #41 fix C,
# directly above) — so a genuine probe-infrastructure failure degrades that ONE recall
# arm to a partial result, the same as any other live DB failure on that arm, instead of
# either silently passing an unverified query through as "safe" or silently mis-filing an
# ordinary query as "pathological."
RECALL_ARM_FAILURES = (psycopg.DatabaseError, TimeoutError)

# issue #59: cold-start-aware classification for the phase-A recall arms
# (max_observed_at/timeline here; readable_namespaces/pinned_constraints's call sites in
# memnos_server.py) that recall_prefetch runs FIRST, before the query embedding even
# arrives -- the most likely DB touches to race a cold connection/cache right after a
# restart. This mirrors fts_clamp's two-tier probe (#49/#58) in SPIRIT, not mechanism.
# fts_clamp needs a genuinely DIFFERENT control query because a pathological tsquery
# never succeeds no matter how long you wait -- the ambiguity there is "probe infra
# broken" vs "query shape is the problem". These arms have no such pathology risk: an
# ordinary query that got cancelled here is either (a) the connection/server is actually
# unresponsive, or (b) it's legitimately slow because the HNSW/GIN indexes it touches
# haven't paged into cache yet (issue #31, real field data: 20-60s of exactly this right
# after a restart at 143K-fact scale). RETRYING the same slow query inline under a much
# wider timeout was considered and rejected: it would turn a fast, already-correct
# degrade (issue #41 fix C) into a multi-second-to-multi-minute hang on the client's
# /recall call, which is worse than today's behavior, not better. Instead this runs ONE
# cheap, short control probe (SELECT 1) on the SAME connection when an arm fails: if it
# succeeds, the connection/server is alive, so the ORIGINAL failure is plausibly the
# cold-cache case -- give the client a legible "likely still warming up, retry shortly"
# hint instead of a bare exception class name (issue #31 acceptance criterion: recall
# must return "a clear 'warming up, retry' signal... rather than an opaque
# QueryCanceled"). If the control ALSO fails, the connection itself is the problem, not
# a warm-up delay, and the hint says so -- worth investigating, not just waiting out.
_COLDSTART_CONTROL_TIMEOUT_MS = int(os.environ.get("MEMNOS_COLDSTART_CONTROL_TIMEOUT_MS", "1500"))


def classify_arm_failure(conn, exc: Exception) -> str | None:
    """Best-effort hint for WHY a RECALL_ARM_FAILURES-class exception happened, via one
    cheap control probe on `conn` (same autocommit-required invariant as
    _tsquery_within_bound's control probe -- true of every BrainStore connection).
    Classification is diagnostic only and must never itself raise or replace the
    original exception; any failure to classify returns None (caller falls back to the
    exception class name alone, exactly today's behavior).

    NOTE: a control probe can only prove "the connection/server is alive", not WHY the
    original query was slow — cold cache (issue #31) is one cause, but lock contention
    (e.g. the ACCESS EXCLUSIVE lock test_recall_arm_degrade_http.py itself uses to force
    this path) or ordinary load are others. The hint is worded to match exactly what was
    proven, not to guess a specific cause it can't actually distinguish."""
    try:
        with conn.cursor() as c:
            c.execute(f"SET statement_timeout = {_COLDSTART_CONTROL_TIMEOUT_MS}")
            try:
                c.execute("SELECT 1")
                c.fetchone()
                return ("connection is responsive — this arm's own query was transient "
                        "(blocked, slow, or a cold cache right after a restart); safe to retry")
            except psycopg.DatabaseError:
                return "connection itself is unresponsive — not a transient per-query delay"
            finally:
                try:
                    c.execute("SET statement_timeout = DEFAULT")
                except (psycopg.DatabaseError, psycopg.InterfaceError):
                    pass
    except Exception:
        return None


def record_arm_failure(reasons, namespace, arm, exc, hint=None):
    """issue #41 fix C: a recall/diagnostic arm (raw/semantic search, the timeline/
    entity-guarantee arm, a wide-recall per-namespace fetch, one of knowledge_health's
    structural-signal queries -- issue #69) hit a RECALL_ARM_FAILURES-class error — log
    the FULL detail server-side (same place #41 was originally diagnosed from: the
    server log) and, if the caller wants a client-visible record, append a SANITIZED
    entry: namespace, which arm, the exception's class name, and its SQLSTATE if it has
    one. Deliberately never the raw exception message — that can echo query text — into
    anything that reaches the client; the message stays server-side in the log line.

    Lives here (not core/service.py, where it originated) so core/store.py — the module
    that already owns RECALL_ARM_FAILURES and classify_arm_failure — can call it directly
    from BrainStore methods like health() without a store->service->store import cycle
    (core/service.py imports FROM core/store.py, never the reverse). core/service.py
    imports this same function rather than keeping its own copy, so every caller shares
    ONE degrade-not-raise implementation instead of two that could drift apart.

    issue #59: `hint` is an OPTIONAL, already-classified string (see classify_arm_failure
    above) a caller can pass when it ran the cheap cold-start-vs-genuinely-broken control
    probe — turns an opaque exception name into a legible "still warming up, retry" or
    "connection itself is unresponsive" signal. Callers that pass no hint get exactly
    today's behavior."""
    logger.warning("arm degraded: namespace=%s arm=%s %s: %s",
                   namespace, arm, type(exc).__name__, exc)
    if reasons is not None:
        entry = {"namespace": namespace, "arm": arm,
                 "error": type(exc).__name__,
                 "sqlstate": getattr(exc, "sqlstate", None)}
        if hint:
            entry["hint"] = hint
        reasons.append(entry)

# issue #69: BrainStore.health()'s full set of output keys -- kept as one tuple so the
# "the call itself never got to run" fallback (memnos_server.py's /knowledge/health
# handler, via health_unavailable() below) can't silently drift out of sync with
# health()'s own key set as signals are added there.
_HEALTH_SIGNAL_KEYS = ("facts_current", "facts_superseded", "facts_expired",
                       "entities", "orphan_entities", "contradiction_groups")


def health_unavailable(reasons) -> dict:
    """The all-signals-unknown shape /knowledge/health falls back to when
    BrainStore.health() itself raises rather than degrading internally. In practice this
    is unreachable while health() is intact -- every signal it queries is ALREADY
    individually guarded by RECALL_ARM_FAILURES (see health()'s own docstring), so a
    live DB failure degrades PER SIGNAL there, never by raising out of the whole call.
    Kept anyway as the same defense-in-depth every other diagnostic/recall arm in this
    codebase gets (issue #41 fix C) -- e.g. a future signal added to health() without
    its own guard, or the connection dying between this call and the one before it."""
    out = {"score": None}
    out.update({k: None for k in _HEALTH_SIGNAL_KEYS})
    out["degraded"] = True
    out["degraded_reasons"] = reasons
    return out

_FTS_DEFAULT_TOKENS = 40   # was 200; a lower cap bounds the size of text the safety probe
                           # itself has to evaluate, independent of the probe's own result.

# safety ceiling on tsquery node count a clamped query may have. Somewhat arbitrary (not
# derived from a hard Postgres limit — the real parser limit is far higher, see
# ProgramLimitExceeded above) but small enough that numnode() evaluation itself stays fast.
_FTS_NODE_BOUND_MULTIPLIER = 3

# statement_timeout (ms) for the numnode() safety probe itself — deliberately much
# tighter than the connection's normal request-scoped timeout, so an oversized/pathological
# probe input fails fast as a caught QueryCanceled instead of tying up the probe for
# seconds. Tunable in case a slower/busier deployment needs more headroom.
_FTS_NODE_CHECK_TIMEOUT_MS = int(os.environ.get("MEMNOS_FTS_NODE_CHECK_TIMEOUT_MS", "300"))

# issue #49 round 2: the control probe below (see the `except psycopg.DatabaseError as
# probe_exc` branch of _tsquery_within_bound) answers a different question than the main
# probe above it, and doesn't belong on the same budget. The main probe's 300ms bounds
# "how long do we wait before assuming THIS query might be pathologically expensive to
# parse" — deliberately tight, tuned for that one job. The control probe's only job is "is
# the connection/server alive and responsive AT ALL", a fundamentally more forgiving
# question. Reusing the same 300ms for both meant the ambiguous-DatabaseError path now
# gets TWO independent chances to trip that tight a threshold instead of one (each `SET
# statement_timeout` starts a fresh per-statement clock, so this isn't one shared window
# getting split thinner — it's the SAME tight window applied twice in a row, doubling the
# exposure to any single cold-start-latency blip that legitimately -- nothing actually
# wrong -- runs past 300ms on a cold-started connection pool: a freshly booted
# memnos_server.py opening its first-ever connection to Postgres, e.g.
# test_recall_arm_degrade_http.py's dedicated subprocess, or CI's first test against a
# just-started service container). A control probe that trips on cold-start latency alone
# misfiles a healthy server as "probe infrastructure is broken" and false-positive
# degrades the arm.
#
# Sized from a real measurement, not a guess: a brand-new connection's first-ever
# statement against a freshly-started pgvector/pgvector:pg16 container (this probe's exact
# shape) totals ~32-48ms locally. This repo already has an established, deliberately
# generous "safe against a cold DB on this exact CI" figure for a related purpose —
# MEMNOS_STMT_TIMEOUT_MS=1800 in test_recall_arm_degrade_http.py's own server setup,
# chosen so a forced cancellation "is fast without flaking on a cold DB" (see that file's
# docstring). 1500ms stays UNDER that existing precedent — preserving this module's own
# stated invariant that a probe timeout is always tighter than the connection's normal
# request-scoped budget, never looser — while still clearing the measured cold-start
# latency by roughly 30x, real margin for a slower/busier shared CI runner without being
# so long that a control probe hitting it could plausibly be legitimate cold-start work
# rather than a genuinely dead/hung connection.
_FTS_CONTROL_PROBE_TIMEOUT_MS = int(os.environ.get("MEMNOS_FTS_CONTROL_PROBE_TIMEOUT_MS", "1500"))


def _fts_max_tokens() -> int:
    try:
        return max(1, int(os.environ.get("MEMNOS_FTS_MAX_TOKENS", str(_FTS_DEFAULT_TOKENS))))
    except (TypeError, ValueError):
        return _FTS_DEFAULT_TOKENS


def _fts_node_bound(cap: int) -> int:
    return _FTS_NODE_BOUND_MULTIPLIER * cap


def _tsquery_within_bound(conn, qtext: str, bound: int) -> bool:
    """Authoritative safety probe: ask Postgres directly whether
    numnode(websearch_to_tsquery('english', qtext)) <= bound, rather than modeling the
    parser's cost in Python (see the module comment above for why that was tried and
    found unsafe). Runs under its own short statement_timeout, independent of and much
    tighter than the connection's normal one, restored via `SET ... TO DEFAULT` afterward
    (reverts to the value configured when this connection was opened, e.g. via the pool's
    `-c statement_timeout=...` option — not a hardcoded system default).

    issue #49: not every DatabaseError-class probe failure is evidence that THIS QUERY is
    pathological.
      - ProgramLimitExceeded (the parser's own "value is too big in tsquery" limit),
        InternalError_ (XX000 "tsquery stack too small" -- a generic internal-error
        SQLSTATE, but the only cause this specific probe query is known to raise it for),
        and DataError (psycopg's own pre-flight rejection of qtext, e.g. an embedded NUL
        byte -- never reaches the server, so it cannot be an infra symptom) are BY
        DEFINITION about this query's text. Treated as "not measurable as safe" and
        returned as False, same as before this fix.
      - Everything else DatabaseError-class (QueryCanceled included -- ambiguous between
        "the probe legitimately ran out of its own tight budget building THIS query" and
        "something unrelated -- admin contention, another session's cancel, a dropped
        connection -- interrupted it") gets a CONTROL PROBE before a verdict is reached:
        one trivial numnode() call, on the same cursor, under its OWN more generous
        timeout (_FTS_CONTROL_PROBE_TIMEOUT_MS -- deliberately NOT this probe's own tight
        budget; see that constant's comment for why a cold-started connection needs the
        room). If the control succeeds, the server just answered a text-search query
        fine, so the original failure really is this query's fault -- return False,
        exactly as before.
        If the control ALSO fails, the probe itself is broken, not the query -- the
        ORIGINAL exception is re-raised rather than filed as either "safe" or
        "pathological" (see the RECALL_ARM_FAILURES comment above for what happens to it
        from there).
    `psycopg.InterfaceError` (client/driver misuse, not a property of qtext) is
    deliberately NOT caught here at all and propagates normally, unchanged from before.
    The connection is left usable afterward when it CAN be (autocommit means no caught
    error poisons a transaction) -- the statement_timeout restore itself is guarded so a
    dead connection there can't overwrite whichever result or exception this probe is
    actually reporting."""
    if not qtext:
        return True
    with conn.cursor() as c:
        c.execute(f"SET statement_timeout = {_FTS_NODE_CHECK_TIMEOUT_MS}")
        try:
            try:
                c.execute(
                    "SELECT (numnode(websearch_to_tsquery('english', %s)) <= %s) AS ok",
                    (qtext, bound))
                row = c.fetchone()
                return bool(row["ok"] if isinstance(row, dict) else row[0])
            except (psycopg.errors.ProgramLimitExceeded, psycopg.errors.InternalError_,
                    psycopg.DataError):
                return False
            except psycopg.DatabaseError as probe_exc:
                # INVARIANT: this control probe -- and the discrimination logic around it
                # -- requires `conn` to be autocommit. True of every current call site
                # (fts_clamp opens no transaction of its own; BrainStore's self.conn is
                # opened with autocommit=True). On a non-autocommit connection, the main
                # probe's caught error above would poison the transaction, so THIS SELECT
                # would itself raise psycopg.errors.InFailedSqlTransaction (also a DatabaseError)
                # regardless of whether the server is actually healthy -- collapsing every
                # ambiguous case into "control also failed, re-raise" and silently
                # defeating the whole point of this branch. A future non-autocommit call
                # site must rollback (or savepoint) before reaching here, or this
                # discrimination breaks without ever raising an obvious error.
                try:
                    # Deliberately NOT the main probe's 300ms budget -- see
                    # _FTS_CONTROL_PROBE_TIMEOUT_MS above for why this needs its own,
                    # more generous, cold-start-tolerant timeout.
                    c.execute(f"SET statement_timeout = {_FTS_CONTROL_PROBE_TIMEOUT_MS}")
                    c.execute("SELECT numnode(websearch_to_tsquery('english', 'x')) AS n")
                    c.fetchone()
                except psycopg.DatabaseError:
                    # the control ALSO failed -- the probe (not qtext) is what's broken.
                    raise probe_exc
                return False
        finally:
            try:
                c.execute("SET statement_timeout = DEFAULT")
            except (psycopg.DatabaseError, psycopg.InterfaceError):
                # connection's already unusable (DatabaseError) or gone (InterfaceError,
                # e.g. closed underneath us) -- nothing left to restore it for. Widened
                # from DatabaseError-only so a genuine InterfaceError propagating out of
                # the try block (client/driver misuse, meant to crash loudly per this
                # function's docstring) can never get silently clobbered by THIS cleanup
                # statement failing the same way.
                pass


def fts_clamp(qtext: str, conn) -> str:
    """Bound the complexity of the tsquery websearch_to_tsquery('english', qtext) will
    build (issue #41), WITHOUT rewriting query semantics for queries that don't need it.
    `conn` is REQUIRED: the safety check is a real, bounded Postgres probe
    (_tsquery_within_bound), not a Python estimate — see the module comment above for why
    a pure-Python model was tried and found to silently undercount real tsquery cost for
    hyphenated/kebab-case text. Every production call site is a BrainStore method with
    `self.conn` already available.

    A query at or under the token cap AND confirmed (via the probe) at or under the node
    bound is returned byte-for-byte unchanged — "-" exclusions, quoted phrases, and "OR"
    all keep their native websearch_to_tsquery meaning. A query over either bound is
    shrunk (word count first, then — only if a single remaining word is itself still over
    bound, e.g. one massively hyphen-decomposed identifier — that word's character length)
    until the probe confirms it's safe.

    issue #49: a probe-infrastructure failure (as opposed to a confirmed-pathological
    query) is NOT swallowed into a shrink attempt here — _tsquery_within_bound re-raises
    it, so it propagates straight out of this function uncaught. Every call site is a
    BrainStore search method that core/service.py invokes under the RECALL_ARM_FAILURES
    degrade-not-raise path (issue #41 fix C), so that's where it's handled."""
    cap = _fts_max_tokens()
    bound = _fts_node_bound(cap)
    parts = qtext.split() if qtext else []
    candidate = qtext if len(parts) <= cap else " ".join(parts[:cap])

    if _tsquery_within_bound(conn, candidate, bound):
        return candidate

    # candidate is confirmed (not estimated) too complex -- shrink by halving word count
    # until the probe confirms it's safe or a single word remains.
    words = candidate.split()
    while len(words) > 1:
        words = words[: max(1, len(words) // 2)]
        shrunk = " ".join(words)
        if _tsquery_within_bound(conn, shrunk, bound):
            return shrunk

    # down to a single word and it's STILL unsafe -- the pathological complexity is
    # packed into one token (e.g. a single identifier with hundreds of internal hyphens,
    # confirmed live to exceed the bound on its own). Shrink its character length the
    # same way, probe-verified at each step.
    word = words[0] if words else ""
    while len(word) > 1 and not _tsquery_within_bound(conn, word, bound):
        word = word[: max(1, len(word) // 2)]
    return word


# The #15 fix clamped only the FTS arm; the EMBEDDING and the cross-encoder RERANKER still
# saw the full query (up to MEMNOS_QUERY_MAX_CHARS=20000 chars). An 8000-word / ~40KB query
# then embedded + reranked the whole thing — ~5s of pure clamp-able overhead, even though no
# legitimate recall is thousands of words and both models cap their own input length anyway
# (a sentence-transformer cross-encoder truncates past ~512 tokens; the embedder past its own
# limit). Clamp the query that reaches the embedder + reranker to a sane token prefix: its
# discriminative signal lives in the first few hundred tokens, so normal queries (well under
# the cap) are byte-for-byte untouched and only pathological ones are bounded.
# MEMNOS_QUERY_RERANK_MAX_TOKENS tunes the cap (default 384 tokens).
def _query_max_tokens() -> int:
    try:
        return max(1, int(os.environ.get("MEMNOS_QUERY_RERANK_MAX_TOKENS", "384")))
    except (TypeError, ValueError):
        return 384


def query_clamp(qtext: str) -> str:
    """Clamp the query text fed to the embedding model and the cross-encoder reranker to the
    first N whitespace tokens. Returns the input UNCHANGED when at/under the cap, so normal
    queries embed + rerank identically to before — only pathological long queries are bounded.
    """
    if not qtext:
        return qtext
    parts = qtext.split()
    cap = _query_max_tokens()
    if len(parts) <= cap:
        return qtext
    return " ".join(parts[:cap])


# pgvector >= 0.7 ships the half-precision `halfvec` type (half the storage). pgvector 0.6
# (the version Debian/Ubuntu ship in apt) does not — only the full-precision `vector` type.
# memnos feature-detects which is available and uses halfvec when it can, vector otherwise,
# so a clean apt install of pgvector 0.6 works with no source build. The two are wire- and
# query-compatible for everything memnos does (cosine distance, HNSW); halfvec is purely a
# storage optimization. The chosen type is consistent within one database.
MIN_PGVECTOR_HALFVEC = (0, 7, 0)


def _vtuple(ver: str) -> tuple:
    parts = []
    for p in str(ver).split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def detect_vector_type(conn) -> str:
    """Return the embedding column type to use ('halfvec' or 'vector') for this database.
    Prefers the type already baked into an existing schema (so we never cast to a type the
    columns aren't); falls back to the installed pgvector version (halfvec needs >= 0.7)."""
    with conn.cursor() as c:
        # 1) If a memnos schema already exists, mirror its actual column type — authoritative.
        c.execute(
            "SELECT t.typname FROM pg_attribute a "
            "JOIN pg_class cl ON cl.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = cl.relnamespace "
            "JOIN pg_type t ON t.oid = a.atttypid "
            "WHERE n.nspname LIKE 'tenant_%' AND cl.relname = 'raw_turns' "
            "AND a.attname = 'embedding' LIMIT 1")
        row = c.fetchone()
        if row:
            name = row["typname"] if isinstance(row, dict) else row[0]
            if name in ("halfvec", "vector"):
                return name
        # 2) No schema yet — pick by installed pgvector version.
        c.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        r = c.fetchone()
        if r:
            ver = r["extversion"] if isinstance(r, dict) else r[0]
            if _vtuple(ver) >= MIN_PGVECTOR_HALFVEC:
                return "halfvec"
        return "vector"


# module-level, shared by every BrainStore instance in this process (issue #41 follow-up):
# once a namespace's registry row is known to exist, insert_raw_turn skips the upsert for
# it on every later call, for the rest of this process's life -- a 50-turn ingest_session
# no longer issues 50 redundant no-op INSERTs. Two threads racing a first write to the same
# new namespace can both miss the cache and both issue the upsert once; harmless (ON
# CONFLICT DO NOTHING backstops correctness), so no lock is needed here.
_known_registered_namespaces: set[str] = set()


class BrainStore:
    def __init__(self, dsn: str | None = None, conn=None):
        # Accept a pooled connection (production) or open one from a DSN (scripts/tests).
        if conn is not None:
            self.conn = conn
            self._owns = False
        else:
            self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
            self._owns = True
        self._vtype = None

    @property
    def vtype(self) -> str:
        """Embedding column/cast type for this DB: 'halfvec' (pgvector>=0.7) or 'vector' (0.6).
        Detected once from the live database and cached. Value is from a fixed safe set, so it
        is safe to interpolate into SQL."""
        if self._vtype is None:
            self._vtype = detect_vector_type(self.conn)
        return self._vtype

    @property
    def vops(self) -> str:
        """HNSW cosine ops class matching the vector type."""
        return "halfvec_cosine_ops" if self.vtype == "halfvec" else "vector_cosine_ops"

    def _chk(self, s: str) -> None:
        if not _IDENT.match(s):
            raise ValueError(f"unsafe schema identifier: {s!r}")

    # --- provisioning -----------------------------------------------------
    def create_schema(self, tenant: str, dim: int = 1536) -> str:
        # (Re)load the schema DDL function from schema.sql first, so additive schema
        # changes (e.g. new columns via ALTER ... ADD COLUMN IF NOT EXISTS) deploy on every
        # boot — then materialise/upgrade the tenant schema. Rolling, additive-only.
        import os
        sql_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        with open(sql_path) as fh:
            ddl = fh.read()
        vtype, vops = self.vtype, self.vops
        with self.conn.cursor() as c:
            c.execute(ddl)                                   # CREATE OR REPLACE FUNCTION (idempotent)
            c.execute("SELECT create_brain_schema(%s, %s, %s, %s)", (tenant, dim, vtype, vops))
        return f"tenant_{tenant}"

    def drop_schema(self, tenant: str) -> None:
        s = f"tenant_{tenant}"; self._chk(s)
        with self.conn.cursor() as c:
            c.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")

    # issue #59: tables carrying an HNSW vector index — the ones warm_indexes() below
    # (and the boot-time call in memnos_server.py's serve()) actually probe. Kept as one
    # named tuple so the boot probe and every future caller stay in sync automatically.
    _HNSW_TABLES = ("raw_turns", "semantic", "episodic")

    def warm_indexes(self, schema: str, dim: int) -> None:
        """Force each HNSW vector index into cache with one trivial real ANN query per
        table (issue #59/#31). `SELECT 1` (what /readyz already did) proves Postgres is
        reachable but proves nothing about whether the FIRST real recall after a restart
        pays a cold page-in cost against these indexes' on-disk graphs — confirmed live
        (EXPLAIN) to be exactly what a plain `SELECT 1`-only readiness check misses, and
        exactly the failure mode issue #31 reports as a burst of statement_timeout
        cancellations right after a restart at 143K-fact scale. A `LIMIT 1` ANN probe
        against each table is enough to pull its HNSW graph into shared_buffers/OS
        cache; an empty table (fresh install, nothing to page in yet) is a harmless
        no-op that still exercises the index's query path. Uses a fixed, nonzero unit
        vector (not all-zero — cosine distance from the zero vector is a degenerate
        edge case on some pgvector builds; verified live that a genuine unit vector has
        no such ambiguity) so results are deterministic and never depend on real data.
        Runs on `self.conn` under whatever statement_timeout the caller already set —
        callers doing this at boot (before the server accepts any traffic) should widen
        it first the same way create_schema's DDL does, since a fresh restart at scale
        is exactly the case this needs room to actually finish in."""
        self._chk(schema)
        qv = vlit([1.0] + [0.0] * (dim - 1))
        with self.conn.cursor() as c:
            for table in self._HNSW_TABLES:
                c.execute(f"SELECT id FROM {schema}.{table} "
                          f"ORDER BY embedding <=> %s::{self.vtype} LIMIT 1", (qv,))
                c.fetchone()

    # --- sensory / verbatim ----------------------------------------------
    def insert_raw_turn(self, schema, ns, session_id, speaker, text, observed_at, vec,
                        author=None, memory_type=None) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.raw_turns(namespace,session_id,speaker,text,observed_at,embedding,author_principal,memory_type) "
                f"VALUES(%s,%s,%s,%s,%s,%s::{self.vtype},%s,%s) RETURNING id",
                (ns, session_id, speaker, text, observed_at,
                 vlit(vec) if vec is not None else None, author, memory_type))
            tid = c.fetchone()["id"]
            # issue #41 fix A: the single choke point every new namespace's first write
            # passes through (remember/remember_turn/ingest_session all land here) — upsert
            # it into the control-plane registry so readable_namespaces() can resolve
            # wildcard grants against that small table instead of DISTINCT-scanning
            # raw_turns/semantic on every wide recall. ON CONFLICT DO NOTHING: never
            # downgrades a namespace an admin already explicitly registered (create_namespace
            # sets auto_registered=false; this only fires for a name not already present).
            # Connections run autocommit (memnos_server.py POOL / connect() below), so this
            # isn't atomic with the raw_turn insert above -- a crash between the two is
            # harmless and self-healing: the next write to this namespace, or the boot-time
            # backfill in Control._run_namespace_registry_backfill, fills the gap. Nothing
            # downstream treats "missing from the registry" as more than "not yet
            # observed", so it never needs to be atomic -- which is also why a failure here
            # must never fail the write: the raw_turn row above is ALREADY DURABLY COMMITTED
            # (autocommit) by the time this statement runs, so letting an exception from it
            # propagate would report a successful write as a failure, inviting a retrying
            # caller to double-insert the turn. Skipped entirely once this process has seen
            # this namespace registered before (module-level cache below) -- ON CONFLICT DO
            # NOTHING makes the upsert a no-op after the first successful write anyway, so a
            # 50-turn ingest_session no longer pays 50 redundant round trips for it.
            if ns not in _known_registered_namespaces:
                try:
                    c.execute(
                        "INSERT INTO memnos_control.namespaces(name, auto_registered) VALUES(%s, true) "
                        "ON CONFLICT (name) DO NOTHING", (ns,))
                    _known_registered_namespaces.add(ns)
                except Exception:
                    logger.warning("namespace registry upsert failed for %r; raw_turn %s "
                                    "already committed, continuing", ns, tid, exc_info=True)
            return tid

    # --- episodic ---------------------------------------------------------
    def insert_episodic(self, schema, ns, session_id, text, *, summary=None,
                        t_start=None, t_end=None, observed_at=None, salience=0.0,
                        source_turn_ids: Iterable[int] = (), vec=None, author=None,
                        memory_type=None) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.episodic"
                f"(namespace,session_id,text,summary,t_start,t_end,observed_at,salience,source_turn_ids,embedding,author_principal,memory_type) "
                f"VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::{self.vtype},%s,%s) RETURNING id",
                (ns, session_id, text, summary, t_start, t_end, observed_at, salience,
                 list(source_turn_ids), vlit(vec) if vec is not None else None, author,
                 memory_type))
            return c.fetchone()["id"]

    def uncovered_raw_turns(self, schema, ns) -> list[dict]:
        """Raw turns not yet assigned to any episode (for incremental segmentation)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"""
                SELECT id, speaker, text, observed_at, session_id, memory_type
                FROM {schema}.raw_turns
                WHERE namespace=%s AND id NOT IN (
                    SELECT unnest(source_turn_ids) FROM {schema}.episodic
                    WHERE namespace=%s AND source_turn_ids IS NOT NULL)
                ORDER BY observed_at, id""", (ns, ns))
            return c.fetchall()

    def link_episode_provenance(self, schema, ns, episode_id, source_turn_ids) -> int:
        """Two-level provenance: link semantic facts whose source turns overlap this episode
        (fact → episode → turn), populating the provenance table as the schema intends."""
        self._chk(schema)
        if not source_turn_ids:
            return 0
        with self.conn.cursor() as c:
            c.execute(f"""
                INSERT INTO {schema}.provenance(semantic_id, episodic_id)
                SELECT s.id, %s FROM {schema}.semantic s
                WHERE s.namespace=%s AND s.source_turn_ids && %s::bigint[]
                ON CONFLICT DO NOTHING""", (episode_id, ns, list(source_turn_ids)))
            return c.rowcount

    def get_episode(self, schema, ns, episode_id) -> dict | None:
        """An episode + its verbatim turns + the facts derived from it (via provenance)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, session_id, summary, text, t_start, t_end, salience, "
                      f"access_count, source_turn_ids, memory_type FROM {schema}.episodic WHERE id=%s AND namespace=%s",
                      (episode_id, ns))
            ep = c.fetchone()
            if not ep:
                return None
            sids = ep.get("source_turn_ids") or []
            turns = []
            if sids:
                c.execute(f"SELECT id, speaker, text AS content, observed_at FROM {schema}.raw_turns "
                          f"WHERE id = ANY(%s) AND namespace=%s ORDER BY id", (list(sids), ns))
                turns = c.fetchall()
            c.execute(f"SELECT s.id, s.statement FROM {schema}.provenance p "
                      f"JOIN {schema}.semantic s ON s.id=p.semantic_id "
                      f"WHERE p.episodic_id=%s AND s.expired_at IS NULL", (episode_id,))
            facts = c.fetchall()
        return {"episode": ep, "turns": turns, "facts": facts}

    def touch_episodes(self, schema, episode_ids) -> None:
        """Record access (recency/frequency signal for decay)."""
        self._chk(schema)
        if not episode_ids:
            return
        with self.conn.cursor() as c:
            c.execute(f"UPDATE {schema}.episodic SET last_access=now(), access_count=access_count+1 "
                      f"WHERE id = ANY(%s)", (list(episode_ids),))

    def decay_episodes(self, schema, ns, *, half_life_days=30) -> int:
        """DECAY pass: recompute episodic salience as time-weighted recency (half-life) plus
        an access-frequency boost. Recent/often-recalled episodes stay salient; old untouched
        ones fade. Semantic facts are untouched (they persist). Returns # episodes updated."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"""
                UPDATE {schema}.episodic SET salience = LEAST(1.0,
                    exp(-ln(2.0) * (EXTRACT(EPOCH FROM (now() - COALESCE(last_access, observed_at)))
                        / 86400.0) / %s)
                    + 0.05 * LEAST(access_count, 10))
                WHERE namespace=%s RETURNING id""", (float(half_life_days), ns))
            return len(c.fetchall())

    # --- associative graph ------------------------------------------------
    def upsert_entity(self, schema, ns, name, vec=None) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.entities(namespace,name,embedding) "
                f"VALUES(%s,%s,%s::{self.vtype}) ON CONFLICT (namespace,name) DO UPDATE SET name=EXCLUDED.name "
                f"RETURNING id",
                (ns, name, vlit(vec) if vec is not None else None))
            return c.fetchone()["id"]

    def add_mention(self, schema, entity_id, memory_id, memory_kind) -> None:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.mentions(entity_id,memory_id,memory_kind) "
                f"VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (entity_id, memory_id, memory_kind))

    def bump_edge(self, schema, ns, src, dst, w=1.0) -> None:
        """Co-mention edge between two entities; weight accumulates (Hebbian-ish)."""
        self._chk(schema)
        if src == dst:
            return
        a, b = sorted((src, dst))
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.edges(namespace,src_entity,dst_entity,weight) "
                f"VALUES(%s,%s,%s,%s) ON CONFLICT (namespace,src_entity,dst_entity) "
                f"DO UPDATE SET weight = {schema}.edges.weight + EXCLUDED.weight",
                (ns, a, b, w))

    # --- semantic + provenance (used by B2 consolidation) -----------------
    def insert_semantic(self, schema, ns, kind, statement, *, subject=None, predicate=None,
                        obj=None, valid_from=None, valid_to=None, confidence=1.0,
                        salience=0.0, vec=None, source_turn_ids: Iterable[int] = (),
                        author=None, memory_type=None, observed_at=None,
                        inference_confidence=None, inference_basis=None,
                        source_fact_ids: Iterable[int] = ()) -> int:
        # observed_at = the OBSERVATION (knowledge) axis used by bi-temporal supersession:
        # when this fact was learned (server: now; session ingest: session date). None →
        # column default now() (legacy callers unchanged).
        self._chk(schema)
        src = list(source_turn_ids) if source_turn_ids else None
        fact_src = list(source_fact_ids) if source_fact_ids else None
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.semantic"
                f"(namespace,kind,statement,subject_entity,predicate,object,valid_from,valid_to,confidence,salience,embedding,source_turn_ids,author_principal,memory_type,observed_at,inference_confidence,inference_basis,source_fact_ids) "
                f"VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::{self.vtype},%s,%s,%s,COALESCE(%s,now()),%s,%s,%s) RETURNING id",
                (ns, kind, statement, subject, predicate, obj, valid_from, valid_to,
                 confidence, salience, vlit(vec) if vec is not None else None, src, author,
                 memory_type, observed_at, inference_confidence, inference_basis, fact_src))
            return c.fetchone()["id"]

    def provenance_of(self, schema, ns, semantic_id) -> dict | None:
        """Evidence chain for a fact: the fact + the verbatim raw_turn(s) it was extracted
        from (or, for a dossier, the turns its source facts derived from)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, kind, statement, valid_from, valid_to, source_turn_ids "
                      f"FROM {schema}.semantic WHERE id=%s AND namespace=%s", (semantic_id, ns))
            fact = c.fetchone()
            if not fact:
                return None
            srcs = fact.get("source_turn_ids") or []
            sources = []
            if srcs:
                c.execute(f"SELECT id, speaker, text AS content, observed_at "
                          f"FROM {schema}.raw_turns WHERE id = ANY(%s) AND namespace=%s ORDER BY id",
                          (list(srcs), ns))
                sources = c.fetchall()
        return {"fact": {"id": fact["id"], "kind": fact["kind"], "statement": fact["statement"],
                         "valid_from": fact["valid_from"], "valid_to": fact["valid_to"]},
                "source_turn_ids": srcs, "sources": sources}

    def add_provenance(self, schema, semantic_id, episodic_ids: Iterable[int]) -> None:
        self._chk(schema)
        with self.conn.cursor() as c:
            for eid in episodic_ids:
                c.execute(f"INSERT INTO {schema}.provenance(semantic_id,episodic_id) "
                          f"VALUES(%s,%s) ON CONFLICT DO NOTHING", (semantic_id, eid))

    # --- reads for consolidation (B2) -------------------------------------
    def fetch_episodes(self, schema, ns, only_unconsolidated=True) -> list[dict]:
        self._chk(schema)
        where = "AND consolidated = false" if only_unconsolidated else ""
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, session_id, text, t_start, t_end, salience "
                      f"FROM {schema}.episodic WHERE namespace=%s {where} ORDER BY id", (ns,))
            return c.fetchall()

    def entity_episodes(self, schema, ns, min_episodes=2) -> list[dict]:
        """Entities and the episodic events that mention them (the cluster for a dossier)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT e.name, array_agg(DISTINCT m.memory_id) AS ep_ids "
                f"FROM {schema}.entities e JOIN {schema}.mentions m ON m.entity_id = e.id "
                f"WHERE e.namespace=%s AND m.memory_kind='episodic' "
                f"GROUP BY e.name HAVING count(DISTINCT m.memory_id) >= %s "
                f"ORDER BY count(DISTINCT m.memory_id) DESC", (ns, min_episodes))
            return c.fetchall()

    def mark_consolidated(self, schema, episodic_ids) -> None:
        self._chk(schema)
        if not episodic_ids:
            return
        with self.conn.cursor() as c:
            c.execute(f"UPDATE {schema}.episodic SET consolidated=true WHERE id = ANY(%s)",
                      (list(episodic_ids),))

    def supersede_similar(self, schema, ns, new_vec, subject, valid_from, thresh=0.12) -> int:
        """Dedup-style: close out near-IDENTICAL currently-valid facts (distance < thresh)."""
        self._chk(schema)
        if not subject:
            return 0
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic SET valid_to=%s "
                f"WHERE namespace=%s AND subject_entity=%s AND valid_to IS NULL AND expired_at IS NULL "
                f"AND (embedding <=> %s::{self.vtype}) < %s RETURNING id",
                (valid_from, ns, subject, vlit(new_vec), thresh))
            return len(c.fetchall())

    def supersede_inferred(self, schema, ns, subject, valid_from) -> int:
        """Close out prior INFERRED conclusions for `subject` before writing fresh ones —
        inferred memories are superseded (not accumulated) on every re-consolidation pass
        that recomputes them (issue #24)."""
        self._chk(schema)
        if not subject:
            return 0
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic SET valid_to=%s "
                f"WHERE namespace=%s AND subject_entity=%s AND memory_type='inferred' "
                f"AND valid_to IS NULL AND expired_at IS NULL RETURNING id",
                (valid_from, ns, subject))
            return len(c.fetchall())

    def supersede_subject(self, schema, ns, subject, new_vec, valid_from,
                          dist_lo=0.05, dist_hi=0.50) -> int:
        """BELIEF-CHANGE supersession (the 'never serve stale as current' guarantee):
        when a new fact about `subject` arrives, close out the PRIOR currently-valid facts
        about that same subject that are TOPICALLY similar but not identical (cosine
        distance in [dist_lo, dist_hi]) and started earlier — e.g. 'lives in Austin' is
        superseded by 'lives in Seattle'. Sets valid_to (valid time);
        never deletes. Returns # superseded."""
        self._chk(schema)
        if not subject or valid_from is None:
            return 0
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic SET valid_to=%(vf)s "
                f"WHERE namespace=%(ns)s AND subject_entity=%(sub)s AND valid_to IS NULL "
                f"AND expired_at IS NULL AND (valid_from IS NULL OR valid_from < %(vf)s) "
                f"AND (embedding <=> %(v)s::{self.vtype}) BETWEEN %(lo)s AND %(hi)s RETURNING id",
                {"vf": valid_from, "ns": ns, "sub": subject, "v": vlit(new_vec),
                 "lo": dist_lo, "hi": dist_hi})
            return len(c.fetchall())

    def supersede_predicate(self, schema, ns, subject, predicate, obj, valid_from,
                            observed_at=None, historical=False, event_date=None) -> list[int]:
        """ROBUST belief-change supersession: when a new (subject, predicate, object)
        fact arrives, close out the currently-valid facts with the SAME subject+predicate
        but a DIFFERENT object (e.g. lives-in Austin → lives-in Seattle). Sets valid_to;
        never deletes. This is what makes 'what is X's CURRENT y?' trustworthy.

        BI-TEMPORAL guard (see service._write_fact): belief change is keyed on the
        OBSERVATION axis — the old fact must have been observed no later than the new
        one (a fact learned later is newer knowledge even when its EVENT date backdates,
        e.g. "moved last week"). Only when the new statement is flagged `historical`
        (past-state wording) do we additionally require event order: the old fact's
        valid_from must be <= `event_date` (the EXPLICIT in-statement date; the caller
        skips the call entirely when a historical statement has none) — a backdated
        historical statement must not displace the current value. valid_to = the new
        fact's event date, clamped to never precede the closed fact's own valid_from.
        Returns the superseded ids (callers stamp superseded_by on them)."""
        self._chk(schema)
        if not subject or not predicate:
            return []
        obs = observed_at if observed_at is not None else valid_from
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic "
                f"SET valid_to=GREATEST(coalesce(valid_from, %(vt)s), %(vt)s) "
                f"WHERE namespace=%(ns)s AND valid_to IS NULL AND expired_at IS NULL "
                f"AND lower(subject_entity)=lower(%(sub)s) AND lower(predicate)=lower(%(pred)s) "
                f"AND lower(coalesce(object,'')) <> lower(coalesce(%(obj)s,'')) "
                f"AND (%(obs)s::timestamptz IS NULL OR observed_at <= %(obs)s) "
                f"AND (NOT %(hist)s OR valid_from IS NULL "
                f"     OR (%(ev)s::timestamptz IS NOT NULL AND valid_from <= %(ev)s)) "
                f"RETURNING id",
                {"vt": valid_from, "ns": ns, "sub": subject, "pred": predicate, "obj": obj,
                 "obs": obs, "hist": bool(historical), "ev": event_date})
            return [r["id"] for r in c.fetchall()]

    def dominant_live_fact(self, schema, ns, subject, predicate, obj, observed_at,
                           historical=False, event_date=None) -> dict | None:
        """issue #60 — the MIRROR of supersede_predicate, read-only: is the fact about to
        be inserted already dominated by an EXISTING live fact for the same
        subject+predicate (different object) that was observed STRICTLY LATER?

        supersede_predicate alone only closes existing facts observed no later than the
        incoming one — it has no opinion on the reverse case. Under out-of-order commits
        (concurrent /remember calls, or replayed write-behind writes landing on different
        MEMNOS_INGEST_WORKERS threads) a later-observed fact can commit BEFORE an
        earlier-observed one, and without this check the earlier-observed fact would land
        live right alongside it — two simultaneously "current" contradictory facts, no
        automatic reconciliation. When this returns a row, _write_fact must insert the
        new fact already closed (see store.close_out) instead of live.

        Ties (`observed_at` exactly equal) intentionally resolve in the ARRIVING fact's
        favor — the mirror image of supersede_predicate's own `<=` — so the two checks
        can never BOTH fire for the same pair. Same historical/event-date guard as
        supersede_predicate, so a backdated historical statement neither closes nor is
        closed by the current value.

        Caller MUST hold the (namespace, subject, predicate) advisory lock (_write_fact)
        for the whole supersede+insert critical section — on its own this is just a
        point-in-time read and does not close the race."""
        self._chk(schema)
        if not subject or not predicate:
            return None
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT id, valid_from FROM {schema}.semantic "
                f"WHERE namespace=%(ns)s AND valid_to IS NULL AND expired_at IS NULL "
                f"AND lower(subject_entity)=lower(%(sub)s) AND lower(predicate)=lower(%(pred)s) "
                f"AND lower(coalesce(object,'')) <> lower(coalesce(%(obj)s,'')) "
                f"AND observed_at > %(obs)s "
                f"AND (NOT %(hist)s OR valid_from IS NULL "
                f"     OR (%(ev)s::timestamptz IS NOT NULL AND valid_from <= %(ev)s)) "
                f"ORDER BY observed_at DESC LIMIT 1",
                {"ns": ns, "sub": subject, "pred": predicate, "obj": obj,
                 "obs": observed_at, "hist": bool(historical), "ev": event_date})
            return c.fetchone()

    def mark_superseded_by(self, schema, ids, new_id) -> None:
        """Stamp the supersession LINK (additive column): which fact replaced these."""
        self._chk(schema)
        if not ids:
            return
        with self.conn.cursor() as c:
            c.execute(f"UPDATE {schema}.semantic SET superseded_by=%s WHERE id = ANY(%s)",
                      (new_id, list(ids)))

    def nearest_live_facts(self, schema, ns, vec, *, k=8, exclude_id=None,
                           observed_before=None) -> list[dict]:
        """Top-k semantically nearest LIVE extracted facts (HNSW) in a namespace — the
        candidate set for the reversal/negation close-out. kind='fact' only (dossiers/
        constraints are consolidation-owned), optional knowledge-axis cutoff."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT id, statement, subject_entity, (embedding <=> %(v)s::{self.vtype}) AS dist "
                f"FROM {schema}.semantic "
                f"WHERE namespace=%(ns)s AND kind='fact' AND valid_to IS NULL "
                f"AND expired_at IS NULL AND embedding IS NOT NULL "
                f"AND (%(ex)s::bigint IS NULL OR id <> %(ex)s) "
                f"AND (%(obs)s::timestamptz IS NULL OR observed_at < %(obs)s) "
                f"ORDER BY embedding <=> %(v)s::{self.vtype} LIMIT %(k)s",
                {"v": vlit(vec), "ns": ns, "ex": exclude_id, "obs": observed_before, "k": k})
            return c.fetchall()

    def close_out(self, schema, ns, fact_id, *, valid_to, superseded_by=None) -> int:
        """Close ONE live fact (belief change): set valid_to (clamped to its own
        valid_from) + the superseded_by link. Never deletes. Returns 0/1."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic "
                f"SET valid_to=GREATEST(coalesce(valid_from, %(vt)s), %(vt)s), superseded_by=%(by)s "
                f"WHERE id=%(id)s AND namespace=%(ns)s AND valid_to IS NULL AND expired_at IS NULL "
                f"RETURNING id",
                {"vt": valid_to, "by": superseded_by, "id": fact_id, "ns": ns})
            return len(c.fetchall())

    def find_near_duplicate(self, schema, ns, vec, subject, thresh) -> dict | None:
        """Nearest LIVE extracted fact within `thresh` cosine distance (write-path dedupe).
        Subject agreement is required only when BOTH sides carry a subject."""
        self._chk(schema)
        if vec is None or thresh <= 0:
            return None
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT id, statement, (embedding <=> %(v)s::{self.vtype}) AS dist "
                f"FROM {schema}.semantic "
                f"WHERE namespace=%(ns)s AND kind='fact' AND valid_to IS NULL "
                f"AND expired_at IS NULL AND embedding IS NOT NULL "
                f"AND (%(sub)s::text IS NULL OR subject_entity IS NULL "
                f"     OR lower(subject_entity)=lower(%(sub)s)) "
                f"AND (embedding <=> %(v)s::{self.vtype}) < %(t)s "
                f"ORDER BY embedding <=> %(v)s::{self.vtype} LIMIT 1",
                {"v": vlit(vec), "ns": ns, "sub": subject, "t": thresh})
            return c.fetchone()

    def near_duplicate_pairs(self, schema, ids, thresh) -> list[tuple]:
        """RECALL-PATH dedupe (issue #2): among the GIVEN candidate raw-turn ids, return
        (a, b) pairs whose embeddings are within `thresh` cosine distance (a<b). A single
        self-join over the small candidate set (k<=~80) — NOT a namespace scan — so it is
        cheap and bounded. Reuses the write-path dedupe threshold (MEMNOS_DEDUPE_THRESHOLD,
        0.03). The caller collapses the resulting groups, keeping one survivor."""
        self._chk(schema)
        ids = [i for i in (ids or ()) if i is not None]
        if len(ids) < 2 or thresh <= 0:
            return []
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT a.id AS a, b.id AS b "
                f"FROM {schema}.raw_turns a JOIN {schema}.raw_turns b "
                f"  ON a.id < b.id "
                f"WHERE a.id = ANY(%(ids)s) AND b.id = ANY(%(ids)s) "
                f"  AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL "
                f"  AND (a.embedding <=> b.embedding) < %(t)s",
                {"ids": ids, "t": thresh})
            return [(r["a"], r["b"]) for r in c.fetchall()]

    def bump_restatement(self, schema, fact_id, source_turn_ids=()) -> None:
        """Reinforce an existing live fact instead of inserting a near-duplicate:
        restatements counter + salience bump + provenance union (additive columns)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic SET restatements = restatements + 1, "
                f"salience = LEAST(1.0, salience + 0.1), "
                f"source_turn_ids = (SELECT ARRAY(SELECT DISTINCT t FROM "
                f"unnest(coalesce(source_turn_ids,'{{}}'::bigint[]) || %s::bigint[]) AS t ORDER BY t)) "
                f"WHERE id=%s", (list(source_turn_ids or ()), fact_id))

    # --- namespace reconcile (issue #10 residual C: pre-fix contradiction debt) ------
    def live_facts_newest_first(self, schema, ns, limit=None) -> list[dict]:
        """The namespace's LIVE extracted facts, newest-first on the observation axis —
        the walk order for `memnos namespace reconcile` (newer knowledge closes older)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT id, statement, subject_entity, predicate, object, valid_from, "
                f"observed_at, source_turn_ids FROM {schema}.semantic "
                f"WHERE namespace=%(ns)s AND kind='fact' AND valid_to IS NULL "
                f"AND expired_at IS NULL "
                f"ORDER BY observed_at DESC NULLS LAST, id DESC "
                f"LIMIT %(lim)s", {"ns": ns, "lim": limit})
            return c.fetchall()

    def is_live(self, schema, ns, fact_id) -> bool:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT 1 FROM {schema}.semantic WHERE id=%s AND namespace=%s "
                      f"AND valid_to IS NULL AND expired_at IS NULL", (fact_id, ns))
            return c.fetchone() is not None

    def older_near_duplicate(self, schema, ns, fact_id, thresh) -> dict | None:
        """Reconcile twin of find_near_duplicate, against STORED embeddings: the nearest
        OLDER live fact within `thresh` cosine distance of fact `fact_id` (same subject
        agreement rule: required only when both sides carry one). No new embeddings."""
        self._chk(schema)
        if thresh <= 0:
            return None
        with self.conn.cursor() as c:
            c.execute(
                f"WITH a AS (SELECT id, embedding, subject_entity, observed_at "
                f"           FROM {schema}.semantic WHERE id=%(id)s AND namespace=%(ns)s) "
                f"SELECT s.id, s.statement, (s.embedding <=> a.embedding) AS dist "
                f"FROM {schema}.semantic s, a "
                f"WHERE s.namespace=%(ns)s AND s.kind='fact' AND s.valid_to IS NULL "
                f"AND s.expired_at IS NULL AND s.embedding IS NOT NULL "
                f"AND (coalesce(s.observed_at,'epoch'), s.id) < (coalesce(a.observed_at,'epoch'), a.id) "
                f"AND (a.subject_entity IS NULL OR s.subject_entity IS NULL "
                f"     OR lower(s.subject_entity)=lower(a.subject_entity)) "
                f"AND (s.embedding <=> a.embedding) < %(t)s "
                f"ORDER BY s.embedding <=> a.embedding LIMIT 1",
                {"id": fact_id, "ns": ns, "t": thresh})
            return c.fetchone()

    def nearest_live_facts_to(self, schema, ns, fact_id, *, k=8) -> list[dict]:
        """Reconcile twin of nearest_live_facts, against STORED embeddings: top-k live
        facts nearest to fact `fact_id`, restricted to the SAME observation cutoff the
        write path uses (observed no later than the anchor), excluding the anchor."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"WITH a AS (SELECT id, embedding, observed_at "
                f"           FROM {schema}.semantic WHERE id=%(id)s AND namespace=%(ns)s) "
                f"SELECT s.id, s.statement, s.subject_entity, "
                f"       (s.embedding <=> a.embedding) AS dist "
                f"FROM {schema}.semantic s, a "
                f"WHERE s.namespace=%(ns)s AND s.kind='fact' AND s.valid_to IS NULL "
                f"AND s.expired_at IS NULL AND s.embedding IS NOT NULL AND s.id <> a.id "
                f"AND (a.observed_at IS NULL OR s.observed_at <= a.observed_at) "
                f"ORDER BY s.embedding <=> a.embedding LIMIT %(k)s",
                {"id": fact_id, "ns": ns, "k": k})
            return c.fetchall()

    def turn_supersession(self, schema, turn_ids) -> dict:
        """STALE-TURN lookup for recall (issue #10 residual B): for the RETRIEVED turn
        ids only (one batched query — O(retrieved), never O(namespace); GIN index on
        semantic.source_turn_ids), return {turn_id: close_date} for turns whose derived
        semantic facts exist AND are ALL superseded (valid_to set or superseded_by set).
        Turns with no derived facts, or with at least one still-live fact, are absent —
        they stay untouched in ranking/rendering. close_date = the latest valid_to of
        the closed facts (None only in the superseded_by-without-valid_to edge case)."""
        self._chk(schema)
        ids = [int(t) for t in turn_ids if t is not None]
        if not ids:
            return {}
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT t.tid AS turn_id, max(s.valid_to) AS closed_at "
                f"FROM unnest(%(ids)s::bigint[]) AS t(tid) "
                f"JOIN {schema}.semantic s ON s.source_turn_ids @> ARRAY[t.tid] "
                f"  AND s.kind='fact' AND s.expired_at IS NULL "
                f"GROUP BY t.tid "
                f"HAVING bool_and(s.valid_to IS NOT NULL OR s.superseded_by IS NOT NULL)",
                {"ids": ids})
            return {r["turn_id"]: r["closed_at"] for r in c.fetchall()}

    def expire(self, schema, ns, semantic_id) -> None:
        """System-time invalidation (CORRECTION, not belief change): mark a fact as
        system-removed (expired_at). Excluded from all retrieval; history preserved."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"UPDATE {schema}.semantic SET expired_at=now() "
                      f"WHERE id=%s AND namespace=%s", (semantic_id, ns))

    # --- dual hybrid search (B3 retrieval) --------------------------------
    def max_observed_at(self, schema, ns):
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT max(observed_at) AS m FROM {schema}.episodic WHERE namespace=%s", (ns,))
            return c.fetchone()["m"]

    def search_episodic(self, schema, ns, qvec, qtext, k=40) -> list[dict]:
        """Hybrid RRF (vector+FTS) over EPISODIC; returns observed_at for recency."""
        self._chk(schema)
        sql = f"""
        WITH vec AS (SELECT id, text, observed_at, memory_type, row_number() OVER (ORDER BY embedding <=> %(qv)s::{self.vtype}, id) rnk
                     FROM {schema}.episodic WHERE namespace=%(ns)s ORDER BY embedding <=> %(qv)s::{self.vtype}, id LIMIT %(k)s),
        fts AS (SELECT id, text, observed_at, memory_type, row_number() OVER (ORDER BY ts_rank(fts,q) DESC, id) rnk
                FROM {schema}.episodic, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND fts @@ q ORDER BY ts_rank(fts,q) DESC, id LIMIT %(k)s),
        fused AS (SELECT id, text, observed_at, memory_type, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,text,observed_at,memory_type,rnk FROM vec UNION ALL SELECT id,text,observed_at,memory_type,rnk FROM fts) r
                  GROUP BY id,text,observed_at,memory_type)
        SELECT id, text AS content, observed_at, memory_type, score FROM fused ORDER BY score DESC, id LIMIT %(k)s;"""
        qt = fts_clamp(qtext, self.conn)
        with self.conn.cursor() as c:
            c.execute(sql, {"qv": vlit(qvec), "qt": qt, "ns": ns, "k": k})
            return c.fetchall()

    def search_raw_turns(self, schema, ns, qvec, qtext, k=40) -> list[dict]:
        """Hybrid RRF (vector+FTS) over RAW TURNS — the strong open/single-hop layer."""
        self._chk(schema)
        sql = f"""
        WITH vec AS (SELECT id, text, observed_at, author_principal, memory_type, row_number() OVER (ORDER BY embedding <=> %(qv)s::{self.vtype}, id) rnk
                     FROM {schema}.raw_turns WHERE namespace=%(ns)s ORDER BY embedding <=> %(qv)s::{self.vtype}, id LIMIT %(k)s),
        fts AS (SELECT id, text, observed_at, author_principal, memory_type, row_number() OVER (ORDER BY ts_rank(fts,q) DESC, id) rnk
                FROM {schema}.raw_turns, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND fts @@ q ORDER BY ts_rank(fts,q) DESC, id LIMIT %(k)s),
        fused AS (SELECT id, text, observed_at, author_principal, memory_type, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,text,observed_at,author_principal,memory_type,rnk FROM vec UNION ALL SELECT id,text,observed_at,author_principal,memory_type,rnk FROM fts) r
                  GROUP BY id,text,observed_at,author_principal,memory_type)
        SELECT id, text AS content, observed_at, author_principal AS author, memory_type, score FROM fused ORDER BY score DESC, id LIMIT %(k)s;"""
        qt = fts_clamp(qtext, self.conn)
        with self.conn.cursor() as c:
            c.execute(sql, {"qv": vlit(qvec), "qt": qt, "ns": ns, "k": k})
            return c.fetchall()

    def search_semantic(self, schema, ns, qvec, qtext, k=40, current_only=False) -> list[dict]:
        """Hybrid RRF (vector+FTS) over SEMANTIC; current_only filters superseded facts.
        Returns restatements + salience too — rank-time reinforcement signals for the
        fact arm (issue #11). ADDITIVE columns only; fetch semantics unchanged.

        issue #41 fix C: a statement_timeout cancellation on this query (or any other
        RECALL_ARM_FAILURES-class error) no longer propagates straight to the caller —
        core/service.py's recall_fetch/recall_wide_fetch catch it at the call site and
        degrade that arm to a partial result with degraded=true instead of failing the
        whole recall. This method itself is unchanged; it still raises on failure, same
        as every other store method — degrading is the CALLER's decision, made once, at
        the point the arms are orchestrated."""
        self._chk(schema)
        valid = "AND valid_to IS NULL" if current_only else ""
        sql = f"""
        WITH vec AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, inference_confidence, inference_basis, row_number() OVER (ORDER BY embedding <=> %(qv)s::{self.vtype}, id) rnk
                     FROM {schema}.semantic WHERE namespace=%(ns)s AND expired_at IS NULL {valid} ORDER BY embedding <=> %(qv)s::{self.vtype}, id LIMIT %(k)s),
        fts AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, inference_confidence, inference_basis, row_number() OVER (ORDER BY ts_rank(fts,q) DESC, id) rnk
                FROM {schema}.semantic, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND expired_at IS NULL {valid} AND fts @@ q ORDER BY ts_rank(fts,q) DESC, id LIMIT %(k)s),
        fused AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, inference_confidence, inference_basis, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,statement,valid_from,author_principal,memory_type,restatements,salience,inference_confidence,inference_basis,rnk FROM vec UNION ALL SELECT id,statement,valid_from,author_principal,memory_type,restatements,salience,inference_confidence,inference_basis,rnk FROM fts) r
                  GROUP BY id,statement,valid_from,author_principal,memory_type,restatements,salience,inference_confidence,inference_basis)
        SELECT f.id, f.statement AS content, f.valid_from, f.author_principal AS author, f.memory_type, f.restatements, f.salience, f.inference_confidence, f.inference_basis, f.score, s.subject_entity
        FROM fused f JOIN {schema}.semantic s ON s.id=f.id ORDER BY f.score DESC, f.id LIMIT %(k)s;"""
        qt = fts_clamp(qtext, self.conn)
        with self.conn.cursor() as c:
            c.execute(sql, {"qv": vlit(qvec), "qt": qt, "ns": ns, "k": k})
            return c.fetchall()

    def search_semantic_temporal(self, schema, ns, qvec, qtext, k=40, *, start=None, end=None,
                                 current_only=False, order=None) -> list[dict]:
        """Temporal semantic retrieval: hybrid relevance (current_only → valid_to IS NULL)
        UNION-ed with event-time matches — facts inside [start,end] and, for first/last
        questions, the earliest/latest facts — so time-scoped evidence is guaranteed present.
        Returns id, content, valid_from. (Pure SQL; no LLM.)"""
        self._chk(schema)
        cur = "AND valid_to IS NULL" if current_only else ""
        base = f"""
        WITH vec AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, row_number() OVER (ORDER BY embedding <=> %(qv)s::{self.vtype}, id) rnk
                     FROM {schema}.semantic WHERE namespace=%(ns)s AND expired_at IS NULL {cur} ORDER BY embedding <=> %(qv)s::{self.vtype}, id LIMIT %(k)s),
        fts AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, row_number() OVER (ORDER BY ts_rank(fts,q) DESC, id) rnk
                FROM {schema}.semantic, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND expired_at IS NULL {cur} AND fts @@ q ORDER BY ts_rank(fts,q) DESC, id LIMIT %(k)s),
        fused AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,statement,valid_from,author_principal,memory_type,restatements,salience,rnk FROM vec UNION ALL SELECT id,statement,valid_from,author_principal,memory_type,restatements,salience,rnk FROM fts) r
                  GROUP BY id,statement,valid_from,author_principal,memory_type,restatements,salience)
        SELECT id, statement AS content, valid_from, author_principal AS author, memory_type, restatements, salience FROM fused ORDER BY score DESC, id LIMIT %(k)s"""
        params = {"qv": vlit(qvec), "qt": fts_clamp(qtext, self.conn), "ns": ns, "k": k}
        rows, seen = [], set()
        with self.conn.cursor() as c:
            c.execute(base, params)
            for r in c.fetchall():
                if r["id"] not in seen:
                    seen.add(r["id"]); rows.append(r)
            # event-time window guarantee
            if start and end:
                c.execute(f"SELECT id, statement AS content, valid_from, author_principal AS author, memory_type, restatements, salience "
                          f"FROM {schema}.semantic "
                          f"WHERE namespace=%s AND expired_at IS NULL AND valid_from >= %s AND valid_from < %s "
                          f"ORDER BY valid_from LIMIT %s", (ns, start, end, k))
                for r in c.fetchall():
                    if r["id"] not in seen:
                        seen.add(r["id"]); rows.append(r)
            # first/last boundary facts — apply same valid_to guard as the main CTE
            if order in ("asc", "desc"):
                c.execute(f"SELECT id, statement AS content, valid_from, author_principal AS author, memory_type, restatements, salience "
                          f"FROM {schema}.semantic "
                          f"WHERE namespace=%s AND expired_at IS NULL {cur} AND valid_from IS NOT NULL "
                          f"ORDER BY valid_from {('ASC' if order=='asc' else 'DESC')} LIMIT 6", (ns,))
                for r in c.fetchall():
                    if r["id"] not in seen:
                        seen.add(r["id"]); rows.append(r)
        return rows

    def timeline(self, schema, ns, entities, *, start=None, end=None, order="asc", limit=20,
                 current_only=False) -> list[dict]:
        """TIMELINE retrieval — the fix for 'vector can't find dated evidence'. Pull all
        facts about the query's entities, SORTED by event time (valid_from), optionally
        range-filtered (valid_from BETWEEN start AND end). A JOIN/range, not a cosine bet,
        so 'when did X happen' / 'what did X do in May 2023' surface the dated fact even
        though the question doesn't lexically match it. Pure SQL, no LLM.

        current_only=True: add AND valid_to IS NULL — used by the non-temporal entity-
        guarantee arm so superseded facts never reach b["dump"] for present-tense recall."""
        self._chk(schema)
        where = ["namespace=%s", "expired_at IS NULL", "valid_from IS NOT NULL"]
        if current_only:
            where.append("valid_to IS NULL")
        params = [ns]
        if entities:
            ors = []
            for e in entities:
                ors.append("(subject_entity = %s OR statement ILIKE %s)")
                params += [e, f"%{e}%"]
            where.append("(" + " OR ".join(ors) + ")")
        if start and end:
            where.append("valid_from >= %s AND valid_from < %s")
            params += [start, end]
        direction = "ASC" if order != "desc" else "DESC"
        params.append(limit)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, statement AS content, valid_from, author_principal AS author, memory_type "
                      f"FROM {schema}.semantic "
                      f"WHERE {' AND '.join(where)} ORDER BY valid_from {direction} LIMIT %s", params)
            return c.fetchall()

    def graph_expand(self, schema, ns, entity_names, *, hops=2, limit=20) -> list[dict]:
        """GRAPH TRAVERSAL (recursive CTE, no graph DB) — the relationship-reasoning a
        graph gives you. Seed from the query's entities, expand N hops over `edges`, then
        pull facts mentioned by the reachable entity set. Tests whether query-time graph
        traversal adds anything beyond the offline dossier pre-joining."""
        self._chk(schema)
        if not entity_names:
            return []
        names = [n.lower() for n in entity_names]
        sql = f"""
        WITH RECURSIVE seeds AS (
            SELECT id FROM {schema}.entities WHERE namespace=%(ns)s AND lower(name) = ANY(%(names)s)
        ),
        reach(id, hop) AS (
            SELECT id, 0 FROM seeds
            UNION
            SELECT CASE WHEN g.src_entity=r.id THEN g.dst_entity ELSE g.src_entity END, r.hop+1
            FROM reach r JOIN {schema}.edges g ON (g.src_entity=r.id OR g.dst_entity=r.id)
            WHERE r.hop < %(hops)s
        )
        SELECT DISTINCT s.id, s.statement AS content, s.valid_from
        FROM {schema}.mentions m
        JOIN {schema}.semantic s ON s.id=m.memory_id AND m.memory_kind='semantic'
        WHERE m.entity_id IN (SELECT id FROM reach) AND s.namespace=%(ns)s AND s.expired_at IS NULL
        LIMIT %(lim)s"""
        with self.conn.cursor() as c:
            c.execute(sql, {"ns": ns, "names": names, "hops": hops, "lim": limit})
            return c.fetchall()

    def get_entity(self, schema, ns, name, *, depth=1, fact_limit=20) -> dict | None:
        """Entity lookup + its graph neighbourhood + the facts that mention it.
        depth=1 returns direct neighbours; depth>=2 expands over `edges` (recursive CTE).
        Pure SQL over the associative graph — no LLM, no graph DB."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, name FROM {schema}.entities WHERE namespace=%s AND lower(name)=lower(%s)",
                      (ns, name))
            ent = c.fetchone()
            if not ent:
                return None
            eid = ent["id"]
            # neighbours up to `depth` hops, with the edge weight of the first hop
            c.execute(f"""
                WITH RECURSIVE reach(id, hop) AS (
                    SELECT %(eid)s::bigint, 0
                    UNION
                    SELECT CASE WHEN g.src_entity=r.id THEN g.dst_entity ELSE g.src_entity END, r.hop+1
                    FROM reach r JOIN {schema}.edges g ON (g.src_entity=r.id OR g.dst_entity=r.id)
                    WHERE r.hop < %(depth)s
                )
                SELECT DISTINCT e.name, max(g.weight) AS weight
                FROM reach r
                JOIN {schema}.edges g ON (g.src_entity=r.id OR g.dst_entity=r.id)
                JOIN {schema}.entities e ON e.id = CASE WHEN g.src_entity=r.id THEN g.dst_entity ELSE g.src_entity END
                WHERE e.id <> %(eid)s AND e.namespace=%(ns)s
                GROUP BY e.name ORDER BY weight DESC LIMIT 50
            """, {"eid": eid, "depth": depth, "ns": ns})
            related = [{"name": r["name"], "weight": float(r["weight"] or 0)} for r in c.fetchall()]
            c.execute(f"""
                SELECT DISTINCT s.id, s.statement AS content, s.valid_from, s.valid_to
                FROM {schema}.mentions m
                JOIN {schema}.semantic s ON s.id=m.memory_id AND m.memory_kind='semantic'
                WHERE m.entity_id=%s AND s.namespace=%s AND s.expired_at IS NULL
                ORDER BY s.valid_from DESC NULLS LAST LIMIT %s
            """, (eid, ns, fact_limit))
            facts = c.fetchall()
        return {"entity": {"id": eid, "name": ent["name"]}, "related": related, "facts": facts}

    def get_related(self, schema, ns, name, *, limit=50) -> list[dict]:
        """Adjacency list for an entity — direct neighbours over `edges`, weight-ranked."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"""
                SELECT e2.name, g.weight
                FROM {schema}.entities e1
                JOIN {schema}.edges g ON (g.src_entity=e1.id OR g.dst_entity=e1.id)
                JOIN {schema}.entities e2 ON e2.id = CASE WHEN g.src_entity=e1.id THEN g.dst_entity ELSE g.src_entity END
                WHERE e1.namespace=%s AND lower(e1.name)=lower(%s) AND e2.id<>e1.id
                ORDER BY g.weight DESC LIMIT %s
            """, (ns, name, limit))
            return [{"name": r["name"], "weight": float(r["weight"] or 0)} for r in c.fetchall()]

    def community(self, schema, ns, name, *, max_nodes=200) -> dict | None:
        """COMMUNITY (connected component) for an entity — the cluster it belongs to,
        found by expanding the co-mention `edges` graph to convergence (recursive CTE,
        UNION dedups → terminates). A dependency-free stand-in for Louvain: members of
        the same densely-connected neighbourhood surface together."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, name FROM {schema}.entities WHERE namespace=%s AND lower(name)=lower(%s)",
                      (ns, name))
            seed = c.fetchone()
            if not seed:
                return None
            c.execute(f"""
                WITH RECURSIVE comp(id) AS (
                    SELECT %(eid)s::bigint
                    UNION
                    SELECT CASE WHEN g.src_entity=comp.id THEN g.dst_entity ELSE g.src_entity END
                    FROM comp JOIN {schema}.edges g ON (g.src_entity=comp.id OR g.dst_entity=comp.id)
                    WHERE g.namespace=%(ns)s
                )
                SELECT e.name FROM comp JOIN {schema}.entities e ON e.id=comp.id
                WHERE e.id <> %(eid)s ORDER BY e.name LIMIT %(lim)s
            """, {"eid": seed["id"], "ns": ns, "lim": max_nodes})
            members = [r["name"] for r in c.fetchall()]
        return {"entity": seed["name"], "community": members, "size": len(members) + 1}

    def contradictions(self, schema, ns, *, limit=50) -> list[dict]:
        """POTENTIAL CONTRADICTIONS — currently-valid facts where the SAME subject+predicate
        carries MORE THAN ONE distinct object (e.g. lives_in Austin AND lives_in Seattle,
        both un-superseded). Deterministic SQL, no LLM. Non-blocking signal: multi-valued
        predicates (visited, did) legitimately appear here too — surfaces for review."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"""
                SELECT subject_entity, predicate,
                       array_agg(DISTINCT object) AS objects,
                       array_agg(id ORDER BY id) AS ids,
                       count(DISTINCT object) AS n
                FROM {schema}.semantic
                WHERE namespace=%s AND expired_at IS NULL AND valid_to IS NULL
                  AND subject_entity IS NOT NULL AND predicate IS NOT NULL AND object IS NOT NULL
                GROUP BY subject_entity, predicate
                HAVING count(DISTINCT object) > 1
                ORDER BY count(DISTINCT object) DESC LIMIT %s
            """, (ns, limit))
            return [{"subject": r["subject_entity"], "predicate": r["predicate"],
                     "objects": r["objects"], "ids": r["ids"]} for r in c.fetchall()]

    def orphan_entities_sql(self, schema) -> str:
        """SQL for the orphan-entities count (issue #69): entities that appear as
        neither endpoint of any edge. Exposed as a method (not inlined only in health())
        so a regression test can EXPLAIN the exact production query text instead of a
        hand-copied approximation that could silently drift out of sync.

        Deliberately NOT a single correlated `NOT EXISTS (... WHERE g.src_entity=e.id OR
        g.dst_entity=e.id)` — an OR across two columns can't be satisfied by one index
        scan (Postgres can't turn "src_entity=X OR dst_entity=X" into a single btree
        seek), so that form forces a per-entity scan over ALL of `edges`, O(entities x
        edges) — confirmed via EXPLAIN ANALYZE to blow past the 15s statement_timeout on
        a namespace at real production scale (~20k entities / ~19k edges): the OR form
        costs ~33M and takes ~9.8s on 190k edges spread across ten namespaces, this
        rewrite costs ~6.5K and takes ~20ms on the same data.

        Each arm of this UNION is instead an UNCORRELATED scan filtered only by
        namespace (no reference to e.id), so it's a single index-only scan of the
        `(namespace, src_entity, dst_entity)` unique index per arm — namespace is the
        index's leading column (bounds the scan to just this namespace's edges) and
        src_entity/dst_entity are both already IN the index, so neither arm needs a heap
        fetch. Total cost is O(edges in namespace), independent of entity count.
        src_entity/dst_entity are NOT NULL (schema.sql), so NOT IN is NULL-safe here —
        it would otherwise misbehave if the subquery could ever produce a NULL."""
        return (f"SELECT count(*) n FROM {schema}.entities e WHERE e.namespace=%s "
                f"AND e.id NOT IN ("
                f"SELECT src_entity FROM {schema}.edges WHERE namespace=%s "
                f"UNION "
                f"SELECT dst_entity FROM {schema}.edges WHERE namespace=%s)")

    def health(self, schema, ns) -> dict:
        """KNOWLEDGE HEALTH — a 0-100 score from structural signals over one namespace:
        contradictions, orphan entities (no edges), and the superseded ratio. Pure SQL.

        issue #69: each signal below is queried independently and, on a
        RECALL_ARM_FAILURES-class error (e.g. a canceled/timed-out statement), degrades
        to None for THAT signal alone instead of failing the whole report — reuses the
        same record_arm_failure/classify_arm_failure helpers #41 fix C already uses for
        recall arms, so this diagnostic fails soft the same way every recall arm does.
        Safe to keep going on the same cursor after a failed statement: every BrainStore
        connection is autocommit (see classify_arm_failure's docstring), so a canceled
        statement doesn't poison a shared transaction for the signals queried after it.
        score is computed only from the signals that succeeded."""
        self._chk(schema)
        reasons = []
        with self.conn.cursor() as c:
            def one(sql, *p):
                c.execute(sql, p); return c.fetchone()["n"]

            def signal(arm, fn):
                try:
                    return fn()
                except RECALL_ARM_FAILURES as e:
                    record_arm_failure(reasons, ns, arm, e, hint=classify_arm_failure(self.conn, e))
                    return None

            facts_current = signal("facts_current", lambda: one(
                f"SELECT count(*) n FROM {schema}.semantic WHERE namespace=%s AND valid_to IS NULL AND expired_at IS NULL", ns))
            facts_super = signal("facts_superseded", lambda: one(
                f"SELECT count(*) n FROM {schema}.semantic WHERE namespace=%s AND valid_to IS NOT NULL", ns))
            facts_expired = signal("facts_expired", lambda: one(
                f"SELECT count(*) n FROM {schema}.semantic WHERE namespace=%s AND expired_at IS NOT NULL", ns))
            ent_total = signal("entities", lambda: one(
                f"SELECT count(*) n FROM {schema}.entities WHERE namespace=%s", ns))
            orphans = signal("orphan_entities", lambda: one(
                self.orphan_entities_sql(schema), ns, ns, ns))
            contra_rows = signal("contradictions", lambda: self.contradictions(schema, ns, limit=1000))
        contra = None if contra_rows is None else len(contra_rows)
        # issue #69: score is None (not a best-effort number that silently ignores a
        # failed input) if any signal IT DEPENDS ON failed -- a namespace with real
        # contradictions/orphans that happened to fail THIS call must not score as if
        # they don't exist. This is what health_unavailable()'s score:null already does
        # for the "call never ran" fallback; a reader that only checks `score` (not
        # `degraded`) can't be misled by either path — the original issue #69 complaint
        # was exactly a knowledge_health output misleading a session.
        if contra is None or orphans is None or ent_total is None:
            score = None
        else:
            orphan_ratio = (orphans / ent_total) if ent_total else 0.0
            score = max(0, 100 - min(40, contra * 5) - int(min(30, orphan_ratio * 30)))
        out = {"score": score, "facts_current": facts_current, "facts_superseded": facts_super,
               "facts_expired": facts_expired, "entities": ent_total, "orphan_entities": orphans,
               "contradiction_groups": contra}
        if reasons:
            out["degraded"] = True
            out["degraded_reasons"] = reasons
        return out

    def get_semantic(self, schema, ns, semantic_id) -> dict | None:
        """Fetch a single semantic fact by id (for memory_delete confirmation)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, statement, expired_at FROM {schema}.semantic WHERE id=%s AND namespace=%s",
                      (semantic_id, ns))
            return c.fetchone()

    def pinned_constraints(self, schema, namespaces, *, cap=10) -> list[dict]:
        """PINNED CONSTRAINT INJECTION (0.1.6): every LIVE memory typed 'constraint' in the
        given namespaces — regardless of query similarity. Covers ALL THREE stores:
        semantic facts (extraction inheritance / direct fact writes), raw turns (local
        mode has no extraction, so the verbatim typed turn IS the constraint), and
        episodic events (an episode inherits 'constraint' only when its source turns are
        UNANIMOUSLY that type — so the episode body is constraint material). Oldest-first
        (constraints are durable ground rules — earliest laid down come first), deduped on
        content, capped. Pure SQL, no embedding involved."""
        self._chk(schema)
        nss = [ns for ns in namespaces if ns]
        if not nss or cap <= 0:
            return []
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT content, kind, ts, author, namespace FROM ("
                f"  SELECT statement AS content, 'fact'::text AS kind,"
                f"         COALESCE(valid_from, created_at) AS ts,"
                f"         author_principal AS author, namespace"
                f"  FROM {schema}.semantic WHERE namespace = ANY(%(nss)s)"
                f"    AND memory_type='constraint' AND valid_to IS NULL AND expired_at IS NULL"
                f"    AND (source_turn_ids IS NULL OR cardinality(source_turn_ids) = 0)"
                f"  UNION ALL"
                f"  SELECT text AS content, 'turn'::text AS kind, observed_at AS ts,"
                f"         author_principal AS author, namespace"
                f"  FROM {schema}.raw_turns WHERE namespace = ANY(%(nss)s) AND memory_type='constraint'"
                f"  UNION ALL"
                f"  SELECT text AS content, 'episode'::text AS kind,"
                f"         COALESCE(t_start, observed_at) AS ts,"
                f"         author_principal AS author, namespace"
                f"  FROM {schema}.episodic WHERE namespace = ANY(%(nss)s) AND memory_type='constraint'"
                f") u ORDER BY ts, content LIMIT %(lim)s",
                {"nss": nss, "lim": cap * 3})        # over-fetch: dedupe may drop rows
            # dedupe on content (a turn + its identical extracted fact), keep oldest, cap
            rows, seen = [], set()
            for r in c.fetchall():
                if r["content"] in seen:
                    continue
                seen.add(r["content"]); rows.append(r)
                if len(rows) >= cap:
                    break
        return rows

    _CONSTRAINT_RE = re.compile(
        r"\b(SHALL NOT|MUST NOT|SHOULD NOT|MAY NOT|SHALL|MUST|REQUIRED|SHOULD|PROHIBITED|FORBIDDEN)\b")

    def ingest_constraints(self, schema, ns, source, text, author=None) -> list[int]:
        """Parse normative constraints (RFC-2119 keywords) out of an architecture doc and
        store each as a kind='constraint' semantic fact tagged with the source. FTS-searchable
        immediately (embedding optional). Returns the inserted fact ids."""
        self._chk(schema)
        cands = []
        for raw in re.split(r"\n+", text or ""):
            line = raw.strip().lstrip("#-*>| ").strip()
            if not line:
                continue
            for sent in re.split(r"(?<=[.!?])\s+", line):
                sent = sent.strip()
                if len(sent) >= 8 and self._CONSTRAINT_RE.search(sent.upper()):
                    cands.append(sent[:1000])
        ids = []
        with self.conn.cursor() as c:
            # idempotent re-ingest: drop this source's prior constraints first
            c.execute(f"DELETE FROM {schema}.semantic WHERE namespace=%s AND kind='constraint' "
                      f"AND subject_entity=%s", (ns, source))
            for sent in cands:
                c.execute(
                    f"INSERT INTO {schema}.semantic(namespace,kind,statement,subject_entity,predicate,object,author_principal) "
                    f"VALUES(%s,'constraint',%s,%s,'constraint_of',%s,%s) RETURNING id",
                    (ns, sent, source, source, author))
                ids.append(c.fetchone()["id"])
        return ids

    def corpus_check(self, schema, ns, snippet, *, k=10) -> list[dict]:
        """Return the architecture constraints most relevant to a code snippet — FTS over
        the kind='constraint' facts (shared keywords, ranked). Pure SQL, no LLM."""
        self._chk(schema)
        words = list(dict.fromkeys(w.lower() for w in re.findall(r"[A-Za-z]{4,}", snippet or "")))
        if not words:
            return []
        q = " or ".join(words[:40])
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT id, statement AS content, subject_entity AS source, "
                f"ts_rank(fts, websearch_to_tsquery('english',%s)) AS score "
                f"FROM {schema}.semantic "
                f"WHERE namespace=%s AND kind='constraint' AND expired_at IS NULL "
                f"AND fts @@ websearch_to_tsquery('english',%s) "
                f"ORDER BY score DESC LIMIT %s", (q, ns, q, k))
            return c.fetchall()

    def migrate_namespace(self, schema, src, dst, *, mode="copy", like=None) -> dict:
        """Copy or MOVE memories from one namespace to another (same tenant schema).
        `copy` (default) duplicates raw turns + facts (optional `like` substring filter on
        the text) and rebuilds the entity graph in the destination from the facts' SPO —
        no LLM. `move` relocates the WHOLE namespace (raw turns + facts + episodes), rebuilds
        the destination graph, and drops the now-orphaned source graph. Returns counts."""
        self._chk(schema)
        if mode not in ("copy", "move"):
            raise ValueError("mode must be 'copy' or 'move'")
        likeval = f"%{like}%" if like else None
        with self.conn.cursor() as c:
            if mode == "move":
                c.execute(f"UPDATE {schema}.raw_turns SET namespace=%s WHERE namespace=%s", (dst, src))
                n_rt = c.rowcount
                c.execute(f"UPDATE {schema}.semantic SET namespace=%s WHERE namespace=%s "
                          f"RETURNING id, subject_entity, object", (dst, src))
                moved = c.fetchall(); n_sem = len(moved)
                c.execute(f"UPDATE {schema}.episodic SET namespace=%s WHERE namespace=%s", (dst, src))
                n_epi = c.rowcount
                # drop the now-orphaned source graph (facts moved out)
                c.execute(f"DELETE FROM {schema}.mentions m USING {schema}.entities e "
                          f"WHERE m.entity_id=e.id AND e.namespace=%s", (src,))
                c.execute(f"DELETE FROM {schema}.edges WHERE namespace=%s", (src,))
                c.execute(f"DELETE FROM {schema}.entities WHERE namespace=%s", (src,))
            else:  # copy
                rt_filter = " AND text ILIKE %s" if like else ""
                c.execute(f"INSERT INTO {schema}.raw_turns(namespace,session_id,speaker,text,observed_at,embedding) "
                          f"SELECT %s,session_id,speaker,text,observed_at,embedding FROM {schema}.raw_turns "
                          f"WHERE namespace=%s{rt_filter}",
                          ([dst, src] + ([likeval] if like else [])))
                n_rt = c.rowcount
                sem_filter = " AND statement ILIKE %s" if like else ""
                # copied facts lose source_turn_ids (raw-turn ids differ in the copy)
                c.execute(f"INSERT INTO {schema}.semantic"
                          f"(namespace,kind,statement,subject_entity,predicate,object,valid_from,valid_to,confidence,salience,embedding) "
                          f"SELECT %s,kind,statement,subject_entity,predicate,object,valid_from,valid_to,confidence,salience,embedding "
                          f"FROM {schema}.semantic WHERE namespace=%s{sem_filter} "
                          f"RETURNING id, subject_entity, object",
                          ([dst, src] + ([likeval] if like else [])))
                moved = c.fetchall(); n_sem = len(moved); n_epi = 0
        # rebuild the destination graph from the (moved/copied) facts' SPO — idempotent, no LLM
        for r in moved:
            subj = r.get("subject_entity")
            if not subj:
                continue
            se = self.upsert_entity(schema, dst, subj[:100])
            self.add_mention(schema, se, r["id"], "semantic")
            obj = r.get("object")
            if obj:
                oe = self.upsert_entity(schema, dst, obj[:100])
                self.add_mention(schema, oe, r["id"], "semantic")
                self.bump_edge(schema, dst, se, oe)
        return {"mode": mode, "src": src, "dst": dst, "raw_turns": n_rt, "facts": n_sem, "episodes": n_epi}

    def reconcile(self, schema, ns, statement, qvec=None, *, subject=None, predicate=None, k=8) -> dict:
        """Reconcile an EXTERNAL claim (e.g. from a local note the agent trusts) against
        memnos: does memnos hold a CURRENT fact about the same subject whose value is NOT
        reflected in the claim? Surfaces staleness/contradiction so the agent can tell the
        user 'your local memory is stale; memnos has a newer value (as of <date>)'.
        Deterministic — the caller supplies the parsed subject/predicate; no LLM here."""
        self._chk(schema)
        claim_l = (statement or "").lower()
        found, seen = [], set()

        def add(r, conflict):
            if r["id"] in seen:
                return
            seen.add(r["id"])
            found.append({"id": r["id"], "statement": r["statement"], "subject": r["subject_entity"],
                          "predicate": r["predicate"], "object": r["object"],
                          "valid_from": r["valid_from"], "conflict": conflict})

        with self.conn.cursor() as c:
            # SUBJECT arm — deterministic: current facts about the same subject (+predicate)
            if subject:
                pred_clause = " AND predicate ILIKE %s" if predicate else ""
                params = [ns, subject] + ([predicate] if predicate else [])
                c.execute(f"SELECT id, statement, subject_entity, predicate, object, valid_from "
                          f"FROM {schema}.semantic WHERE namespace=%s AND valid_to IS NULL AND expired_at IS NULL "
                          f"AND subject_entity ILIKE %s{pred_clause} ORDER BY valid_from DESC NULLS LAST LIMIT 20",
                          params)
                for r in c.fetchall():
                    obj = (r["object"] or "").strip()
                    add(r, bool(obj) and obj.lower() not in claim_l)
            # VECTOR arm — catch paraphrases / when no subject given: near-but-different facts
            if qvec is not None:
                c.execute(f"SELECT id, statement, subject_entity, predicate, object, valid_from, "
                          f"(embedding <=> %s::{self.vtype}) AS dist FROM {schema}.semantic "
                          f"WHERE namespace=%s AND valid_to IS NULL AND expired_at IS NULL AND embedding IS NOT NULL "
                          f"ORDER BY embedding <=> %s::{self.vtype} LIMIT %s", (vlit(qvec), ns, vlit(qvec), k))
                for r in c.fetchall():
                    near = r["dist"] is not None and r["dist"] < 0.45
                    if not near:
                        continue
                    obj = (r["object"] or "").strip()
                    differs = r["statement"].lower().strip() != claim_l.strip()
                    add(r, bool(obj) and obj.lower() not in claim_l and differs)
        conflicts = [f for f in found if f["conflict"]]
        return {"claim": statement, "matches": found, "conflicts": conflicts, "stale": bool(conflicts)}


    def store_entity_dossier(self, schema, entity_id, namespace, dossier_text, model_used=None) -> int:
        """UPSERT an entity dossier (issue #23): store or replace the generated summary
        paragraph for one entity. Returns the dossier row id."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.entity_dossiers(entity_id, namespace, dossier_text, model_used, generated_at) "
                f"VALUES(%s,%s,%s,%s,now()) "
                f"ON CONFLICT (entity_id, namespace) DO UPDATE "
                f"SET dossier_text=EXCLUDED.dossier_text, model_used=EXCLUDED.model_used, generated_at=now() "
                f"RETURNING id",
                (entity_id, namespace, dossier_text, model_used))
            return c.fetchone()["id"]

    def get_entity_dossier(self, schema, namespace, entity_name) -> dict | None:
        """Retrieve the stored dossier for an entity, looked up by name (issue #23).
        Returns None when no dossier has been generated yet."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT d.id, e.name, d.dossier_text, d.generated_at, d.model_used "
                f"FROM {schema}.entity_dossiers d "
                f"JOIN {schema}.entities e ON e.id = d.entity_id "
                f"WHERE d.namespace=%s AND lower(e.name)=lower(%s) "
                f"LIMIT 1",
                (namespace, entity_name))
            return c.fetchone()

    def counts(self, schema) -> dict:
        self._chk(schema)
        out = {}
        with self.conn.cursor() as c:
            for t in ("raw_turns", "episodic", "semantic", "entities", "mentions", "edges", "provenance"):
                c.execute(f"SELECT count(*) AS n FROM {schema}.{t}")
                out[t] = c.fetchone()["n"]
        return out
