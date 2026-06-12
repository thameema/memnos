"""Recall latency gate (issue #12) — warm-path budget, per-stage audit timings,
deadline degradation, and the reranker knobs.

Seeds a LARGE synthetic namespace directly in SQL (random embeddings generated
server-side — no LLM, no OpenAI, works in free local-384 mode), then asserts over
HTTP against the running server:

  1. WARM BUDGET: p50 of the NON-EMBED stage sum (sql_ms + staleness_ms + rerank_ms)
     over 20 warm recalls stays under MEMNOS_LATENCY_BUDGET_MS (default 500). The
     embed stage is excluded because in OpenAI mode it is a network round-trip the
     server cannot control; when the embed is local/cached the TOTAL is asserted too.
  2. AUDIT TIMINGS: every successful recall writes detail={embed_ms, sql_ms,
     staleness_ms, rerank_ms, total_ms} to the audit ledger.
  3. QUERY-EMBED CACHE: an identical repeated query within the TTL skips the embed
     round-trip (embed_ms == 0 on the repeat).
  4. DEADLINE: an expired deadline_ms returns 200 + best-available memories +
     degraded:true (and the degraded flag lands in the audit detail).
  5. KNOBS (in-process, core.rerank): MEMNOS_RERANK=0 → retrieval-order passthrough
     without loading the model; MEMNOS_RERANK_CAP=N → ≤N candidates cross-encoded,
     beyond-cap rows kept in retrieval order strictly below the reranked minimum.

Sizing: MEMNOS_LATENCY_FACTS (default 5000 — CI-friendly) sets the seeded fact count
(plus facts/5 raw turns). The field gate runs the full 45000 locally:
    MEMNOS_LATENCY_FACTS=45000 MEMNOS_DSN=... python tests/test_recall_latency.py
"""
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row

from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"
NS = "test:latency"
N_FACTS = max(100, int(os.environ.get("MEMNOS_LATENCY_FACTS", "5000")))
N_TURNS = N_FACTS // 5
BUDGET_MS = float(os.environ.get("MEMNOS_LATENCY_BUDGET_MS", "500"))
PASS = FAIL = 0

STAGES = ("embed_ms", "sql_ms", "staleness_ms", "rerank_ms", "total_ms")
# 20 distinct warm queries spanning the recall arms: entity-guarantee (proper nouns),
# temporal (dates / 'when'), broad (status sweeps), and plain hybrid.
QUERIES = [
    "What is the current status of project Alpha?",
    "When did the Gateway service hit its memory threshold?",
    "Where do we stand with the Ledger migration?",
    "Which milestones did project Beta reach in March?",
    "What changed in the Search cluster last week?",
    "Who raised the worker cap on the Billing pipeline?",
    "What is the rate limit of the Ingest service now?",
    "Summarize the latest on the Deploy rollout.",
    "What happened to project Gamma in 2025?",
    "Is the Auth service still blocked by the schema rollout?",
    "What throughput does the Alpha pipeline handle?",
    "When was the capacity review for the Gateway cluster?",
    "What are the open incidents on the Ledger service?",
    "Which projects reached milestone three?",
    "What did the on-call rotation flag about Search?",
    "How is the Billing cutover coming along?",
    "What version is the Ingest backend running?",
    "What was decided about the Deploy worker cap?",
    "When did project Delta finish the security sign-off?",
    "What are the p95 latency numbers for the Auth cluster?",
]


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def call(path, token, body, timeout=120):
    req = urllib.request.Request(URL + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def cleanup(conn):
    with conn.cursor() as c:
        for t in ("semantic", "raw_turns", "episodic"):
            c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (NS,))
        c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals p "
                  "WHERE t.principal_id=p.id AND p.name='latency_bot'")
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals p "
                  "WHERE g.principal_id=p.id AND p.name='latency_bot'")
        c.execute("DELETE FROM memnos_control.principals WHERE name='latency_bot'")


def seed(conn, dim):
    """Seed N_FACTS facts + N_TURNS turns with SQL-generated random embeddings (the
    correlated `WHERE g.i = g.i` keeps the volatile subquery per-row). Batched so HNSW
    maintenance shows progress instead of one multi-minute statement."""
    rndvec = ("('[' || (SELECT string_agg(round((random()-0.5)::numeric, 4)::text, ',') "
              f"FROM generate_series(1, {dim}) WHERE g.i = g.i) || ']')::halfvec")
    projects = "ARRAY['Alpha','Beta','Gamma','Delta','Gateway','Search','Ledger','Billing','Ingest','Deploy','Auth']"
    t0 = time.perf_counter()
    with conn.cursor() as c:
        for lo in range(1, N_FACTS + 1, 5000):
            hi = min(lo + 4999, N_FACTS)
            c.execute(f"""
                INSERT INTO {SCHEMA}.semantic
                    (namespace, kind, statement, subject_entity, predicate, object,
                     valid_from, observed_at, salience, embedding)
                SELECT %(ns)s, 'fact',
                       'Project ' || ({projects})[1 + g.i %% 11] || ' reached milestone '
                         || (g.i %% 9) || ' on 2025-' || lpad((1 + g.i %% 12)::text, 2, '0')
                         || '-' || lpad((1 + g.i %% 28)::text, 2, '0')
                         || ' with throughput ' || (100 + g.i %% 900)
                         || ' requests per second (fact ' || g.i || ').',
                       'project-' || (g.i %% 60), 'status', 'milestone-' || (g.i %% 9),
                       now() - ((g.i %% 700) || ' days')::interval,
                       now() - ((g.i %% 700) || ' days')::interval,
                       0.5, {rndvec}
                FROM generate_series(%(lo)s::int, %(hi)s::int) AS g(i)""",
                {"ns": NS, "lo": lo, "hi": hi})
            print(f"    seeded facts {hi}/{N_FACTS} ({time.perf_counter()-t0:.0f}s)", flush=True)
        for lo in range(1, N_TURNS + 1, 5000):
            hi = min(lo + 4999, N_TURNS)
            c.execute(f"""
                INSERT INTO {SCHEMA}.raw_turns(namespace, session_id, speaker, text,
                                               observed_at, embedding)
                SELECT %(ns)s, 's' || (g.i %% 40), 'user',
                       'We discussed the ' || ({projects})[1 + g.i %% 11]
                         || ' rollout today; the team flagged the worker cap and the p95 '
                         || 'latency numbers, and agreed to revisit milestone ' || (g.i %% 9)
                         || ' next sprint (turn ' || g.i || ').',
                       now() - ((g.i %% 700) || ' days')::interval, {rndvec}
                FROM generate_series(%(lo)s::int, %(hi)s::int) AS g(i)""",
                {"ns": NS, "lo": lo, "hi": hi})
            print(f"    seeded turns {hi}/{N_TURNS} ({time.perf_counter()-t0:.0f}s)", flush=True)
    print(f"  seeded {N_FACTS} facts + {N_TURNS} turns in {time.perf_counter()-t0:.0f}s", flush=True)


def recall_details(conn, n):
    """Newest-first audit detail rows for successful recalls on the test namespace."""
    with conn.cursor() as c:
        c.execute("SELECT detail FROM memnos_control.audit_log "
                  "WHERE action='recall' AND namespace=%s AND ok "
                  "ORDER BY ts DESC, id DESC LIMIT %s", (NS, n))
        return [r["detail"] for r in c.fetchall()]


def knob_tests():
    """In-process checks on core.rerank: kill switch + candidate cap."""
    from core import rerank as rr
    cands = rr._prewarm_candidates(10)

    os.environ["MEMNOS_RERANK"] = "0"
    try:
        t0 = time.perf_counter()
        out = rr.rerank("which project is blocked?", cands)
        dt = time.perf_counter() - t0
        check("MEMNOS_RERANK=0 -> retrieval-order passthrough",
              [i for i, _ in out] == list(range(10))
              and all(out[j][1] > out[j + 1][1] for j in range(9)))
        check("kill switch never loads the model (<50ms)", dt < 0.05, f"{dt*1000:.0f}ms")
    finally:
        del os.environ["MEMNOS_RERANK"]

    os.environ["MEMNOS_RERANK_CAP"] = "4"
    try:
        out = rr.rerank("which project is blocked?", cands)
        head = [(i, s) for i, s in out if i < 4]
        tail = [(i, s) for i, s in out if i >= 4]
        check("cap: all 10 candidates still returned", len(out) == 10)
        check("cap: beyond-cap rows keep retrieval order",
              [i for i, _ in tail] == [4, 5, 6, 7, 8, 9])
        check("cap: beyond-cap scores strictly below reranked minimum",
              max(s for _, s in tail) < min(s for _, s in head))
    finally:
        del os.environ["MEMNOS_RERANK_CAP"]


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    with conn.cursor() as c:
        c.execute(f"SELECT atttypmod AS dim FROM pg_attribute "
                  f"WHERE attrelid='{SCHEMA}.raw_turns'::regclass AND attname='embedding'")
        dim = c.fetchone()["dim"]
    cleanup(conn)

    pid = Control.create_principal(conn, "latency_bot", "service")
    tok = Control.mint_token(conn, pid, "latency-test", None)
    Control.grant(conn, pid, NS, True, True)

    print(f"== seeding {N_FACTS} synthetic facts (dim={dim}) ==")
    seed(conn, dim)

    print("== warm latency: 20 recalls over the seeded namespace ==")
    st, _ = call("/recall", tok, {"namespace": NS, "query": "warmup question about project Alpha"})
    check("warmup recall 200", st == 200)
    wall = []
    for q in QUERIES:
        t0 = time.perf_counter()
        st, out = call("/recall", tok, {"namespace": NS, "query": q})
        wall.append((time.perf_counter() - t0) * 1000)
        if st != 200:
            check(f"recall 200 for {q!r}", False, json.dumps(out)[:120])
    details = [d for d in recall_details(conn, len(QUERIES)) if d]
    check("audit detail present on every warm recall", len(details) == len(QUERIES),
          f"got {len(details)}")
    check("audit detail carries all per-stage timings",
          details and all(all(k in d for k in STAGES) for d in details),
          json.dumps(details[0] if details else {}))

    nonembed = sorted(d["sql_ms"] + d["staleness_ms"] + d["rerank_ms"] for d in details)
    totals = sorted(d["total_ms"] for d in details)
    embeds = sorted(d["embed_ms"] for d in details)
    p = lambda v, q: v[min(len(v) - 1, int(q * len(v)))]
    print(f"  wall p50={statistics.median(wall):.0f}ms p95={p(sorted(wall), 0.95):.0f}ms | "
          f"server total p50={statistics.median(totals):.0f}ms p95={p(totals, 0.95):.0f}ms | "
          f"non-embed p50={statistics.median(nonembed):.0f}ms p95={p(nonembed, 0.95):.0f}ms | "
          f"embed p50={statistics.median(embeds):.0f}ms")
    if details:
        d = details[0]
        print(f"  stage sample: embed={d['embed_ms']} sql={d['sql_ms']} "
              f"staleness={d['staleness_ms']} rerank={d['rerank_ms']} total={d['total_ms']}")
    check(f"warm p50 non-embed stages < {BUDGET_MS:.0f}ms",
          details and statistics.median(nonembed) < BUDGET_MS,
          f"{statistics.median(nonembed):.0f}ms" if details else "no detail rows")
    if details and statistics.median(embeds) < 50:   # local/cached embeds: total budget too
        check(f"warm p50 TOTAL < {BUDGET_MS:.0f}ms (local-embed mode)",
              statistics.median(totals) < BUDGET_MS, f"{statistics.median(totals):.0f}ms")
    else:
        print("  (OpenAI-mode embeds — total budget assertion covered by non-embed sum)")

    print("== query-embed cache (repeat skips the round-trip) ==")
    rq = "what is the exact throughput of the Gateway pipeline right now?"
    call("/recall", tok, {"namespace": NS, "query": rq})
    call("/recall", tok, {"namespace": NS, "query": rq})
    d = recall_details(conn, 1)
    check("repeated query has embed_ms == 0 (cache hit)", d and d[0] and d[0]["embed_ms"] == 0)

    print("== deadline-aware recall ==")
    st, out = call("/recall", tok, {"namespace": NS, "query": "status of project Beta",
                                    "deadline_ms": 1})
    check("expired deadline returns 200", st == 200)
    check("response flags degraded:true", out.get("degraded") is True)
    check("degraded response still returns memories", bool(out.get("memories")))
    d = recall_details(conn, 1)
    check("audit detail records degraded", d and d[0] and d[0].get("degraded") is True)
    st, out = call("/recall", tok, {"namespace": NS, "query": "status of project Beta",
                                    "deadline_ms": "abc"})
    check("non-integer deadline_ms -> 400", st == 400)

    print("== reranker knobs (kill switch + candidate cap) ==")
    knob_tests()

    cleanup(conn)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
