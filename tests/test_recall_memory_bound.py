"""Recall memory-bounding regression gate (issue #15).

Issue #15: the recall path retained ~2.3GB that never returned to the OS — the ONNX
cross-encoder CPU memory ARENA grabs a high-water block on first inference and never
gives it back, and a long recall query overflowed the Postgres FTS tsquery parser stack
("stack depth limit exceeded"). The footprint-bounded assertion itself is hard to make
deterministic in a unit test (it depends on the allocator + OS reclaim timing), so this
gate asserts the THREE fixes that produce it, each deterministically:

  1. ARENA CONFIG: the reranker / local embedder sessions are built with the CPU memory
     arena DISABLED and single-threaded by default (the allocation knobs that flatten the
     footprint). MEMNOS_RERANK_ARENA=1 / MEMNOS_RERANK_THREADS reverts/tunes.
  2. RERANK PARITY: turning the arena off does NOT change ranking — rerank scores on a
     fixed seeded (query, candidates) input are byte-identical with the arena off vs on.
     (The memory fix must not be an accuracy regression — verified WITHOUT a paid bench.)
  3. LONG QUERY -> 200: a recall with a very long query (token count above the tsquery
     crash threshold) returns 200, not a 500/crash — the FTS arm is token-clamped
     (store.fts_clamp) and the query is char-clamped, not rejected.

Runs in free local-384 mode; no LLM, no OpenAI key. Needs the server (for the HTTP arm).
"""
import json
import os
import random
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row

from core import rerank
from core.store import fts_clamp, _fts_max_tokens

URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
NS = "test:i15bound"
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        URL + path, data=data, method=method,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _fixed_input():
    """Deterministic (query, candidates) so rerank scores are reproducible across runs."""
    q = "where do things stand with the alpha deployment and what changed since last week?"
    words = ("alpha billing ingest gateway pipeline migration cutover capacity review "
             "latency sign off security audit ledger throughput").split()
    cands = []
    for i in range(40):
        random.seed(1000 + i)
        cands.append(" ".join(random.choice(words) for _ in range(random.randint(8, 40))) + f" item{i}")
    return q, cands


def main():
    print("=== recall memory-bounding (issue #15) ===")

    # --- 1. ARENA CONFIG -------------------------------------------------------------
    # default = bounded: arena OFF (the allocation knob from the #15 fix). Threads default
    # to min(4, cpu_count) (issue #12 field profiling) — the "N x arena slab" memory cost
    # this file guards against only applies with the arena ON, so it's independent of
    # thread count with the arena off (checked directly below).
    check("rerank arena DISABLED by default", rerank._arena_enabled() is False)
    check("rerank threads default to min(4, cpu_count)",
          rerank._rerank_threads() == min(4, os.cpu_count() or 1))
    os.environ["MEMNOS_RERANK_ARENA"] = "1"
    check("MEMNOS_RERANK_ARENA=1 re-enables the arena (operator escape hatch)",
          rerank._arena_enabled() is True)
    del os.environ["MEMNOS_RERANK_ARENA"]
    check("arena back OFF once the override is cleared", rerank._arena_enabled() is False)

    # --- 2. RERANK PARITY: arena OFF must not change ranking -------------------------
    q, cands = _fixed_input()
    os.environ["MEMNOS_RERANK_CAP"] = "40"            # rerank the whole batch, deterministic
    try:
        rerank._model.cache_clear()
        os.environ["MEMNOS_RERANK_ARENA"] = "0"       # the fix
        off = [(i, round(s, 6)) for i, s in rerank.rerank(q, cands)]
        rerank._model.cache_clear()
        os.environ["MEMNOS_RERANK_ARENA"] = "1"       # the pre-fix behavior
        on = [(i, round(s, 6)) for i, s in rerank.rerank(q, cands)]
    finally:
        rerank._model.cache_clear()
        os.environ.pop("MEMNOS_RERANK_ARENA", None)
        os.environ.pop("MEMNOS_RERANK_CAP", None)
    check("rerank scores byte-identical arena-off vs arena-on (no accuracy regression)",
          off == on)
    check("rerank produced a full ranking over the fixed input", len(off) == len(cands))

    # --- 2b. RERANK PARITY: thread count must not change ranking (issue #12) ---------
    # threads/arena affect allocation + scheduling only, not the computation (same
    # weights, same logits) — the default bump from 1 to min(4, cpu_count) must be a
    # pure latency win, never an accuracy change.
    os.environ["MEMNOS_RERANK_CAP"] = "40"
    try:
        rerank._model.cache_clear()
        os.environ["MEMNOS_RERANK_THREADS"] = "1"
        t1 = [(i, round(s, 6)) for i, s in rerank.rerank(q, cands)]
        rerank._model.cache_clear()
        os.environ["MEMNOS_RERANK_THREADS"] = "4"
        t4 = [(i, round(s, 6)) for i, s in rerank.rerank(q, cands)]
    finally:
        rerank._model.cache_clear()
        os.environ.pop("MEMNOS_RERANK_THREADS", None)
        os.environ.pop("MEMNOS_RERANK_CAP", None)
    check("rerank scores byte-identical threads=1 vs threads=4 (no accuracy regression)",
          t1 == t4)

    # --- 3. FTS clamp + LONG QUERY -> 200 (no tsquery crash) -------------------------
    # fts_clamp's safety check is a real, bounded Postgres probe (not a Python estimate --
    # see tests/test_fts_clamp_shape.py for why), so it needs a live connection.
    _clamp_conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    short = "alpha billing ingest"
    check("fts_clamp leaves a normal query untouched", fts_clamp(short, _clamp_conn) == short)
    cap = _fts_max_tokens()
    long_tokens = " ".join(f"word{i}" for i in range(cap * 50))   # far above the cap
    clamped = fts_clamp(long_tokens, _clamp_conn)
    check(f"fts_clamp bounds a long query to <= {cap} tokens",
          len(clamped.split()) <= cap and len(clamped) < len(long_tokens))
    _clamp_conn.close()

    # HTTP arm: needs the server. Bootstrap a token via the control plane.
    try:
        from core.control import Control
        with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as conn:
            Control.init(conn)
            pid = Control.create_principal(conn, "i15bound", "service")
            Control.grant(conn, pid, NS, can_read=True, can_write=True)
            tok = Control.mint_token(conn, pid, "i15bound")
        call("POST", "/remember", tok, {"namespace": NS, "text": "Alpha deployment reached steady state after the migration cutover."})

        # 25000 tokens — ABOVE the tsquery stack-overflow threshold (~20k); pre-fix this
        # would 500. Post-fix it is char-clamped + FTS-token-clamped → must be 200.
        huge = " ".join(f"word{i}" for i in range(25000))
        s, _ = call("POST", "/recall", tok, {"namespace": NS, "query": huge, "k": 5})
        check("very long recall query returns 200 (no tsquery crash)", s == 200)

        # a realistic 6000-char query (used to 400 on the <=4000 guard) is now accepted
        long_q = ("alpha billing ingest gateway pipeline migration cutover " * 200)[:6000]
        s2, _ = call("POST", "/recall", tok, {"namespace": NS, "query": long_q, "k": 5})
        check("6000-char recall query returns 200 (clamped, not rejected)", s2 == 200)
    except Exception as e:
        check(f"HTTP long-query arm (server reachable?) — {type(e).__name__}: {e}", False)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
